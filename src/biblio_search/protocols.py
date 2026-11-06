"""Interfaces shared by the different parts of the pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from biblio_search.models import StrategyOptions, StructuredQuestion, ThesaurusEntry


class QueryDialect(Protocol):
    """Translates a structured question into the query syntax of one database."""

    name: str

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        """Return the full search equation for ``question``."""


class Thesaurus(Protocol):
    """A controlled vocabulary (MeSH, later ERIC or DeCS) that can confirm headings."""

    name: str

    async def lookup(self, texts: Sequence[str]) -> Mapping[str, ThesaurusEntry | None]:
        """Map every text to the heading it names (itself or as a synonym), or ``None``."""
