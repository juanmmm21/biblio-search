from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from conftest import load_fixture

from biblio_search.eutils import (
    EutilsClient,
    EutilsConfig,
    EutilsError,
    parse_search,
    parse_summaries,
)
from biblio_search.ratelimit import RateLimiter


def test_parse_search_fixture() -> None:
    result = parse_search(load_fixture("esearch_caregiver.json"))
    assert result.count == 2808
    assert result.ids == ("42970524", "42956697", "42919807")
    assert result.query_translation.startswith('("anxiety"[MeSH Major Topic:noexp]')
    assert result.not_found == ()
    assert result.warnings == ()


def test_parse_search_reports_unknown_phrases() -> None:
    result = parse_search(load_fixture("esearch_not_found.json"))
    assert result.count == 0
    assert result.not_found == ("qwzxvplorkt",)
    assert result.warnings == ("No items found.",)


def test_parse_search_surfaces_dropped_syntax() -> None:
    result = parse_search(load_fixture("esearch_syntax.json"))
    assert result.query_translation == '"anxiety"[Title/Abstract]'
    assert result.warnings == ("(",)


def test_parse_search_raises_on_error_field() -> None:
    with pytest.raises(EutilsError, match="rejected"):
        parse_search({"esearchresult": {"ERROR": "Invalid query"}})
    with pytest.raises(EutilsError, match="not a number"):
        parse_search({"esearchresult": {"count": "many"}})
    with pytest.raises(EutilsError, match="esearchresult"):
        parse_search({})


def test_parse_summaries_fixture() -> None:
    records = parse_summaries(load_fixture("esummary_caregiver.json"))
    assert [r.pmid for r in records] == ["42970524", "42956697", "42919807"]
    first = records[0]
    assert first.title == "Music-Based Interventions for People Living With Dementia."
    assert first.authors == ("Mandolang E", "Brush P", "Petrovsky DV")
    assert first.journal == "Home healthcare now"
    assert first.year == 2026
    assert first.doi == "10.1097/NHH.0000000000001471"
    assert first.publication_types == ("Journal Article", "Review")


def test_parse_summaries_raises_on_missing_record() -> None:
    payload = {"result": {"uids": ["1"], "1": {"uid": "1", "error": "cannot get document summary"}}}
    with pytest.raises(EutilsError, match="PMID 1"):
        parse_summaries(payload)


def test_config_from_env_and_rate() -> None:
    config = EutilsConfig.from_env({"NCBI_EMAIL": "a@b.org", "NCBI_API_KEY": ""})
    assert config.email == "a@b.org"
    assert config.api_key is None
    assert config.requests_per_second == 3
    assert EutilsConfig(api_key="k").requests_per_second == 10


@pytest.mark.parametrize(
    "kwargs",
    [{"tool": "my tool"}, {"email": ""}, {"max_retries": -1}, {"timeout_seconds": 0}],
)
def test_config_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EutilsConfig(**kwargs)  # type: ignore[arg-type]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self, config: EutilsConfig | None = None) -> EutilsClient:
        async def no_wait(_: float) -> None:
            return None

        return EutilsClient(
            config or EutilsConfig(email="me@example.org"),
            transport=httpx.MockTransport(self.handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=self.sleep,
        )


def _ok(fixture: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(fixture))


def test_search_sends_identity_and_parses() -> None:
    recorder = Recorder(_ok("esearch_caregiver.json"))

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("caregiv*[tiab]", retmax=3)
        assert result.count == 2808

    asyncio.run(scenario())
    params = recorder.requests[0].url.params
    assert recorder.requests[0].url.path.endswith("/esearch.fcgi")
    assert params["db"] == "pubmed"
    assert params["term"] == "caregiv*[tiab]"
    assert params["retmax"] == "3"
    assert params["retmode"] == "json"
    assert params["tool"] == "biblio-search"
    assert params["email"] == "me@example.org"
    assert "api_key" not in params


def test_api_key_is_sent_when_configured() -> None:
    recorder = Recorder(_ok("esearch_caregiver.json"))

    async def scenario() -> None:
        async with recorder.client(EutilsConfig(api_key="secret")) as client:
            await client.search("x")

    asyncio.run(scenario())
    assert recorder.requests[0].url.params["api_key"] == "secret"
    assert "email" not in recorder.requests[0].url.params


def test_retries_on_rate_limit_and_server_errors() -> None:
    recorder = Recorder(
        httpx.Response(
            429, headers={"Retry-After": "2"}, json={"error": "API rate limit exceeded"}
        ),
        httpx.ConnectError("boom"),
        httpx.Response(200, json={"error": "API rate limit exceeded", "count": "4"}),
        _ok("esearch_caregiver.json"),
    )

    async def scenario() -> None:
        async with recorder.client() as client:
            assert (await client.search("x")).count == 2808

    asyncio.run(scenario())
    assert len(recorder.requests) == 4
    assert recorder.sleeps == [2.0, 2.0, 4.0]


def test_gives_up_after_max_retries() -> None:
    recorder = Recorder(*(httpx.Response(503) for _ in range(2)))

    async def scenario() -> None:
        async with recorder.client(EutilsConfig(max_retries=1)) as client:
            await client.search("x")

    with pytest.raises(EutilsError, match="after 2 attempts: HTTP 503"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(400, text="bad"), "HTTP 400"),
        (httpx.Response(200, text="<html>"), "did not return JSON"),
        (httpx.Response(200, json=["x"]), "not an object"),
        (httpx.Response(200, json={"error": "Invalid db"}), "Invalid db"),
    ],
)
def test_non_retriable_errors(response: httpx.Response, message: str) -> None:
    recorder = Recorder(response)

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.search("x")

    with pytest.raises(EutilsError, match=message):
        asyncio.run(scenario())
    assert len(recorder.requests) == 1


def test_summaries_are_batched() -> None:
    payload = load_fixture("esummary_caregiver.json")
    recorder = Recorder(*(httpx.Response(200, json=payload) for _ in range(2)))
    pmids = [str(n) for n in range(1, 251)]

    async def scenario() -> None:
        async with recorder.client() as client:
            records = await client.summaries(pmids)
        assert len(records) == 6

    asyncio.run(scenario())
    sent = [r.url.params["id"].split(",") for r in recorder.requests]
    assert [len(batch) for batch in sent] == [200, 50]
    assert recorder.requests[0].url.path.endswith("/esummary.fcgi")


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda c: c.search(" "), "empty"),
        (lambda c: c.search("x", retmax=10_001), "retmax"),
        (lambda c: c.search("x", retstart=-1), "retstart"),
        (lambda c: c.summaries(["12", "PMC1"]), "PMC1"),
    ],
)
def test_input_validation(call: Callable[[EutilsClient], Awaitable[object]], message: str) -> None:
    recorder = Recorder()

    async def scenario() -> None:
        async with recorder.client() as client:
            await call(client)

    with pytest.raises(ValueError, match=message):
        asyncio.run(scenario())
    assert recorder.requests == []
