"""Choosing the databases a question is searched in.

Choosing the databases is the first step of the strategy (Del Pino, 2024). The automatic
choice follows fixed rules instead of a model, so it is reproducible and every database
comes with the reasons it was picked; a choice made by hand always overrides it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from biblio_search.models import (
    Database,
    Discipline,
    QuestionError,
    QuestionFormat,
    StructuredQuestion,
    Vocabulary,
)

# arXiv solo cubre ciencias exactas, informática y economía cuantitativa.
ARXIV_DISCIPLINES = frozenset(
    {
        Discipline.PHYSICS,
        Discipline.MATHEMATICS,
        Discipline.COMPUTER_SCIENCE,
        Discipline.ECONOMICS,
    }
)
# Orden fijo de presentación: primero las bases especializadas, la general al final.
DATABASE_ORDER = (Database.PUBMED, Database.ERIC, Database.ARXIV, Database.OPENALEX)


class SelectionMode(StrEnum):
    """Who chose the databases."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class DatabaseChoice:
    """A chosen database, why it was chosen and whether it can be searched yet."""

    database: Database
    reasons: tuple[str, ...]
    searchable: bool


@dataclass(frozen=True, slots=True)
class DatabaseSelection:
    """The databases of a search and how they were chosen."""

    mode: SelectionMode
    choices: tuple[DatabaseChoice, ...]

    @property
    def databases(self) -> tuple[Database, ...]:
        return tuple(choice.database for choice in self.choices)

    @property
    def searchable(self) -> tuple[Database, ...]:
        return tuple(choice.database for choice in self.choices if choice.searchable)

    @property
    def pending(self) -> tuple[Database, ...]:
        """Chosen databases biblio-search cannot search yet."""
        return tuple(choice.database for choice in self.choices if not choice.searchable)


def _uses(question: StructuredQuestion, vocabulary: Vocabulary) -> bool:
    return any(term.vocabulary == vocabulary for term in question.controlled_terms)


def automatic_reasons(question: StructuredQuestion) -> dict[Database, tuple[str, ...]]:
    """Apply the selection rules and return every database they pick with its reasons."""
    reasons: dict[Database, list[str]] = {database: [] for database in DATABASE_ORDER}
    if _uses(question, Vocabulary.MESH):
        reasons[Database.PUBMED].append("the question uses MeSH headings")
    if question.format in (QuestionFormat.SPC, QuestionFormat.PICO):
        reasons[Database.PUBMED].append(
            f"{question.format.value.upper()} is a health-science question format"
        )
    if question.discipline is Discipline.HEALTH:
        reasons[Database.PUBMED].append("the discipline is health")
    if question.discipline is Discipline.EDUCATION:
        reasons[Database.ERIC].append("the discipline is education")
    if _uses(question, Vocabulary.ERIC):
        reasons[Database.ERIC].append("the question uses ERIC descriptors")
    if question.discipline in ARXIV_DISCIPLINES:
        label = question.discipline.value.replace("_", " ")
        reasons[Database.ARXIV].append(f"the discipline is {label}")
    reasons[Database.OPENALEX].append(
        "general multidisciplinary database, so nothing outside the specialised ones is missed"
    )
    return {database: tuple(why) for database, why in reasons.items() if why}


def select_databases(
    question: StructuredQuestion,
    searchable: Collection[Database],
    requested: Sequence[Database] = (),
) -> DatabaseSelection:
    """Choose where to search ``question``.

    ``requested`` (e.g. from the command line) overrides the databases of the question file,
    and both override the automatic rules. ``searchable`` are the databases biblio-search
    already has a client for.
    """
    manual = tuple(requested) or question.databases
    if manual:
        if len(set(manual)) != len(manual):
            raise QuestionError("each database can be chosen only once")
        # Lo que dirían las reglas se conserva como motivo extra, para documentarlo igual.
        rules = automatic_reasons(question)
        choices = tuple(
            DatabaseChoice(
                database,
                ("chosen by hand", *rules.get(database, ())),
                database in searchable,
            )
            for database in manual
        )
        return DatabaseSelection(SelectionMode.MANUAL, choices)
    choices = tuple(
        DatabaseChoice(database, why, database in searchable)
        for database, why in automatic_reasons(question).items()
    )
    return DatabaseSelection(SelectionMode.AUTOMATIC, choices)
