from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from conftest import load_fixture

from biblio_search.eutils import parse_search, parse_summaries
from biblio_search.models import ArticleSummary, SearchResult, StrategyOptions
from biblio_search.pipeline import (
    CountReceived,
    EquationBuilt,
    RecordsReceived,
    SearchBackend,
    SearchEvent,
    run_search,
    search_progress,
)
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"


class FakeBackend:
    def __init__(self, result: SearchResult) -> None:
        self.result = result
        self.terms: list[tuple[str, int]] = []
        self.summary_calls: list[tuple[str, ...]] = []

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        self.terms.append((term, retmax))
        return self.result

    async def summaries(self, pmids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        self.summary_calls.append(tuple(pmids))
        return parse_summaries(load_fixture("esummary_caregiver.json"))


def test_run_search_builds_runs_and_fetches() -> None:
    question, options = load_question(EXAMPLE)
    backend = FakeBackend(parse_search(load_fixture("esearch_caregiver.json")))
    typed: SearchBackend = backend

    report = asyncio.run(run_search(question, options, PubMedDialect(), typed, max_records=3))

    assert backend.terms == [(PubMedDialect().render(question, options), 3)]
    assert backend.summary_calls == [("42970524", "42956697", "42919807")]
    assert report.result.count == 2808
    assert len(report.records) == 3


def test_run_search_skips_summaries_without_results() -> None:
    question, _ = load_question(EXAMPLE)
    backend = FakeBackend(parse_search(load_fixture("esearch_not_found.json")))

    report = asyncio.run(run_search(question, StrategyOptions(), PubMedDialect(), backend))

    assert backend.summary_calls == []
    assert report.records == ()
    assert report.result.not_found == ("qwzxvplorkt",)


async def _collect(backend: FakeBackend) -> list[SearchEvent]:
    question, options = load_question(EXAMPLE)
    steps = search_progress(question, options, PubMedDialect(), backend, max_records=3)
    return [event async for event in steps]


def test_search_progress_reports_each_step_in_order() -> None:
    backend = FakeBackend(parse_search(load_fixture("esearch_caregiver.json")))

    events = asyncio.run(_collect(backend))

    assert [type(event) for event in events] == [EquationBuilt, CountReceived, RecordsReceived]
    assert {event.database for event in events} == {"pubmed"}
    first, second, third = events
    assert isinstance(first, EquationBuilt)
    assert first.equation == backend.terms[0][0]
    assert isinstance(second, CountReceived)
    assert second.result.count == 2808
    assert isinstance(third, RecordsReceived)
    assert len(third.records) == 3


def test_search_progress_reports_empty_records_without_results() -> None:
    backend = FakeBackend(parse_search(load_fixture("esearch_not_found.json")))

    events = asyncio.run(_collect(backend))

    last = events[-1]
    assert isinstance(last, RecordsReceived)
    assert last.records == ()
    assert backend.summary_calls == []
