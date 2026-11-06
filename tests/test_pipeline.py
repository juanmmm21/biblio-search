from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from conftest import load_fixture

from biblio_search.eutils import parse_search, parse_summaries
from biblio_search.models import ArticleSummary, SearchResult, StrategyOptions
from biblio_search.pipeline import SearchBackend, run_search
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
