"""End-to-end flow: structured question -> equation -> PubMed results to screen."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from biblio_search.models import ArticleSummary, SearchResult, StrategyOptions, StructuredQuestion
from biblio_search.protocols import QueryDialect


class SearchBackend(Protocol):
    """What the pipeline needs from a database client (EutilsClient satisfies it)."""

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        """Run ``term`` and return the count and the first ``retmax`` record ids."""

    async def summaries(self, pmids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        """Fetch the summaries of the given records."""


@dataclass(frozen=True, slots=True)
class SearchReport:
    """Everything needed to document and screen one run of a search."""

    equation: str
    result: SearchResult
    records: tuple[ArticleSummary, ...]


async def run_search(
    question: StructuredQuestion,
    options: StrategyOptions,
    dialect: QueryDialect,
    backend: SearchBackend,
    *,
    max_records: int = 20,
) -> SearchReport:
    """Build the equation, run it and fetch the summaries of the first ``max_records``."""
    equation = dialect.render(question, options)
    result = await backend.search(equation, retmax=max_records)
    records = await backend.summaries(result.ids) if result.ids else ()
    return SearchReport(equation=equation, result=result, records=records)
