from __future__ import annotations

import pytest

from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    Database,
    Discipline,
    QuestionError,
    QuestionFormat,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.selection import (
    DatabaseSelection,
    SelectionMode,
    automatic_reasons,
    select_databases,
)

SEARCHABLE = frozenset({Database.PUBMED})


def _question(
    question_format: QuestionFormat = QuestionFormat.CONCEPTS,
    *terms: Term,
    discipline: Discipline | None = None,
    databases: tuple[Database, ...] = (),
) -> StructuredQuestion:
    role = ComponentRole.CONCEPT
    if question_format is not QuestionFormat.CONCEPTS:
        role = ComponentRole.POPULATION
    condition = Condition("c", terms or (Term.free("x"),))
    return StructuredQuestion(
        "q",
        question_format,
        (Component(role, "d", (condition,)),),
        discipline=discipline,
        databases=databases,
    )


def test_health_question_goes_to_pubmed_and_openalex() -> None:
    question = _question(QuestionFormat.SPC, Term.controlled("Anxiety"))
    selection = select_databases(question, SEARCHABLE)
    assert selection.mode is SelectionMode.AUTOMATIC
    assert selection.databases == (Database.PUBMED, Database.OPENALEX)
    assert selection.choices[0].reasons == (
        "the question uses MeSH headings",
        "SPC is a health-science question format",
    )
    assert "general multidisciplinary" in selection.choices[1].reasons[0]
    assert selection.searchable == (Database.PUBMED,)
    assert selection.pending == (Database.OPENALEX,)


def test_health_discipline_alone_picks_pubmed() -> None:
    reasons = automatic_reasons(_question(discipline=Discipline.HEALTH))
    assert reasons[Database.PUBMED] == ("the discipline is health",)


def test_education_by_discipline_and_by_descriptors() -> None:
    eric = Term.controlled("Peer Teaching", vocabulary=Vocabulary.ERIC)
    by_discipline = automatic_reasons(_question(discipline=Discipline.EDUCATION))
    by_vocabulary = automatic_reasons(_question(QuestionFormat.CONCEPTS, eric))
    assert list(by_discipline) == [Database.ERIC, Database.OPENALEX]
    assert by_discipline[Database.ERIC] == ("the discipline is education",)
    assert by_vocabulary[Database.ERIC] == ("the question uses ERIC descriptors",)


@pytest.mark.parametrize(
    ("discipline", "label"),
    [
        (Discipline.PHYSICS, "physics"),
        (Discipline.MATHEMATICS, "mathematics"),
        (Discipline.COMPUTER_SCIENCE, "computer science"),
        (Discipline.ECONOMICS, "economics"),
    ],
)
def test_exact_sciences_go_to_arxiv(discipline: Discipline, label: str) -> None:
    reasons = automatic_reasons(_question(discipline=discipline))
    assert list(reasons) == [Database.ARXIV, Database.OPENALEX]
    assert reasons[Database.ARXIV] == (f"the discipline is {label}",)


def test_plain_concepts_only_get_openalex() -> None:
    selection = select_databases(_question(), SEARCHABLE)
    assert selection.databases == (Database.OPENALEX,)
    assert selection.searchable == ()


def test_file_databases_override_the_rules() -> None:
    question = _question(
        QuestionFormat.PICO,
        Term.controlled("Anxiety"),
        databases=(Database.OPENALEX, Database.PUBMED),
    )
    selection = select_databases(question, SEARCHABLE)
    assert selection.mode is SelectionMode.MANUAL
    assert selection.databases == (Database.OPENALEX, Database.PUBMED)
    pubmed = selection.choices[1]
    assert pubmed.reasons[0] == "chosen by hand"
    assert "the question uses MeSH headings" in pubmed.reasons
    assert pubmed.searchable


def test_requested_databases_override_the_file() -> None:
    question = _question(databases=(Database.ERIC,))
    selection = select_databases(question, SEARCHABLE, [Database.PUBMED])
    assert selection.databases == (Database.PUBMED,)
    assert selection.choices[0].reasons == ("chosen by hand",)


def test_requested_databases_cannot_repeat() -> None:
    with pytest.raises(QuestionError, match="only once"):
        select_databases(_question(), SEARCHABLE, [Database.ERIC, Database.ERIC])


def test_empty_selection_has_nothing_pending() -> None:
    selection = DatabaseSelection(SelectionMode.MANUAL, ())
    assert selection.databases == selection.searchable == selection.pending == ()
