"""Local web interface: write a question, watch its strategy take shape and run it.

The server only listens on 127.0.0.1. Even so, any website open in the same browser could
send requests to it, so it also refuses foreign ``Host`` headers (DNS rebinding), foreign
``Origin`` headers and bodies that are not JSON (which a plain HTML form cannot send).
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from biblio_search.assistant import (
    MAX_QUESTION_LENGTH,
    AssistantError,
    LanguageModel,
    draft_question,
)
from biblio_search.catalog import DATABASE_LABELS, DIALECTS
from biblio_search.check import apply_checks, check_all, vocabularies_to_check
from biblio_search.decs import DecsMeshThesaurus, DecsSearch
from biblio_search.eric import EricClient
from biblio_search.eric_thesaurus import (
    EricApiThesaurus,
    EricFileThesaurus,
    EricThesaurusFile,
    describe_source,
)
from biblio_search.eutils import MAX_RETMAX
from biblio_search.export import to_csv, to_ris
from biblio_search.mesh import JsonBackend, MeshThesaurus
from biblio_search.models import (
    ArticleSummary,
    Database,
    DatabaseError,
    Discipline,
    QuestionError,
    QuestionFormat,
    SearchResult,
    StrategyOptions,
    Vocabulary,
)
from biblio_search.pipeline import (
    CountReceived,
    EquationBuilt,
    RecordsReceived,
    SearchBackend,
    SearchReport,
    search_progress,
)
from biblio_search.protocols import Thesaurus
from biblio_search.question_file import (
    dump_question,
    parse_question,
    parse_question_text,
    question_to_data,
)
from biblio_search.screening import (
    MAX_REASON_LENGTH,
    Decision,
    EntryFilter,
    FlowSummary,
    ScreeningError,
    ScreeningStore,
    merged_entries,
)
from biblio_search.search_log import SearchLog, build_log, render_log
from biblio_search.selection import select_databases
from biblio_search.snowball import (
    ALL_DIRECTIONS,
    DEFAULT_MAX_CITING,
    MAX_CITING,
    SnowballDirection,
    SnowballSource,
    snowball,
)
from biblio_search.tuning import tune
from biblio_search.web.views import (
    added_view,
    check_view,
    count_view,
    entry_view,
    flow_view,
    merged_view,
    preview_view,
    record_view,
    selection_view,
    snowball_view,
    tuning_view,
)

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Registros de búsqueda que se pueden descargar: los últimos, para no crecer sin límite.
KEPT_SEARCHES = 20
DownloadKind = Literal["log", "ris", "csv"]
_DOWNLOADS: dict[str, tuple[str, str]] = {
    "log": ("md", "text/markdown; charset=utf-8"),
    "ris": ("ris", "application/x-research-info-systems; charset=utf-8"),
    "csv": ("csv", "text/csv; charset=utf-8"),
}


class NcbiClient(SearchBackend, JsonBackend, Protocol):
    """What the interface needs from NCBI: searching PubMed and reading MeSH."""


# El cuerpo es la pregunta tal cual: parse_question la valida con mensajes que nombran el campo.
QuestionBody = Annotated[dict[str, Any], Body()]
ClientFactory = Callable[[], AbstractAsyncContextManager[NcbiClient]]
BackendFactory = Callable[[], AbstractAsyncContextManager[SearchBackend]]
DecsFactory = Callable[[], AbstractAsyncContextManager[DecsSearch]]
AssistantFactory = Callable[[], AbstractAsyncContextManager[LanguageModel]]


class SearchRequest(BaseModel):
    question: dict[str, Any]
    max_records: int = Field(default=20, ge=0, le=MAX_RETMAX)


class TuneRequest(BaseModel):
    question: dict[str, Any]
    none_relevant: bool = False


class ImportRequest(BaseModel):
    text: str


class DraftRequest(BaseModel):
    text: str = Field(max_length=MAX_QUESTION_LENGTH)
    discipline: Discipline | None = None
    format: QuestionFormat | None = None


class ScreenSearchRequest(BaseModel):
    search_id: str


class SnowballRequest(BaseModel):
    # None sigue las dos direcciones.
    direction: SnowballDirection | None = None
    max_citing: int = Field(default=DEFAULT_MAX_CITING, ge=1, le=MAX_CITING)


class DecisionRequest(BaseModel):
    # None devuelve el registro a pendiente.
    decision: Decision | None
    reason: str = Field(default="", max_length=MAX_REASON_LENGTH)


EMPTY_FLOW = FlowSummary(
    searches=0, identified=0, screened=0, included=0, excluded=0, maybe=0, exclusion_reasons=()
)
ScreeningExport = Literal["ris", "csv", "log"]


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


def create_app(
    open_client: ClientFactory,
    initial_question: Mapping[str, object] | None = None,
    open_backends: Mapping[Database, BackendFactory] | None = None,
    open_decs: DecsFactory | None = None,
    screening_path: Path | None = None,
    open_assistant: AssistantFactory | None = None,
    eric_thesaurus: EricThesaurusFile | None = None,
) -> FastAPI:
    """Build the application.

    ``open_client`` opens the NCBI client (PubMed and MeSH) and ``open_backends`` the client
    of every other searchable database. Each one is opened once for the whole life of the
    server, so every request shares its rate limiter. ``open_decs``, only when the user has a
    DeCS key, lets the check understand Spanish headings. ``initial_question`` is shown when
    the page opens. ``screening_path`` is the SQLite file where searches are screened; it is
    only created when the first search is added to it. ``open_assistant`` opens the local
    model that drafts a question from a sentence; without it that button answers an error.
    ERIC descriptors are checked in ``eric_thesaurus`` when given, else through the ERIC
    client of ``open_backends``; without either they are left unchecked.
    """
    others = dict(open_backends or {})
    searchable = frozenset({Database.PUBMED, *others})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            ncbi = await stack.enter_async_context(open_client())
            backends: dict[Database, SearchBackend] = {Database.PUBMED: ncbi}
            for database, factory in others.items():
                backends[database] = await stack.enter_async_context(factory())
            decs = await stack.enter_async_context(open_decs()) if open_decs else None
            app.state.decs = decs
            assistant = (
                await stack.enter_async_context(open_assistant()) if open_assistant else None
            )
            app.state.assistant = assistant
            app.state.client = ncbi
            app.state.backends = backends
            app.state.searches = OrderedDict()
            app.state.screening = None
            try:
                yield
            finally:
                store: ScreeningStore | None = app.state.screening
                if store is not None:
                    store.close()

    app = FastAPI(title="biblio-search", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))

    @app.middleware("http")
    async def same_origin_json(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "requests from other sites are refused"}, 403)
        content_type = request.headers.get("content-type", "")
        if request.method == "POST" and not content_type.startswith("application/json"):
            return JSONResponse({"error": "the body must be JSON"}, 415)
        return await call_next(request)

    @app.exception_handler(QuestionError)
    async def question_error(request: Request, exc: QuestionError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, 422)

    @app.exception_handler(DatabaseError)
    async def database_error(request: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, 502)

    @app.exception_handler(AssistantError)
    async def assistant_error(request: Request, exc: AssistantError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, 503)

    @app.exception_handler(ScreeningError)
    async def screening_error(request: Request, exc: ScreeningError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, 409)

    def client() -> NcbiClient:
        value: NcbiClient = app.state.client
        return value

    def backend(database: Database) -> SearchBackend:
        value: SearchBackend = app.state.backends[database]
        return value

    def keep(log: SearchLog) -> str:
        searches: OrderedDict[str, SearchLog] = app.state.searches
        search_id = secrets.token_urlsafe(9)
        searches[search_id] = log
        while len(searches) > KEPT_SEARCHES:
            searches.popitem(last=False)
        return search_id

    # Las operaciones de SQLite son locales y de milisegundos: se hacen en el propio bucle, que
    # además es el único hilo que toca la conexión (sqlite3 no la comparte entre hilos).
    def screening(*, create: bool) -> ScreeningStore | None:
        """The open screening file; None when it does not exist yet and ``create`` is false."""
        if screening_path is None:
            raise ScreeningError("this interface was started without a screening file")
        store: ScreeningStore | None = app.state.screening
        if store is None and (create or screening_path.exists()):
            store = ScreeningStore.open(screening_path)
            app.state.screening = store
        return store

    def screening_to_write() -> ScreeningStore:
        store = screening(create=False)
        if store is None:
            raise ScreeningError("there is nothing to screen yet: add a search first")
        return store

    def flow(store: ScreeningStore | None) -> dict[str, object]:
        return flow_view(store.summary() if store is not None else EMPTY_FLOW)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/initial")
    async def initial() -> dict[str, object]:
        return {"question": initial_question}

    @app.post("/api/preview")
    async def preview(data: QuestionBody) -> dict[str, object]:
        question, options = parse_question(data)
        return preview_view(question, options, searchable)

    @app.post("/api/import")
    async def import_question(body: ImportRequest) -> dict[str, object]:
        question, options = parse_question_text(body.text)
        return {"question": question_to_data(question, options)}

    @app.post("/api/export")
    async def export_question(data: QuestionBody) -> PlainTextResponse:
        question, options = parse_question(data)
        return PlainTextResponse(
            dump_question(question, options),
            media_type="application/toml",
            headers={"Content-Disposition": 'attachment; filename="question.toml"'},
        )

    @app.post("/api/check")
    async def check(data: QuestionBody) -> dict[str, object]:
        question, options = parse_question(data)
        decs: DecsSearch | None = app.state.decs
        vocabularies = vocabularies_to_check(question)
        thesauri: list[Thesaurus] = []
        if Vocabulary.MESH in vocabularies:
            mesh = MeshThesaurus(client())
            thesauri.append(mesh if decs is None else DecsMeshThesaurus(mesh, decs))
        eric = app.state.backends.get(Database.ERIC)
        if Vocabulary.ERIC in vocabularies and eric_thesaurus is not None:
            thesauri.append(EricFileThesaurus(eric_thesaurus))
        elif Vocabulary.ERIC in vocabularies and isinstance(eric, EricClient):
            thesauri.append(EricApiThesaurus(eric))
        results = await check_all(question, thesauri)
        fixed = apply_checks(question, results)
        views = [
            check_view(
                result,
                decs=decs is not None,
                source=describe_source(eric_thesaurus)
                if result.vocabulary == Vocabulary.ERIC
                else None,
            )
            for result in results
        ]
        return {"results": views, "fixed": question_to_data(fixed, options)}

    @app.post("/api/draft")
    async def draft(body: DraftRequest) -> dict[str, object]:
        model: LanguageModel | None = app.state.assistant
        if model is None:
            raise AssistantError("this interface was started without the local assistant")
        result = await draft_question(
            body.text,
            model,
            discipline=body.discipline,
            question_format=body.format,
            thesaurus=MeshThesaurus(client()),
        )
        return {
            "question": question_to_data(result.question, StrategyOptions()),
            "english": result.english,
            "model": result.model,
            "notes": list(result.notes),
            # El modelo escribe en inglés: el aviso de DeCS no aplica a su borrador.
            "check": {"results": [check_view(result.check, decs=True)]} if result.check else None,
        }

    @app.post("/api/search", response_class=EventSourceResponse)
    async def search(body: SearchRequest) -> AsyncIterator[ServerSentEvent]:
        # Ya se está mandando el flujo: los errores van como evento, no como código HTTP.
        try:
            question, options = parse_question(body.question)
            selection = select_databases(question, searchable)
            yield ServerSentEvent(data=selection_view(selection), event="selection")
            for database in selection.pending:
                skipped = {"database": database.value, "label": DATABASE_LABELS[database]}
                yield ServerSentEvent(data=skipped, event="skipped")
            if not selection.searchable:
                names = " or ".join(DATABASE_LABELS[d] for d in sorted(searchable))
                raise QuestionError(
                    f"none of the chosen databases can be searched yet; choose {names} to search"
                )
            reports: list[tuple[Database, SearchReport]] = []
            for database in selection.searchable:
                equation = ""
                result: SearchResult | None = None
                records: tuple[ArticleSummary, ...] = ()
                steps = search_progress(
                    question,
                    options,
                    DIALECTS[database],
                    backend(database),
                    max_records=body.max_records,
                )
                async for event in steps:
                    match event:
                        case EquationBuilt():
                            equation = event.equation
                            built = {"database": event.database, "equation": event.equation}
                            yield ServerSentEvent(data=built, event="equation")
                        case CountReceived():
                            result = event.result
                            count = count_view(database, event.result)
                            yield ServerSentEvent(data=count, event="count")
                        case RecordsReceived():
                            records = event.records
                            views = [record_view(database, r) for r in event.records]
                            listed = {"database": event.database, "records": views}
                            yield ServerSentEvent(data=listed, event="records")
                if result is None:
                    raise DatabaseError(f"{DATABASE_LABELS[database]} returned no count")
                reports.append((database, SearchReport(equation, result, records)))
            log = build_log(question, options, selection, reports, max_records=body.max_records)
        except (QuestionError, DatabaseError) as exc:
            yield ServerSentEvent(data={"message": str(exc)}, event="error")
            return
        yield ServerSentEvent(data=merged_view(keep(log), log), event="done")

    @app.get("/api/searches/{search_id}/{kind}")
    async def download(search_id: str, kind: DownloadKind) -> PlainTextResponse:
        log: SearchLog | None = app.state.searches.get(search_id)
        if log is None:
            raise HTTPException(404, "that search is no longer kept: run it again")
        if kind == "log":
            body = render_log(log)
        elif kind == "ris":
            body = to_ris(log.merged.records)
        else:
            body = to_csv(log.merged.records)
        extension, media_type = _DOWNLOADS[kind]
        stamp = log.searched_at.strftime("%Y%m%d-%H%M")
        name = f"biblio-search-{stamp}.{extension}"
        return PlainTextResponse(
            body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/screening")
    async def screening_state(which: EntryFilter = "pending") -> dict[str, object]:
        store = screening(create=False)
        entries = store.entries(which) if store is not None else ()
        return {
            "file": str(screening_path),
            "flow": flow(store),
            "entries": [entry_view(entry) for entry in entries],
            "to_snowball": len(store.to_snowball()) if store is not None else 0,
            "can_snowball": isinstance(app.state.backends.get(Database.OPENALEX), SnowballSource),
        }

    @app.post("/api/screening/searches")
    async def screen_search(body: ScreenSearchRequest) -> dict[str, object]:
        log: SearchLog | None = app.state.searches.get(body.search_id)
        if log is None:
            raise HTTPException(404, "that search is no longer kept: run it again")
        store = screening(create=True)
        if store is None:
            raise ScreeningError(f"cannot open {screening_path}")
        added = store.add_search(log)
        return {"added": added_view(added), "flow": flow(store)}

    @app.post("/api/screening/snowball")
    async def snowball_round(body: SnowballRequest) -> dict[str, object]:
        source = app.state.backends.get(Database.OPENALEX)
        if not isinstance(source, SnowballSource):
            raise ScreeningError("snowballing needs OpenAlex, and this interface has no client")
        store = screening_to_write()
        seeds = store.to_snowball()
        if not seeds:
            raise ScreeningError(
                "no included record is waiting to be snowballed: include some records first"
            )
        directions = (body.direction,) if body.direction is not None else ALL_DIRECTIONS
        run = await snowball(source, seeds, directions=directions, max_citing=body.max_citing)
        added = store.add_snowball(run)
        return {"added": added_view(added), "run": snowball_view(run), "flow": flow(store)}

    @app.post("/api/screening/records/{entry_id}")
    async def decide(entry_id: int, body: DecisionRequest) -> dict[str, object]:
        store = screening_to_write()
        if body.decision is None:
            entry = store.undecide(entry_id)
        else:
            entry = store.decide(entry_id, body.decision, body.reason)
        return {"entry": entry_view(entry), "flow": flow(store)}

    @app.get("/api/screening/export/{kind}")
    async def export_screening(
        kind: ScreeningExport, which: EntryFilter = "include"
    ) -> PlainTextResponse:
        store = screening_to_write()
        if kind == "log":
            body = "\n\n---\n\n".join(store.search_logs())
            name = "screening-searches.md"
        else:
            records = merged_entries(store.entries(which))
            body = to_ris(records) if kind == "ris" else to_csv(records)
            name = f"screening-{which}.{kind}"
        return PlainTextResponse(
            body,
            media_type=_DOWNLOADS[kind][1],
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/api/tune", response_class=EventSourceResponse)
    async def tune_search(body: TuneRequest) -> AsyncIterator[ServerSentEvent]:
        # Cada base tarda lo suyo (decenas de recuentos): se manda su informe en cuanto acaba.
        try:
            question, options = parse_question(body.question)
            selection = select_databases(question, searchable)
            if not selection.searchable:
                raise QuestionError("none of the chosen databases can be searched yet")
            for database in selection.searchable:
                started = {"database": database.value, "label": DATABASE_LABELS[database]}
                yield ServerSentEvent(data=started, event="tuning")
                report = await tune(
                    question,
                    options,
                    DIALECTS[database],
                    backend(database),
                    none_relevant=body.none_relevant,
                )
                yield ServerSentEvent(data=tuning_view(database, report), event="report")
        except (QuestionError, DatabaseError) as exc:
            yield ServerSentEvent(data={"message": str(exc)}, event="error")
            return
        yield ServerSentEvent(data={}, event="done")

    return app
