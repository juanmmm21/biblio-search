from __future__ import annotations

import json
import tomllib
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES, eric_fixture_for, load_fixture, mesh_fixture_for
from fastapi.testclient import TestClient

import biblio_search.web as web
from biblio_search import openalex
from biblio_search.__main__ import main
from biblio_search.assistant import AssistantError, LanguageModel
from biblio_search.decs import DecsDescriptor, DecsSearch, parse_descriptors
from biblio_search.eric import EricClient, EricConfig
from biblio_search.eric_thesaurus import EricThesaurusFile
from biblio_search.eutils import EutilsError, parse_search, parse_summaries
from biblio_search.models import ArticleSummary, Database, SearchResult
from biblio_search.openalex import OpenAlexError
from biblio_search.pipeline import SearchBackend
from biblio_search.ratelimit import RateLimiter
from biblio_search.web.app import KEPT_SEARCHES, BackendFactory, NcbiClient, create_app
from biblio_search.web.server import bind_local

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CAREGIVERS = EXAMPLES / "caregiver-burden.toml"
BASE_URL = "http://127.0.0.1:8000"


def example_data(name: str = "caregiver-burden.toml") -> dict[str, object]:
    with (EXAMPLES / name).open("rb") as handle:
        return tomllib.load(handle)


class FakeNcbi:
    """Recorded NCBI answers: the caregiver search and the MeSH fixtures."""

    def __init__(self, search_error: EutilsError | None = None) -> None:
        self.search_error = search_error
        self.searched: list[tuple[str, int]] = []

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        if self.search_error is not None:
            raise self.search_error
        self.searched.append((term, retmax))
        return parse_search(load_fixture("esearch_caregiver.json"))

    async def summaries(self, pmids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        return parse_summaries(load_fixture("esummary_caregiver.json"))

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        return mesh_fixture_for(endpoint, params)


class FakeOpenAlex:
    """Recorded OpenAlex answers for the caregiver search."""

    def __init__(self, search_error: OpenAlexError | None = None) -> None:
        self.search_error = search_error
        self.searched: list[str] = []

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        if self.search_error is not None:
            raise self.search_error
        self.searched.append(term)
        result = openalex.parse_search(load_fixture("openalex_search_caregiver.json"))
        return SearchResult(result.count, result.ids[:retmax], result.query_translation)

    async def summaries(self, ids: Sequence[str], /) -> tuple[ArticleSummary, ...]:
        works = load_fixture("openalex_works_caregiver.json")["results"]
        assert isinstance(works, list)
        by_id = {record.record_id: record for record in map(openalex.parse_work, works)}
        return tuple(by_id[work_id] for work_id in ids)


def make_client(
    fake: FakeNcbi | None = None,
    initial: Mapping[str, object] | None = None,
    other: SearchBackend | None = None,
    *,
    with_openalex: bool = True,
    decs: DecsSearch | None = None,
    screening: Path | None = None,
    assistant: LanguageModel | None = None,
    with_eric: bool = False,
    eric_thesaurus: EricThesaurusFile | None = None,
) -> TestClient:
    ncbi = fake or FakeNcbi()
    openalex_backend = other or FakeOpenAlex()

    @asynccontextmanager
    async def open_client() -> AsyncIterator[NcbiClient]:
        yield ncbi

    @asynccontextmanager
    async def open_openalex() -> AsyncIterator[SearchBackend]:
        yield openalex_backend

    @asynccontextmanager
    async def open_decs() -> AsyncIterator[DecsSearch]:
        assert decs is not None
        yield decs

    @asynccontextmanager
    async def open_assistant() -> AsyncIterator[LanguageModel]:
        assert assistant is not None
        yield assistant

    def open_eric() -> EricClient:
        def handler(request: httpx.Request) -> httpx.Response:
            name = eric_fixture_for(request.url.params["search"])
            return httpx.Response(200, content=(FIXTURES / name).read_bytes())

        async def no_wait(_: float) -> None:
            return None

        return EricClient(
            EricConfig(),
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=no_wait,
        )

    backends: dict[Database, BackendFactory] = (
        {Database.OPENALEX: open_openalex} if with_openalex else {}
    )
    if with_eric:
        backends[Database.ERIC] = open_eric
    app = create_app(
        open_client,
        initial,
        backends,
        open_decs if decs is not None else None,
        screening,
        open_assistant if assistant is not None else None,
        eric_thesaurus,
    )
    return TestClient(app, base_url=BASE_URL)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as test_client:
        yield test_client


def sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data += line.removeprefix("data:").strip()
        if data:
            events.append((name, json.loads(data)))
    return events


def test_index_and_static_files_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_preview_draws_tree_grid_databases_and_equation(client: TestClient) -> None:
    response = client.post("/api/preview", json=example_data())

    assert response.status_code == 200
    preview = response.json()
    tree = preview["tree"]
    assert tree["format"] == "spc"
    assert [c["role"] for c in tree["components"]] == ["situation", "population", "question"]
    rows = preview["grid"]["rows"]
    assert [row["label"] for row in rows] == [
        "anxiety or depression",
        "caregivers",
        "older people",
        "burden",
    ]
    assert rows[0]["terms"][0] == {
        "text": "Anxiety",
        "kind": "controlled",
        "vocabulary": "mesh",
        "vocabulary_label": "MeSH",
        "scope": "major_no_explode",
    }
    assert rows[3]["terms"][0] == {"text": "burden*", "kind": "free", "field": "title_abstract"}
    selection = preview["selection"]
    assert selection["mode"] == "automatic"
    assert [(c["database"], c["searchable"]) for c in selection["choices"]] == [
        ("pubmed", True),
        ("openalex", True),
    ]
    pubmed, openalex_equation = preview["equations"]
    assert pubmed["database"] == "pubmed"
    assert pubmed["equation"].startswith("(Anxiety[mj:noexp] OR Depression[mj]")
    assert pubmed["notes"] == []
    assert openalex_equation["label"] == "OpenAlex"
    assert openalex_equation["equation"].startswith("(anxiety OR depress*) AND")
    assert "Caregivers (MeSH)" in openalex_equation["notes"][0]


def test_without_an_openalex_client_it_is_not_searchable() -> None:
    with make_client(with_openalex=False) as test_client:
        preview = test_client.post("/api/preview", json=example_data()).json()
        events = sse_events(test_client.post("/api/search", json={"question": example_data()}).text)
    assert [(c["database"], c["searchable"]) for c in preview["selection"]["choices"]] == [
        ("pubmed", True),
        ("openalex", False),
    ]
    assert ("skipped", {"database": "openalex", "label": "OpenAlex"}) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["identified"] == 3


def test_preview_of_a_physics_question_builds_the_arxiv_equation(client: TestClient) -> None:
    preview = client.post("/api/preview", json=example_data("neural-decoders-qec.toml")).json()
    choices = preview["selection"]["choices"]
    assert [c["database"] for c in choices] == ["arxiv", "openalex"]
    # Este cliente de prueba no tiene backend de arXiv: se ve, pero no se puede buscar.
    assert choices[0]["searchable"] is False
    assert "the question narrows arXiv to some categories" in choices[0]["reasons"]
    data = example_data("neural-decoders-qec.toml")
    data["arxiv_categories"] = ["cs LG"]
    bad = client.post("/api/preview", json=data)
    assert bad.status_code == 422
    assert "not an arXiv category" in bad.text


def test_preview_notes_headings_searched_as_free_text(client: TestClient) -> None:
    data = example_data("peer-tutoring-reading.toml")
    data["databases"] = ["pubmed"]

    preview = client.post("/api/preview", json=data).json()

    assert preview["selection"]["mode"] == "manual"
    [equation] = preview["equations"]
    assert "Peer Teaching (ERIC)" in equation["notes"][0]


def test_preview_reports_the_field_that_is_wrong(client: TestClient) -> None:
    data = example_data()
    data["format"] = "spx"

    response = client.post("/api/preview", json=data)

    assert response.status_code == 422
    assert response.json() == {
        "error": "format must be one of: spc, pico, concepts (got 'spx')",
    }


def test_requests_from_other_sites_are_refused(client: TestClient) -> None:
    foreign = client.post("/api/preview", json=example_data(), headers={"Origin": "http://x.io"})
    assert foreign.status_code == 403
    same = client.post(
        "/api/preview", json=example_data(), headers={"Origin": "http://127.0.0.1:8000"}
    )
    assert same.status_code == 200


def test_bodies_that_are_not_json_are_refused(client: TestClient) -> None:
    response = client.post(
        "/api/preview",
        content=json.dumps(example_data()),
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_foreign_host_headers_are_refused() -> None:
    with make_client() as test_client:
        response = test_client.get("/", headers={"Host": "attacker.example"})
    assert response.status_code == 400


def test_initial_question_is_served() -> None:
    with make_client(initial={"question": "q"}) as test_client:
        assert test_client.get("/api/initial").json() == {"question": {"question": "q"}}
    with make_client() as test_client:
        assert test_client.get("/api/initial").json() == {"question": None}


def test_import_returns_the_question_without_defaults(client: TestClient) -> None:
    response = client.post("/api/import", json={"text": CAREGIVERS.read_text(encoding="utf-8")})

    assert response.status_code == 200
    question = response.json()["question"]
    assert question["format"] == "spc"
    older = question["components"][1]["conditions"][1]
    assert older == {
        "label": "older people",
        "controlled": ["Aged"],
        "free": ["elder*", "older people"],
    }


def test_import_rejects_broken_toml(client: TestClient) -> None:
    response = client.post("/api/import", json={"text": "question = "})
    assert response.status_code == 422
    assert "not valid TOML" in response.json()["error"]


def test_export_writes_a_toml_file_that_imports_back(client: TestClient) -> None:
    response = client.post("/api/export", json=example_data())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/toml")
    assert "question.toml" in response.headers["content-disposition"]
    back = client.post("/api/import", json={"text": response.text}).json()["question"]
    assert (
        back == client.post("/api/import", json={"text": CAREGIVERS.read_text()}).json()["question"]
    )


def test_check_reports_headings_and_returns_the_fixed_question(client: TestClient) -> None:
    data = example_data()
    components = data["components"]
    assert isinstance(components, list)
    caregivers = components[1]["conditions"][0]
    caregivers["controlled"] = ["Carers", "Made Up Heading"]

    response = client.post("/api/check", json=data)

    assert response.status_code == 200
    body = response.json()
    (result,) = body["results"]
    assert result["source"] is None
    statuses = {check["term"]: check["status"] for check in result["checks"]}
    assert statuses == {
        "Anxiety": "valid",
        "Depression": "valid",
        "Carers": "entry_term",
        "Made Up Heading": "not_found",
        "Aged": "valid",
    }
    carers = next(c for c in result["checks"] if c["term"] == "Carers")
    assert (carers["heading"], carers["identifier"]) == ("Caregivers", "D017028")
    aged = next(c for c in result["checks"] if c["term"] == "Aged")
    assert "narrower" in aged["note"]
    fixed = body["fixed"]["components"][1]["conditions"][0]
    assert fixed["controlled"] == ["Caregivers"]
    assert "Made Up Heading" in fixed["free"]
    assert result["unchecked"] == []


def test_check_reports_ncbi_failures_as_bad_gateway() -> None:
    class Broken(FakeNcbi):
        async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
            raise EutilsError("NCBI is down")

    with make_client(Broken()) as test_client:
        response = test_client.post("/api/check", json=example_data())
    assert response.status_code == 502
    assert response.json() == {"error": "NCBI is down"}


def test_search_streams_each_step() -> None:
    fake = FakeNcbi()
    other = FakeOpenAlex()
    with make_client(fake, other=other) as test_client:
        response = test_client.post(
            "/api/search", json={"question": example_data(), "max_records": 3}
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert [name for name, _ in events] == [
        "selection",
        "equation",
        "count",
        "records",
        "equation",
        "count",
        "records",
        "done",
    ]
    by_database: dict[object, dict[str, dict[str, object]]] = {}
    for name, data in events[1:-1]:
        by_database.setdefault(data["database"], {})[name] = data
    pubmed = by_database["pubmed"]
    assert pubmed["equation"]["equation"] == fake.searched[0][0]
    assert fake.searched[0][1] == 3
    assert pubmed["count"]["count"] == 2808
    assert pubmed["count"]["label"] == "PubMed"
    assert pubmed["count"]["translation"]
    records = pubmed["records"]["records"]
    assert isinstance(records, list)
    assert len(records) == 3
    assert records[0]["id_label"] == "PMID"
    assert records[0]["url"] == f"https://pubmed.ncbi.nlm.nih.gov/{records[0]['id']}/"
    found = by_database["openalex"]
    assert found["equation"]["equation"] == other.searched[0]
    assert found["count"]["count"] == 2516
    assert found["count"]["label"] == "OpenAlex"
    works = found["records"]["records"]
    assert isinstance(works, list)
    assert [w["id"] for w in works] == ["W2145256182", "W4391296183", "W1923367678"]
    assert works[0]["id_label"] == "OpenAlex"
    assert works[0]["url"] == "https://openalex.org/W2145256182"
    assert works[0]["doi"] == "10.1093/ageing/afq053"


def test_finished_search_can_be_downloaded() -> None:
    with make_client() as test_client:
        response = test_client.post(
            "/api/search", json={"question": example_data(), "max_records": 3}
        )
        name, done = sse_events(response.text)[-1]
        assert name == "done"
        assert (done["identified"], done["duplicates"], done["unique"]) == (6, 0, 6)
        downloads = done["downloads"]
        assert isinstance(downloads, dict)
        log = test_client.get(downloads["log"])
        ris = test_client.get(downloads["ris"])
        csv = test_client.get(downloads["csv"])
        missing = test_client.get("/api/searches/nope/log")
        unknown = test_client.get(f"/api/searches/{done['search_id']}/pdf")

    assert log.status_code == 200 and log.headers["content-type"].startswith("text/markdown")
    assert log.text.startswith("# Search log")
    assert 'filename="biblio-search-' in log.headers["content-disposition"]
    assert log.headers["content-disposition"].endswith('.md"')
    assert ris.text.count("ER  - ") == 6
    assert csv.text.startswith("\ufefftitle,")
    assert missing.status_code == 404 and "run it again" in missing.json()["detail"]
    assert unknown.status_code == 422


def test_only_the_latest_searches_are_kept() -> None:
    with make_client() as test_client:
        ids = []
        for _ in range(KEPT_SEARCHES + 1):
            body = {"question": example_data(), "max_records": 3}
            response = test_client.post("/api/search", json=body)
            ids.append(sse_events(response.text)[-1][1]["search_id"])
        first = test_client.get(f"/api/searches/{ids[0]}/csv")
        last = test_client.get(f"/api/searches/{ids[-1]}/csv")
    assert (first.status_code, last.status_code) == (404, 200)


def _finished_search(test_client: TestClient) -> str:
    response = test_client.post("/api/search", json={"question": example_data(), "max_records": 3})
    name, done = sse_events(response.text)[-1]
    assert name == "done"
    search_id = done["search_id"]
    assert isinstance(search_id, str)
    return search_id


def test_screening_starts_empty_without_creating_the_file(tmp_path: Path) -> None:
    path = tmp_path / "screening.sqlite"
    with make_client(screening=path) as test_client:
        state = test_client.get("/api/screening").json()
        decided = test_client.post("/api/screening/records/1", json={"decision": "include"})
        export = test_client.get("/api/screening/export/ris")
    assert state["file"] == str(path) and state["entries"] == []
    assert state["flow"]["screened"] == 0 and state["flow"]["exclusion_reasons"] == []
    assert decided.status_code == 409 and "add a search first" in decided.json()["error"]
    assert export.status_code == 409
    assert not path.exists()


def test_a_search_is_screened_and_resumed(tmp_path: Path) -> None:
    path = tmp_path / "screening.sqlite"
    with make_client(screening=path) as test_client:
        search_id = _finished_search(test_client)
        added = test_client.post("/api/screening/searches", json={"search_id": search_id})
        again = test_client.post("/api/screening/searches", json={"search_id": search_id})
        pending = test_client.get("/api/screening").json()["entries"]
        first, second, third = (entry["id"] for entry in pending[:3])
        include = test_client.post(f"/api/screening/records/{first}", json={"decision": "include"})
        test_client.post(
            f"/api/screening/records/{second}",
            json={"decision": "exclude", "reason": "not caregivers"},
        )
        test_client.post(f"/api/screening/records/{third}", json={"decision": "maybe"})
        undo = test_client.post(f"/api/screening/records/{third}", json={"decision": None})
        unknown = test_client.post("/api/screening/records/999", json={"decision": "include"})
        too_long = test_client.post(
            f"/api/screening/records/{first}", json={"decision": "exclude", "reason": "x" * 501}
        )
        bad = test_client.post(f"/api/screening/records/{first}", json={"decision": "perhaps"})

    assert added.json()["added"] == {
        "search_number": 1,
        "identified": 6,
        "new_records": 6,
        "already_known": 0,
    }
    assert again.json()["added"]["already_known"] == 6
    assert include.json()["entry"]["decision"] == "include"
    assert include.json()["entry"]["sources"][0]["id_label"] in {"PMID", "OpenAlex"}
    assert undo.json()["entry"]["decision"] is None
    assert undo.json()["flow"]["pending"] == 4
    assert unknown.status_code == 409 and "no record 999" in unknown.json()["error"]
    assert (too_long.status_code, bad.status_code) == (422, 422)

    # Otro servidor con el mismo fichero retoma el cribado donde quedó.
    with make_client(screening=path) as test_client:
        state = test_client.get("/api/screening", params={"which": "exclude"}).json()
        everything = test_client.get("/api/screening", params={"which": "all"}).json()
        ris = test_client.get("/api/screening/export/ris")
        csv = test_client.get("/api/screening/export/csv", params={"which": "exclude"})
        logs = test_client.get("/api/screening/export/log")
        wrong = test_client.get("/api/screening", params={"which": "later"})
    flow = state["flow"]
    assert (flow["identified"], flow["duplicates"], flow["screened"]) == (12, 6, 6)
    assert (flow["included"], flow["excluded"], flow["maybe"], flow["pending"]) == (1, 1, 0, 4)
    assert flow["exclusion_reasons"] == [{"reason": "not caregivers", "count": 1}]
    assert [entry["id"] for entry in state["entries"]] == [second]
    assert state["entries"][0]["reason"] == "not caregivers"
    assert len(everything["entries"]) == 6
    assert ris.text.count("ER  - ") == 1
    assert 'filename="screening-include.ris"' in ris.headers["content-disposition"]
    assert csv.text.startswith("\ufefftitle,") and csv.text.count("\r\n") == 2
    assert logs.text.count("# Search log") == 2
    assert wrong.status_code == 422


def test_screening_needs_a_kept_search_and_a_file(tmp_path: Path) -> None:
    with make_client(screening=tmp_path / "s.sqlite") as test_client:
        missing = test_client.post("/api/screening/searches", json={"search_id": "nope"})
    with make_client() as test_client:
        search_id = _finished_search(test_client)
        off = test_client.post("/api/screening/searches", json={"search_id": search_id})
    assert missing.status_code == 404
    assert off.status_code == 409 and "without a screening file" in off.json()["error"]


def test_a_foreign_file_is_reported_as_a_screening_error(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"
    path.write_text("not a database\n" * 50, encoding="utf-8")
    with make_client(screening=path) as test_client:
        response = test_client.get("/api/screening")
    assert response.status_code == 409
    assert "not a biblio-search screening file" in response.json()["error"]


def test_openalex_errors_arrive_after_the_pubmed_results() -> None:
    broken = FakeOpenAlex(OpenAlexError("OpenAlex rejected the request (HTTP 400)"))
    with make_client(other=broken) as test_client:
        response = test_client.post("/api/search", json={"question": example_data()})
    names = [name for name, _ in sse_events(response.text)]
    assert names == ["selection", "equation", "count", "records", "equation", "error"]


def test_search_errors_arrive_as_events() -> None:
    with make_client(FakeNcbi(EutilsError("PubMed rejected the search"))) as test_client:
        response = test_client.post("/api/search", json={"question": example_data()})
    events = sse_events(response.text)
    assert events[-1] == ("error", {"message": "PubMed rejected the search"})
    assert "done" not in [name for name, _ in events]


def test_search_needs_a_searchable_database(client: TestClient) -> None:
    data = example_data()
    data["databases"] = ["eric"]

    events = sse_events(client.post("/api/search", json={"question": data}).text)

    assert [name for name, _ in events] == ["selection", "skipped", "error"]
    assert "choose OpenAlex or PubMed" in str(events[-1][1]["message"])


def test_search_reports_invalid_questions_as_events(client: TestClient) -> None:
    events = sse_events(client.post("/api/search", json={"question": {"format": "spc"}}).text)
    assert [name for name, _ in events] == ["error"]


def test_search_limits_the_number_of_records(client: TestClient) -> None:
    response = client.post("/api/search", json={"question": example_data(), "max_records": -1})
    assert response.status_code == 422


def test_bind_local_listens_only_on_loopback() -> None:
    sock = bind_local(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
        with pytest.raises(OSError):
            bind_local(port)
    finally:
        sock.close()


def test_cli_ui_opens_the_question_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_serve(config: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(web, "serve", fake_serve)

    assert main(["ui", str(CAREGIVERS), "--port", "8123", "--no-browser"]) == 0
    assert main(["ui"]) == 0

    first, second = calls
    assert first["port"] == 8123
    assert first["open_browser"] is False
    initial = first["initial_question"]
    assert isinstance(initial, dict)
    assert initial["format"] == "spc"
    assert second == {
        "port": 0,
        "open_browser": True,
        "initial_question": None,
        "screening_path": Path("screening.sqlite"),
    }


def test_cli_ui_reports_a_busy_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(config: object, **kwargs: object) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(web, "serve", busy)

    assert main(["ui", "--port", "8123"]) == 1
    assert "Address already in use" in capsys.readouterr().err


def test_cli_ui_rejects_ports_out_of_range(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["ui", "--port", "70000"])
    assert "between 0 and 65535" in capsys.readouterr().err


class FakeDecs:
    """Recorded DeCS answer: «Músculos» and its synonym lead to Muscles."""

    async def search_words(self, words: str) -> tuple[DecsDescriptor, ...]:
        if words.casefold() in ("músculos", "tejido muscular"):
            return parse_descriptors(load_fixture("decs_words_musculos.json"))
        return ()


def _spanish_question() -> dict[str, object]:
    return {
        "question": "q",
        "format": "concepts",
        "components": [
            {
                "description": "muscles",
                "conditions": [
                    {"label": "muscles", "controlled": ["Músculos", "Qwzx"], "free": ["muscle*"]}
                ],
            }
        ],
    }


def test_check_translates_spanish_headings_with_decs() -> None:
    with make_client(decs=FakeDecs()) as test_client:
        body = test_client.post("/api/check", json=_spanish_question()).json()
    (result,) = body["results"]
    statuses = {c["term"]: (c["status"], c["heading"]) for c in result["checks"]}
    assert statuses == {"Músculos": ("translated", "Muscles"), "Qwzx": ("not_found", None)}
    assert result["decs"] is True and result["decs_hint"] is None
    fixed = body["fixed"]["components"][0]["conditions"][0]
    assert fixed["controlled"] == ["Muscles"]
    assert fixed["free"] == ["Qwzx", "muscle*"]


def test_check_without_decs_says_how_to_get_it(client: TestClient) -> None:
    (result,) = client.post("/api/check", json=_spanish_question()).json()["results"]
    assert result["decs"] is False
    assert result["decs_hint"]["url"] == "https://decs.bvsalud.org/en/for-developers/"
    (example,) = client.post("/api/check", json=example_data()).json()["results"]
    assert example["decs_hint"] is None


def _eric_question() -> dict[str, object]:
    return {
        "question": "q",
        "format": "concepts",
        "components": [
            {
                "role": "concept",
                "description": "peer tutoring",
                "conditions": [
                    {
                        "label": "peer tutoring",
                        "controlled": [
                            {"term": "peer teaching", "vocabulary": "eric"},
                            {"term": "Peer Tutoring", "vocabulary": "eric"},
                            "Anxiety",
                        ],
                        "free": ["tutor*"],
                    }
                ],
            }
        ],
    }


def test_check_asks_mesh_and_eric_each_for_its_headings() -> None:
    with make_client(with_eric=True) as test_client:
        body = test_client.post("/api/check", json=_eric_question()).json()
    mesh, eric = body["results"]
    assert (mesh["vocabulary"], eric["vocabulary"]) == ("mesh", "eric")
    assert [c["term"] for c in mesh["checks"]] == ["Anxiety"]
    statuses = {c["term"]: (c["status"], c["heading"], c["identifier"]) for c in eric["checks"]}
    assert statuses == {
        "peer teaching": ("valid", "Peer Teaching", ""),
        "Peer Tutoring": ("not_found", None, None),
    }
    assert mesh["unchecked"] == eric["unchecked"] == []
    assert mesh["source"] is None and "through the ERIC API" in eric["source"]
    fixed = body["fixed"]["components"][0]["conditions"][0]
    assert fixed["controlled"] == [{"term": "Peer Teaching", "vocabulary": "eric"}, "Anxiety"]
    assert fixed["free"] == ["Peer Tutoring", "tutor*"]


def test_check_prefers_the_eric_thesaurus_file() -> None:
    thesaurus = EricThesaurusFile.load(FIXTURES / "eric_thesaurus_excerpt.xml")
    with make_client(eric_thesaurus=thesaurus) as test_client:
        body = test_client.post("/api/check", json=_eric_question()).json()
    eric = body["results"][1]
    tutoring = next(c for c in eric["checks"] if c["term"] == "Peer Tutoring")
    assert (tutoring["status"], tutoring["heading"]) == ("entry_term", "Peer Teaching")
    assert "Tutoring too" in tutoring["note"]
    assert eric["source"].endswith("ERIC Thesaurus file (7 terms)")


def test_check_leaves_eric_unchecked_without_a_way_to_ask() -> None:
    with make_client() as test_client:
        body = test_client.post("/api/check", json=_eric_question()).json()
    (mesh,) = body["results"]
    assert [t["text"] for t in mesh["unchecked"]] == ["peer teaching", "Peer Tutoring"]


def test_tune_streams_one_report_per_database(client: TestClient) -> None:
    response = client.post("/api/tune", json={"question": example_data(), "none_relevant": True})

    assert response.status_code == 200
    events = sse_events(response.text)
    assert [name for name, _ in events] == ["tuning", "report", "tuning", "report", "done"]
    pubmed = events[1][1]
    assert (pubmed["database"], pubmed["label"], pubmed["total"]) == ("pubmed", "PubMed", 2808)
    conditions = pubmed["conditions"]
    assert isinstance(conditions, list) and len(conditions) == 4
    variants = pubmed["variants"]
    assert isinstance(variants, list)
    repeat = next(v for v in variants if v["key"] == "repeat")
    assert repeat["direction"] == "sensitive"
    assert repeat["question"]["options"]["repeat_controlled_as_free"] is True
    advice = pubmed["advice"]
    assert isinstance(advice, list) and advice[0]["kind"] == "too_sensitive"
    openalex_report = events[3][1]
    assert openalex_report["database"] == "openalex" and openalex_report["variants"] == []


def test_tune_reports_errors_as_events() -> None:
    with make_client(FakeNcbi(search_error=EutilsError("NCBI is down"))) as test_client:
        data = {**example_data(), "databases": ["pubmed"]}
        response = test_client.post("/api/tune", json={"question": data})
    events = sse_events(response.text)
    assert events[-1] == ("error", {"message": "NCBI is down"})


class RecordedOllama:
    """The recorded qwen3:8b answers for the fibromyalgia question."""

    model = "qwen3:8b"

    def __init__(self, error: AssistantError | None = None) -> None:
        self.error = error
        self.schemas: list[Mapping[str, object]] = []

    async def complete_json(
        self, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self.error is not None:
            raise self.error
        self.schemas.append(schema)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        name = "classify" if "discipline" in properties else "draft"
        message = load_fixture(f"ollama_{name}_fibromyalgia.json")["message"]
        assert isinstance(message, dict)
        answer: Mapping[str, object] = json.loads(message["content"])
        return answer


FIBROMYALGIA = (
    "¿El ejercicio en el agua mejora el dolor en la fibromialgia comparado con el ejercicio "
    "en tierra?"
)


def test_draft_fills_the_form_and_checks_the_headings() -> None:
    ollama = RecordedOllama()
    with make_client(assistant=ollama) as client:
        response = client.post("/api/draft", json={"text": FIBROMYALGIA, "discipline": "health"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "qwen3:8b"
    assert body["english"].startswith("Does water exercise improve pain")
    question = body["question"]
    assert question["question"] == FIBROMYALGIA
    assert (question["format"], question["discipline"]) == ("pico", "health")
    assert any("comparison" in note for note in body["notes"])
    (check,) = body["check"]["results"]
    statuses = {c["term"]: c["status"] for c in check["checks"]}
    assert statuses == {"Fibromyalgia": "not_found", "Exercise": "not_found", "Pain": "not_found"}
    assert check["decs_hint"] is None
    # La disciplina viene elegida pero el formato no: el modelo clasifica igualmente.
    assert len(ollama.schemas) == 2


def test_draft_errors_are_readable() -> None:
    with make_client() as client:
        response = client.post("/api/draft", json={"text": FIBROMYALGIA})
    assert response.status_code == 503
    assert "without the local assistant" in response.json()["error"]

    down = RecordedOllama(AssistantError("Ollama is not running at http://127.0.0.1:11434"))
    with make_client(assistant=down) as client:
        response = client.post("/api/draft", json={"text": FIBROMYALGIA})
        assert response.status_code == 503
        assert "not running" in response.json()["error"]
        assert client.post("/api/draft", json={"text": "  "}).status_code == 422
        assert client.post("/api/draft", json={"text": "x" * 1001}).status_code == 422
