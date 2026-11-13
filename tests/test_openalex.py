from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

import httpx
import pytest
from conftest import load_fixture

from biblio_search.models import DatabaseError
from biblio_search.openalex import (
    SEARCH_PARAM,
    OpenAlexClient,
    OpenAlexConfig,
    OpenAlexError,
    parse_links,
    parse_search,
    parse_work,
)
from biblio_search.ratelimit import RateLimiter

FIRST_IDS = ("W2145256182", "W4391296183", "W1923367678")


def test_parse_search_fixture() -> None:
    result = parse_search(load_fixture("openalex_search_caregiver.json"))
    assert result.count == 2516
    assert len(result.ids) == 100
    assert result.ids[:3] == FIRST_IDS
    assert result.query_translation.startswith('get works with (("older people" OR "aged"')
    assert result.not_found == result.warnings == ()


def test_parse_search_rejects_unexpected_payloads() -> None:
    with pytest.raises(OpenAlexError, match="meta"):
        parse_search({})
    with pytest.raises(OpenAlexError, match="not a number"):
        parse_search({"meta": {"count": "many"}})
    result = parse_search({"meta": {"count": 0}, "results": []})
    assert result.ids == () and result.query_translation == ""


def test_parse_work_fixture() -> None:
    works = load_fixture("openalex_works_caregiver.json")["results"]
    assert isinstance(works, list)
    by_id = {record.record_id: record for record in map(parse_work, works)}
    record = by_id["W4391296183"]
    assert record.title.startswith("Coping and anxiety symptoms in family carers")
    assert record.journal == "Journal of Nursing Scholarship"
    assert record.year == 2024
    assert record.doi == "10.1111/jnu.12957"
    assert record.publication_types == ("article",)
    assert record.url == "https://openalex.org/W4391296183"
    assert record.pmid == "38282022"
    assert len(record.authors) == 4


def test_parse_work_tolerates_missing_fields() -> None:
    record = parse_work(
        {"id": "https://openalex.org/W1", "display_name": None, "primary_location": None}
    )
    assert (record.title, record.journal, record.year, record.doi) == ("", "", None, None)
    assert record.pmid is None


def test_parse_work_ignores_a_pmid_that_is_not_a_number() -> None:
    record = parse_work({"id": "https://openalex.org/W1", "ids": {"pmid": "https://x.org/abc"}})
    assert record.pmid is None
    assert record.authors == record.publication_types == ()
    with pytest.raises(OpenAlexError, match="no id"):
        parse_work({"display_name": "x"})


def test_config_from_env_and_rate() -> None:
    assert OpenAlexConfig.from_env({}).api_key is None
    assert OpenAlexConfig.from_env({"OPENALEX_API_KEY": ""}).api_key is None
    keyed = OpenAlexConfig.from_env({"OPENALEX_API_KEY": "k"})
    assert keyed.api_key == "k"
    assert keyed.requests_per_second == 5
    assert OpenAlexConfig().requests_per_second == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"api_key": ""}, {"api_key": "a b"}, {"max_retries": -1}, {"timeout_seconds": 0}],
)
def test_config_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OpenAlexConfig(**kwargs)  # type: ignore[arg-type]


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

    def client(self, config: OpenAlexConfig | None = None) -> OpenAlexClient:
        async def no_wait(_: float) -> None:
            return None

        return OpenAlexClient(
            config or OpenAlexConfig(),
            transport=httpx.MockTransport(self.handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=self.sleep,
        )


def _ok(payload: Mapping[str, object] | str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(payload) if isinstance(payload, str) else payload)


def _page(start: int, size: int, count: int = 250) -> dict[str, object]:
    ids = [{"id": f"https://openalex.org/W{n}"} for n in range(start, start + size)]
    return {"meta": {"count": count}, "results": ids}


def test_search_sends_the_exact_title_abstract_search() -> None:
    recorder = Recorder(_ok("openalex_search_caregiver.json"))

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("(caregiv*) AND burden*", retmax=3)
        assert result.count == 2516
        assert result.ids == FIRST_IDS

    asyncio.run(scenario())
    request = recorder.requests[0]
    assert request.url.path == "/works"
    assert request.url.params[SEARCH_PARAM] == "(caregiv*) AND burden*"
    assert request.url.params["select"] == "id"
    assert request.url.params["per_page"] == "100"
    assert request.url.params["page"] == "1"
    assert "authorization" not in request.headers
    assert request.headers["user-agent"].startswith("biblio-search")


def test_api_key_goes_in_the_header_not_the_url() -> None:
    recorder = Recorder(_ok("openalex_search_caregiver.json"))

    async def scenario() -> None:
        async with recorder.client(OpenAlexConfig(api_key="secret")) as client:
            await client.search("x")

    asyncio.run(scenario())
    assert recorder.requests[0].headers["authorization"] == "Bearer secret"
    assert "secret" not in str(recorder.requests[0].url)


def test_search_pages_through_results() -> None:
    recorder = Recorder(_ok(_page(1, 100)), _ok(_page(101, 100)), _ok(_page(201, 50)))

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("x", retmax=230, retstart=10)
        assert result.ids == tuple(f"W{n}" for n in range(11, 241))

    asyncio.run(scenario())
    assert [r.url.params["page"] for r in recorder.requests] == ["1", "2", "3"]


def test_search_stops_when_results_run_out() -> None:
    recorder = Recorder(_ok(_page(1, 40, count=40)))

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("x", retmax=500)
        assert len(result.ids) == 40

    asyncio.run(scenario())
    assert len(recorder.requests) == 1


def test_count_only_asks_for_a_minimal_page() -> None:
    recorder = Recorder(_ok(_page(1, 1)))

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("x", retmax=0)
        assert result.count == 250 and result.ids == ()

    asyncio.run(scenario())
    assert recorder.requests[0].url.params["per_page"] == "1"


def test_summaries_keep_the_search_order() -> None:
    recorder = Recorder(_ok("openalex_works_caregiver.json"))
    wanted = (FIRST_IDS[2], FIRST_IDS[0], FIRST_IDS[1])

    async def scenario() -> None:
        async with recorder.client() as client:
            records = await client.summaries(wanted)
        assert tuple(r.record_id for r in records) == wanted

    asyncio.run(scenario())
    params = recorder.requests[0].url.params
    assert params["filter"] == "ids.openalex:" + "|".join(wanted)
    assert "display_name" in params["select"]


def test_summaries_are_batched_and_complain_about_missing_works() -> None:
    def works(start: int, size: int) -> dict[str, object]:
        ids = range(start, start + size)
        return {"results": [{"id": f"https://openalex.org/W{n}"} for n in ids]}

    recorder = Recorder(_ok(works(1, 100)), _ok(works(101, 49)))

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.summaries([f"W{n}" for n in range(1, 151)])

    with pytest.raises(OpenAlexError, match="no record for W150"):
        asyncio.run(scenario())
    assert len(recorder.requests) == 2


ZARIT = "W2130900065"


def test_parse_links_fixture() -> None:
    (work,) = load_fixture("openalex_links_zarit.json")["results"]  # type: ignore[misc]
    links = parse_links(work)
    assert links.work_id == ZARIT
    assert links.doi == "10.1093/geront/20.6.649"
    assert links.pmid == "7203086"
    assert len(links.referenced) == 25
    assert links.referenced[0] == "W229834578"
    with pytest.raises(OpenAlexError, match="no id"):
        parse_links({"referenced_works": []})


def test_links_ask_by_each_kind_of_identifier() -> None:
    recorder = Recorder(
        _ok({"results": [{"id": "https://openalex.org/W5", "referenced_works": None}]}),
        _ok("openalex_links_zarit.json"),
        _ok({"results": []}),
    )

    async def scenario() -> None:
        async with recorder.client() as client:
            works = await client.links(
                work_ids=["W5"], dois=["10.1093/geront/20.6.649"], pmids=["1", "2"]
            )
        assert [w.work_id for w in works] == ["W5", ZARIT]
        assert works[0].referenced == ()

    asyncio.run(scenario())
    filters = [r.url.params["filter"] for r in recorder.requests]
    assert filters == ["ids.openalex:W5", "doi:10.1093/geront/20.6.649", "pmid:1|2"]
    assert recorder.requests[0].url.params["select"] == "id,doi,ids,referenced_works"


def test_links_skip_empty_lists_and_refuse_bad_identifiers() -> None:
    recorder = Recorder()

    async def scenario(**kwargs: list[str]) -> None:
        async with recorder.client() as client:
            assert await client.links(**kwargs) == ()

    asyncio.run(scenario())
    assert recorder.requests == []
    for bad in ({"work_ids": ["X1"]}, {"pmids": ["12a"]}, {"dois": ["10.1/a|b"]}):
        with pytest.raises(ValueError, match="invalid identifiers"):
            asyncio.run(scenario(**bad))


def test_citing_sorts_by_citations_and_keeps_the_total() -> None:
    recorder = Recorder(_ok("openalex_citing_zarit.json"))

    async def scenario() -> None:
        async with recorder.client() as client:
            found = await client.citing(ZARIT, limit=3)
        assert found.total == 5968
        assert len(found.records) == 3
        assert found.records[1].title == "Caregiver Burden: A Clinical Review"

    asyncio.run(scenario())
    params = recorder.requests[0].url.params
    assert params["filter"] == f"cites:{ZARIT}"
    assert params["sort"] == "cited_by_count:desc"
    assert (params["per_page"], params["page"]) == ("3", "1")


def test_citing_pages_up_to_the_limit() -> None:
    def works(start: int, size: int) -> dict[str, object]:
        ids = range(start, start + size)
        return {
            "meta": {"count": 900},
            "results": [{"id": f"https://openalex.org/W{n}"} for n in ids],
        }

    recorder = Recorder(_ok(works(1, 200)), _ok(works(201, 200)))

    async def scenario() -> None:
        async with recorder.client() as client:
            found = await client.citing("W1", limit=250)
        assert found.total == 900
        assert [r.record_id for r in found.records] == [f"W{n}" for n in range(1, 251)]

    asyncio.run(scenario())
    assert [r.url.params["page"] for r in recorder.requests] == ["1", "2"]

    async def refused(work_id: str, limit: int) -> None:
        async with Recorder().client() as client:
            await client.citing(work_id, limit=limit)

    with pytest.raises(ValueError, match="invalid OpenAlex work id"):
        asyncio.run(refused("12", 1))
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(refused("W1", 10_001))


def test_summaries_can_leave_out_works_openalex_no_longer_has() -> None:
    recorder = Recorder(_ok("openalex_references_zarit.json"))
    wanted = ("W229834578", "W99999999999", "W235183263", "W1640326000")

    async def scenario() -> None:
        async with recorder.client() as client:
            records = await client.summaries(wanted, missing_ok=True)
        assert [r.record_id for r in records] == ["W229834578", "W235183263", "W1640326000"]
        assert records[0].title == "Envejecimiento y familia"

    asyncio.run(scenario())


def test_rejected_queries_explain_why() -> None:
    recorder = Recorder(httpx.Response(400, json=load_fixture("openalex_error_wildcard.json")))

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.search('"family caregiv*"')

    with pytest.raises(DatabaseError, match=r"HTTP 400\): Wildcards \(\* or \?\) do not work"):
        asyncio.run(scenario())


def test_retries_on_rate_limit_and_server_errors() -> None:
    recorder = Recorder(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.ConnectError("boom"),
        httpx.Response(503),
        _ok("openalex_search_caregiver.json"),
    )

    async def scenario() -> None:
        async with recorder.client() as client:
            assert (await client.search("x")).count == 2516

    asyncio.run(scenario())
    assert recorder.sleeps == [2.0, 2.0, 4.0]


def test_long_waits_mean_the_daily_budget_is_spent() -> None:
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "3600"}))

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.search("x")

    with pytest.raises(OpenAlexError, match="daily budget"):
        asyncio.run(scenario())
    assert recorder.sleeps == []


def test_gives_up_after_max_retries() -> None:
    recorder = Recorder(*(httpx.Response(503) for _ in range(2)))

    async def scenario() -> None:
        async with recorder.client(OpenAlexConfig(max_retries=1)) as client:
            await client.search("x")

    with pytest.raises(OpenAlexError, match="after 2 attempts: HTTP 503"):
        asyncio.run(scenario())


def test_non_json_answers_are_errors() -> None:
    recorder = Recorder(httpx.Response(200, text="<html>"))

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.search("x")

    with pytest.raises(OpenAlexError, match="did not return JSON"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda c: c.search(" "), "cannot be empty"),
        (lambda c: c.search("x", retmax=-1), "cannot be negative"),
        (lambda c: c.search("x", retmax=9_000, retstart=2_000), "at most the first 10000"),
        (lambda c: c.summaries(["12345"]), "invalid OpenAlex work ids: 12345"),
    ],
)
def test_input_validation(
    call: Callable[[OpenAlexClient], Awaitable[object]], message: str
) -> None:
    recorder = Recorder()

    async def scenario() -> None:
        async with recorder.client() as client:
            await call(client)

    with pytest.raises(ValueError, match=message):
        asyncio.run(scenario())
    assert recorder.requests == []
