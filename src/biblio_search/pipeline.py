"""End-to-end flow: structured question -> equation -> PubMed results to screen."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
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


@dataclass(frozen=True, slots=True)
class EquationBuilt:
    """The equation of ``database`` is ready and about to be sent."""

    database: str
    equation: str


@dataclass(frozen=True, slots=True)
class CountReceived:
    """``database`` answered with the number of records and how it read the equation."""

    database: str
    result: SearchResult


@dataclass(frozen=True, slots=True)
class RecordsReceived:
    """The summaries of the first records of ``database`` arrived."""

    database: str
    records: tuple[ArticleSummary, ...]


SearchEvent = EquationBuilt | CountReceived | RecordsReceived


async def search_progress(
    question: StructuredQuestion,
    options: StrategyOptions,
    dialect: QueryDialect,
    backend: SearchBackend,
    *,
    max_records: int = 20,
) -> AsyncIterator[SearchEvent]:
    """Run a search step by step, reporting each step as soon as it finishes.

    The web interface shows these events live; :func:`run_search` just collects them.
    """
    equation = dialect.render(question, options)
    yield EquationBuilt(dialect.name, equation)
    result = await backend.search(equation, retmax=max_records)
    yield CountReceived(dialect.name, result)
    records = await backend.summaries(result.ids) if result.ids else ()
    yield RecordsReceived(dialect.name, records)


async def run_search(
    question: StructuredQuestion,
    options: StrategyOptions,
    dialect: QueryDialect,
    backend: SearchBackend,
    *,
    max_records: int = 20,
) -> SearchReport:
    """Build the equation, run it and fetch the summaries of the first ``max_records``."""
    equation = ""
    result: SearchResult | None = None
    records: tuple[ArticleSummary, ...] = ()
    steps = search_progress(question, options, dialect, backend, max_records=max_records)
    async for event in steps:
        match event:
            case EquationBuilt():
                equation = event.equation
            case CountReceived():
                result = event.result
            case RecordsReceived():
                records = event.records
    if result is None:
        raise RuntimeError("the search finished without a result")
    return SearchReport(equation=equation, result=result, records=records)
