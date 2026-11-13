from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from biblio_search.arxiv import (
    PAGE_SIZE,
    ArxivClient,
    ArxivConfig,
    ArxivError,
    parse_page,
    short_id,
)
from biblio_search.merge import merge_records
from biblio_search.models import ArticleSummary, Database, DatabaseError, SearchResult
from biblio_search.ratelimit import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"
QEC_IDS = ("2304.07362", "2307.09463", "2310.05900")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _feed(*ids: str, total: int | None = None) -> bytes:
    """A minimal Atom feed with one entry per id, like arXiv's."""
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}v1</id><title>Paper {i}</title>"
        "<published>2024-01-02T00:00:00Z</published></entry>"
        for i in ids
    )
    count = len(ids) if total is None else total
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        "<title>arXiv Query: search_query=ti:x&amp;id_list=&amp;start=0</title>"
        f"<opensearch:totalResults>{count}</opensearch:totalResults>{entries}</feed>"
    ).encode()


class _Recorder:
    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)


async def _no_sleep(seconds: float) -> None:
    return None


def _client(recorder: _Recorder, sleeps: list[float] | None = None) -> ArxivClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return ArxivClient(
        ArxivConfig(),
        transport=httpx.MockTransport(recorder),
        limiter=RateLimiter(1000, sleep=_no_sleep),
        sleep=sleep,
    )


def test_short_id_drops_url_and_version() -> None:
    assert short_id("http://arxiv.org/abs/2301.01234v2") == "2301.01234"
    assert short_id("http://arxiv.org/abs/hep-th/9711200v3") == "hep-th/9711200"


def test_parse_page_fixture() -> None:
    page = parse_page(_fixture("arxiv_search_qec.xml"))
    assert page.count == 115
    assert tuple(r.record_id for r in page.records) == QEC_IDS
    assert page.query.startswith('(ti:"neural network" OR abs:"neural network" OR ')
    preprint, _, published = page.records
    assert preprint.title == "The END: An Equivariant Neural Decoder for Quantum Error Correction"
    assert preprint.authors == ("Evgenii Egorov", "Roberto Bondesan", "Max Welling")
    assert preprint.year == 2023
    assert preprint.publication_types == ("preprint",)
    assert preprint.doi == "10.48550/arXiv.2304.07362" and preprint.journal == ""
    assert preprint.url == "https://arxiv.org/abs/2304.07362"
    assert published.journal == "Nature 635, 834-840 (2024)"
    assert published.doi == "10.1038/s41586-024-08148-8"
    assert published.publication_types == ("article",)


def test_parse_page_empty_search() -> None:
    page = parse_page(_fixture("arxiv_search_empty.xml"))
    assert page.count == 0 and page.records == ()


def test_parse_page_id_list_keeps_old_style_ids() -> None:
    page = parse_page(_fixture("arxiv_id_list.xml"))
    # arXiv no garantiza el orden de id_list (esta respuesta lo trae al revés).
    assert {r.record_id for r in page.records} == {"1706.03762", "hep-th/9711200", "2007.05129"}


def test_parse_page_rejects_error_feeds_and_garbage() -> None:
    with pytest.raises(ArxivError, match="arXiv rejected the query: Invalid query"):
        parse_page(_fixture("arxiv_error_syntax.xml"))
    with pytest.raises(ArxivError, match="valid Atom feed"):
        parse_page(b"<html>")
    with pytest.raises(ArxivError, match="totalResults is not a number"):
        parse_page(b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')


def test_preprint_merges_with_its_openalex_twin_by_the_arxiv_doi() -> None:
    preprint = parse_page(_fixture("arxiv_search_qec.xml")).records[0]
    # Así aparece en OpenAlex (W4366341245), con el DOI en minúsculas.
    twin = replace(preprint, record_id="W4366341245", doi="10.48550/arxiv.2304.07362")
    merged = merge_records([(Database.ARXIV, [preprint]), (Database.OPENALEX, [twin])])
    assert merged.duplicates == 1


def test_search_returns_ids_and_remembers_records() -> None:
    recorder = _Recorder(
        lambda request: httpx.Response(200, content=_fixture("arxiv_search_qec.xml"))
    )

    async def run() -> tuple[SearchResult, tuple[ArticleSummary, ...]]:
        async with _client(recorder) as client:
            result = await client.search("ti:x", retmax=3)
            records = await client.summaries([QEC_IDS[2], QEC_IDS[0]])
            return result, records

    result, records = asyncio.run(run())
    assert len(recorder.requests) == 1
    params = recorder.requests[0].url.params
    assert params["search_query"] == "ti:x"
    assert (params["start"], params["max_results"]) == ("0", "3")
    assert recorder.requests[0].url.host == "export.arxiv.org"
    assert result.count == 115 and result.ids == QEC_IDS
    assert [r.record_id for r in records] == [QEC_IDS[2], QEC_IDS[0]]


def test_count_only_asks_for_one_record() -> None:
    # max_results=0 hace que arXiv conteste con un error 500.
    recorder = _Recorder(lambda request: httpx.Response(200, content=_feed("1", total=42)))

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("ti:x", retmax=0)

    result = asyncio.run(run())
    assert recorder.requests[0].url.params["max_results"] == "1"
    assert result.count == 42 and result.ids == ()


def test_search_pages_until_retmax_or_the_end() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        size = int(request.url.params["max_results"])
        ids = [f"2401.{n:05d}" for n in range(start, min(start + size, PAGE_SIZE + 10))]
        return httpx.Response(200, content=_feed(*ids, total=PAGE_SIZE + 10))

    recorder = _Recorder(answer)

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("ti:x", retmax=PAGE_SIZE + 50)

    result = asyncio.run(run())
    starts = [(r.url.params["start"], r.url.params["max_results"]) for r in recorder.requests]
    assert starts == [("0", str(PAGE_SIZE)), (str(PAGE_SIZE), "50")]
    assert len(result.ids) == PAGE_SIZE + 10


def test_summaries_fetch_unseen_ids_with_id_list() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, content=_fixture("arxiv_id_list.xml")))

    async def run() -> tuple[ArticleSummary, ...]:
        async with _client(recorder) as client:
            return await client.summaries(["2007.05129", "1706.03762", "hep-th/9711200"])

    records = asyncio.run(run())
    params = recorder.requests[0].url.params
    assert params["id_list"] == "2007.05129,1706.03762,hep-th/9711200"
    assert params["max_results"] == "3"
    ids = [r.record_id for r in records]
    assert ids == ["2007.05129", "1706.03762", "hep-th/9711200"]


def test_summaries_validate_ids_and_report_missing_records() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, content=_feed()))

    async def run(ids: list[str]) -> tuple[ArticleSummary, ...]:
        async with _client(recorder) as client:
            return await client.summaries(ids)

    with pytest.raises(ValueError, match=r"invalid arXiv ids: 2301\.01234v2"):
        asyncio.run(run(["2301.01234v2"]))
    with pytest.raises(ArxivError, match=r"no record for 2301\.01234"):
        asyncio.run(run(["2301.01234"]))


def test_syntax_error_is_not_retried() -> None:
    error = _fixture("arxiv_error_syntax.xml")
    recorder = _Recorder(lambda request: httpx.Response(400, content=error))

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("(ti:qubit")

    with pytest.raises(DatabaseError, match=r"HTTP 400\): Invalid query string"):
        asyncio.run(run())
    assert len(recorder.requests) == 1


def test_rate_limit_is_retried_after_the_delay_arxiv_asks_for() -> None:
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, content=_feed("1")),
        ]
    )
    recorder = _Recorder(lambda request: next(answers))
    sleeps: list[float] = []

    async def run() -> SearchResult:
        async with _client(recorder, sleeps) as client:
            return await client.search("ti:x", retmax=1)

    assert asyncio.run(run()).count == 1
    assert sleeps == [7.0, 6.0]


def test_network_errors_give_up_after_the_retries() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    recorder = _Recorder(fail)

    async def run() -> SearchResult:
        async with _client(recorder) as client:
            return await client.search("ti:x")

    with pytest.raises(ArxivError, match="failed after 4 attempts: network error: ConnectError"):
        asyncio.run(run())


def test_search_validates_its_arguments() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, content=_feed()))

    async def run(term: str, retmax: int, retstart: int = 0) -> SearchResult:
        async with _client(recorder) as client:
            return await client.search(term, retmax=retmax, retstart=retstart)

    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run(" ", 1))
    with pytest.raises(ValueError, match="negative"):
        asyncio.run(run("x", -1))
    with pytest.raises(ValueError, match="first 30000"):
        asyncio.run(run("x", 10, 29_995))
    assert recorder.requests == []


def test_default_pace_is_one_request_every_three_seconds() -> None:
    client = ArxivClient(ArxivConfig())
    try:
        assert client._limiter.interval == pytest.approx(3.0)
    finally:
        asyncio.run(client.aclose())


def test_config_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ArxivConfig(max_retries=-1)
    with pytest.raises(ValueError, match="timeout"):
        ArxivConfig(timeout_seconds=0)
