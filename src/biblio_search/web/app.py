"""Local web interface: write a question, watch its strategy take shape and run it.

The server only listens on 127.0.0.1. Even so, any website open in the same browser could
send requests to it, so it also refuses foreign ``Host`` headers (DNS rebinding), foreign
``Origin`` headers and bodies that are not JSON (which a plain HTML form cannot send).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from biblio_search.catalog import DATABASE_LABELS, DIALECTS, SEARCHABLE
from biblio_search.check import apply_check, check_question
from biblio_search.eutils import MAX_RETMAX, EutilsError
from biblio_search.mesh import JsonBackend, MeshThesaurus
from biblio_search.models import QuestionError
from biblio_search.pipeline import (
    CountReceived,
    EquationBuilt,
    RecordsReceived,
    SearchBackend,
    search_progress,
)
from biblio_search.question_file import (
    dump_question,
    parse_question,
    parse_question_text,
    question_to_data,
)
from biblio_search.selection import select_databases
from biblio_search.web.views import (
    check_view,
    count_view,
    preview_view,
    record_view,
    selection_view,
)

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class NcbiClient(SearchBackend, JsonBackend, Protocol):
    """What the interface needs from NCBI: searching PubMed and reading MeSH."""


# El cuerpo es la pregunta tal cual: parse_question la valida con mensajes que nombran el campo.
QuestionBody = Annotated[dict[str, Any], Body()]
ClientFactory = Callable[[], AbstractAsyncContextManager[NcbiClient]]


class SearchRequest(BaseModel):
    question: dict[str, Any]
    max_records: int = Field(default=20, ge=0, le=MAX_RETMAX)


class ImportRequest(BaseModel):
    text: str


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


def create_app(
    open_client: ClientFactory,
    initial_question: Mapping[str, object] | None = None,
) -> FastAPI:
    """Build the application.

    ``open_client`` opens the NCBI client once for the whole life of the server, so every
    request shares its rate limiter. ``initial_question`` is shown when the page opens.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_client() as client:
            app.state.client = client
            yield

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

    @app.exception_handler(EutilsError)
    async def ncbi_error(request: Request, exc: EutilsError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, 502)

    def client() -> NcbiClient:
        value: NcbiClient = app.state.client
        return value

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
        return preview_view(question, options, SEARCHABLE)

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
        result = await check_question(question, MeshThesaurus(client()))
        fixed = apply_check(question, result)
        return {**check_view(result), "fixed": question_to_data(fixed, options)}

    @app.post("/api/search", response_class=EventSourceResponse)
    async def search(body: SearchRequest) -> AsyncIterator[ServerSentEvent]:
        # Ya se está mandando el flujo: los errores van como evento, no como código HTTP.
        try:
            question, options = parse_question(body.question)
            selection = select_databases(question, SEARCHABLE)
            yield ServerSentEvent(data=selection_view(selection), event="selection")
            for database in selection.pending:
                skipped = {"database": database.value, "label": DATABASE_LABELS[database]}
                yield ServerSentEvent(data=skipped, event="skipped")
            if not selection.searchable:
                raise QuestionError(
                    "none of the chosen databases can be searched yet; choose PubMed to search"
                )
            for database in selection.searchable:
                steps = search_progress(
                    question, options, DIALECTS[database], client(), max_records=body.max_records
                )
                async for event in steps:
                    match event:
                        case EquationBuilt():
                            built = {"database": event.database, "equation": event.equation}
                            yield ServerSentEvent(data=built, event="equation")
                        case CountReceived():
                            count = count_view(event.database, event.result)
                            yield ServerSentEvent(data=count, event="count")
                        case RecordsReceived():
                            records = [record_view(r) for r in event.records]
                            listed = {"database": event.database, "records": records}
                            yield ServerSentEvent(data=listed, event="records")
        except (QuestionError, EutilsError) as exc:
            yield ServerSentEvent(data={"message": str(exc)}, event="error")
            return
        yield ServerSentEvent(data={}, event="done")

    return app
