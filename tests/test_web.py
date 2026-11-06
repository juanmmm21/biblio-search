from __future__ import annotations

import json
import tomllib
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import load_fixture, mesh_fixture_for
from fastapi.testclient import TestClient

import biblio_search.web as web
from biblio_search.__main__ import main
from biblio_search.eutils import EutilsError, parse_search, parse_summaries
from biblio_search.models import ArticleSummary, SearchResult
from biblio_search.web.app import NcbiClient, create_app
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


def make_client(
    fake: FakeNcbi | None = None, initial: Mapping[str, object] | None = None
) -> TestClient:
    ncbi = fake or FakeNcbi()

    @asynccontextmanager
    async def open_client() -> AsyncIterator[NcbiClient]:
        yield ncbi

    return TestClient(create_app(open_client, initial), base_url=BASE_URL)


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
        ("openalex", False),
    ]
    [equation] = preview["equations"]
    assert equation["database"] == "pubmed"
    assert equation["equation"].startswith("(Anxiety[mj:noexp] OR Depression[mj]")
    assert equation["note"] is None


def test_preview_notes_headings_searched_as_free_text(client: TestClient) -> None:
    data = example_data("peer-tutoring-reading.toml")
    data["databases"] = ["pubmed"]

    preview = client.post("/api/preview", json=data).json()

    assert preview["selection"]["mode"] == "manual"
    [equation] = preview["equations"]
    assert "Peer Teaching (ERIC)" in equation["note"]


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
    result = response.json()
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
    fixed = result["fixed"]["components"][1]["conditions"][0]
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
    with make_client(fake) as test_client:
        response = test_client.post(
            "/api/search", json={"question": example_data(), "max_records": 3}
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert [name for name, _ in events] == [
        "selection",
        "skipped",
        "equation",
        "count",
        "records",
        "done",
    ]
    data = dict(events)
    assert data["skipped"] == {"database": "openalex", "label": "OpenAlex"}
    assert data["equation"]["equation"] == fake.searched[0][0]
    assert fake.searched[0][1] == 3
    assert data["count"]["count"] == 2808
    assert data["count"]["translation"]
    records = data["records"]["records"]
    assert isinstance(records, list)
    assert len(records) == 3
    assert records[0]["url"] == f"https://pubmed.ncbi.nlm.nih.gov/{records[0]['id']}/"


def test_search_errors_arrive_as_events() -> None:
    with make_client(FakeNcbi(EutilsError("PubMed rejected the search"))) as test_client:
        response = test_client.post("/api/search", json={"question": example_data()})
    events = sse_events(response.text)
    assert events[-1] == ("error", {"message": "PubMed rejected the search"})
    assert "done" not in [name for name, _ in events]


def test_search_needs_a_searchable_database(client: TestClient) -> None:
    data = example_data()
    data["databases"] = ["openalex"]

    events = sse_events(client.post("/api/search", json={"question": data}).text)

    assert [name for name, _ in events] == ["selection", "skipped", "error"]


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
    assert second == {"port": 0, "open_browser": True, "initial_question": None}


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
