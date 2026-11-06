"""Interfaces shared by the different parts of the pipeline."""

from __future__ import annotations

from typing import Protocol

from biblio_search.models import StrategyOptions, StructuredQuestion


class QueryDialect(Protocol):
    """Translates a structured question into the query syntax of one database."""

    name: str

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        """Return the full search equation for ``question``."""
