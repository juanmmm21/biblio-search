from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from biblio_search.eric import (
    PAGE_SIZE,
    EricClient,
    EricConfig,
    EricError,
    parse_page,
    parse_record,
)
from biblio_search.models import ArticleSummary, DatabaseError, SearchResult
from biblio_search.ratelimit import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_IDS = ("EJ888659", "EJ1257338", "ED395269")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _page(*ids: str, total: int | None = None) -> httpx.Response:
    docs = [{"id": i, "title": f"Paper {i}"} for i in ids]
    count = len(ids) if total is None else total
    return httpx.Response(200, json={"response": {"numFound": count, "start": 0, "docs": docs}})


class _Recorder:
    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)


async def _no_sleep(seconds: float) -> None:
    return None


def _client(recorder: _Recorder, sleeps: list[float] | None = None) -> EricClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return EricClient(
        EricConfig(),
        transport=httpx.MockTransport(recorder),
        limiter=RateLimiter(1000, sleep=_no_sleep),
        sleep=sleep,
    )


def test_parse_page_fixture() -> None:
    page = parse_page(_fixture("eric_search_peer_tutoring.json"))
    assert page.count == 160
    records = [parse_record(doc) for doc in page.docs]
    assert tuple(r.record_id for r in records) == EXAMPLE_IDS
    article, unescaped, report = records
    assert article.title.startswith("The Impact of Cross-Age Peer Tutoring on Third")
    assert article.authors == ("Van Keer, Hilde", "Vanderlinde, Ruben")
    assert article.journal == "Middle Grades Research Journal"
    assert article.year == 2010
    assert article.publication_types == ("Journal Articles", "Reports - Research")
    assert article.url == "https://eric.ed.gov/?id=EJ888659"
    assert article.doi is None and article.pmid is None
    # ERIC deja «&apos;» en el título tal cual.
    assert "Children's e-Book" in unescaped.title
    assert report.journal == "" and report.year == 1995


def test_parse_page_rejects_solr_errors_and_garbage() -> None:
    with pytest.raises(EricError, match=r"ERIC rejected the query: Cannot parse 'title:\(peer'"):
        parse_page(_fixture("eric_error_syntax.json"))
    with pytest.raises(EricError, match="valid JSON"):
        parse_page(b"<html>")
    with pytest.raises(EricError, match="no results"):
        parse_page(b"{}")
    with pytest.raises(EricError, match="no count"):
        parse_page(b'{"response": {"docs": []}}')


def test_parse_record_needs_an_eric_number() -> None:
    with pytest.raises(EricError, match="no ERIC number"):
        parse_record({"title": "x"})


def test_search_returns_ids_and_remembers_records() -> None:
    recorder = _Recorder(
        lambda request: httpx.Response(200, content=_fixture("eric_search_peer_tutoring.json"))
    )

    async def run() -> tuple[SearchResult, tuple[ArticleSummary, ...]]:
        async with _client(recorder) as client:
            result = await client.search("title:x", retmax=3)
            records = await client.summaries([EXAMPLE_IDS[2], EXAMPLE_IDS[0]])
            return result, records

    result, records = asyncio.run(run())
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.url.host == "api.ies.ed.gov" and request.url.path == "/eric/"
    params = request.url.params
    assert params["search"] == "title:x"
    assert (params["start"], params["rows"], params["format"]) == ("0", "3", "json")
    assert params["fields"] == "id,title,author,source,publicationdateyear,publicationtype"
    assert result == SearchResult(count=160, ids=EXAMPLE_IDS, query_translation="")
    assert [r.record_id for r in records] == [EXAMPLE_IDS[2], EXAMPLE_IDS[0]]


def test_count_only_asks_for_no_rows() -> None:
    recorder = _Recorder(lambda request: _page(total=42))

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("title:x", retmax=0)

    assert asyncio.run(run()) == SearchResult(count=42, ids=(), query_translation="")
    assert recorder.requests[0].url.params["rows"] == "0"


def test_search_pages_until_retmax() -> None:
    ids = [f"EJ{n}" for n in range(PAGE_SIZE + 2)]

    def answer(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        rows = int(request.url.params["rows"])
        return _page(*ids[start : start + rows], total=len(ids))

    recorder = _Recorder(answer)

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("title:x", retmax=PAGE_SIZE + 1)

    result = asyncio.run(run())
    assert result.ids == tuple(ids[: PAGE_SIZE + 1])
    starts = [(r.url.params["start"], r.url.params["rows"]) for r in recorder.requests]
    assert starts == [("0", str(PAGE_SIZE)), (str(PAGE_SIZE), "1")]


def test_summaries_fetch_unseen_ids_in_the_order_asked() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, content=_fixture("eric_id_list.json")))

    async def run() -> tuple[ArticleSummary, ...]:
        async with _client(recorder) as client:
            return await client.summaries(list(EXAMPLE_IDS))

    records = asyncio.run(run())
    assert tuple(r.record_id for r in records) == EXAMPLE_IDS
    assert recorder.requests[0].url.params["search"] == "id:(EJ888659 OR EJ1257338 OR ED395269)"


def test_summaries_reject_bad_ids_and_report_missing_ones() -> None:
    recorder = _Recorder(lambda request: _page())

    async def run(ids: list[str]) -> tuple[ArticleSummary, ...]:
        async with _client(recorder) as client:
            return await client.summaries(ids)

    with pytest.raises(ValueError, match="invalid ERIC numbers: W1"):
        asyncio.run(run(["W1"]))
    with pytest.raises(EricError, match="no record for EJ1"):
        asyncio.run(run(["EJ1"]))


def test_retries_gateway_errors_then_succeeds() -> None:
    answers = iter(
        [
            httpx.Response(504, json={"message": "Endpoint request timed out"}),
            httpx.Response(200, json={"message": "Network error communicating with endpoint"}),
            _page("EJ1"),
        ]
    )
    sleeps: list[float] = []
    recorder = _Recorder(lambda request: next(answers))

    async def run() -> SearchResult:
        async with _client(recorder, sleeps) as client:
            return await client.search("title:x", retmax=1)

    assert asyncio.run(run()).ids == ("EJ1",)
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_the_retries() -> None:
    recorder = _Recorder(lambda request: httpx.Response(502, json={"message": "Bad gateway"}))

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("title:x")

    with pytest.raises(DatabaseError, match="after 4 attempts: HTTP 502, Bad gateway"):
        asyncio.run(run())


def test_client_errors_are_not_retried() -> None:
    recorder = _Recorder(
        lambda request: httpx.Response(403, json={"message": "Missing Authentication Token"})
    )

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("title:x")

    with pytest.raises(EricError, match=r"\(HTTP 403\): Missing Authentication Token"):
        asyncio.run(run())
    assert len(recorder.requests) == 1


def test_network_errors_are_retried() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async def run() -> SearchResult:
        async with _client(_Recorder(answer)) as client:
            return await client.search("title:x")

    with pytest.raises(EricError, match="network error: ConnectError"):
        asyncio.run(run())


def test_rejects_bad_arguments() -> None:
    async def run(term: str, retmax: int) -> SearchResult:
        async with _client(_Recorder(lambda request: _page())) as client:
            return await client.search(term, retmax=retmax)

    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run("  ", 1))
    with pytest.raises(ValueError, match="negative"):
        asyncio.run(run("x", -1))
    with pytest.raises(ValueError, match="max_retries"):
        EricConfig(max_retries=-1)
