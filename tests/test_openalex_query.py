from __future__ import annotations

from pathlib import Path

from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    FreeTextField,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.openalex_query import OpenAlexDialect, render_term
from biblio_search.protocols import QueryDialect
from biblio_search.question_file import load_question

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _question(*conditions: Condition, exclusions: tuple[Condition, ...] = ()) -> StructuredQuestion:
    return StructuredQuestion(
        text="q",
        format=QuestionFormat.CONCEPTS,
        components=(Component(ComponentRole.CONCEPT, "concept", conditions),),
        exclusions=exclusions,
    )


def test_render_term_quotes_phrases_and_keeps_words_bare() -> None:
    assert render_term(Term.free("caregiv*")) == "caregiv*"
    assert render_term(Term.free("older people")) == '"older people"'
    assert render_term(Term.free("aged, 80 and over")) == '"aged, 80 and over"'


def test_truncated_phrases_become_zero_distance_proximity() -> None:
    # OpenAlex rechaza «"family caregiv*"» con un 400; la proximidad 0 sí la acepta.
    assert render_term(Term.free("family caregiv*")) == '"family caregiv*"~0'


def test_dialect_satisfies_protocol_and_indexes_no_thesaurus() -> None:
    dialect: QueryDialect = OpenAlexDialect()
    assert dialect.name == "openalex"
    assert dialect.vocabularies == frozenset()


def test_example_equation_searches_headings_as_free_text() -> None:
    question, options = load_question(EXAMPLES / "caregiver-burden.toml")
    assert OpenAlexDialect().render(question, options) == (
        "(anxiety OR depress*) AND (caregiv* OR carer*) AND "
        '(aged OR elder* OR "older people") AND (burden* OR overload* OR strain*)'
    )


def test_options_do_not_change_the_equation() -> None:
    question, _ = load_question(EXAMPLES / "caregiver-burden.toml")
    dialect = OpenAlexDialect()
    sensitive = StrategyOptions(repeat_controlled_as_free=True, free_text_unindexed_only=True)
    assert dialect.render(question, sensitive) == dialect.render(question, StrategyOptions())


def test_exclusions_and_eric_headings() -> None:
    question = _question(
        Condition(
            "tutoring",
            (
                Term.controlled("Peer Teaching", vocabulary=Vocabulary.ERIC),
                Term.free("peer tutor*"),
            ),
        ),
        exclusions=(Condition("adults", (Term.free("adult*"), Term.free("university"))),),
    )
    assert OpenAlexDialect().render(question, StrategyOptions()) == (
        '("peer teaching" OR "peer tutor*"~0) NOT (adult* OR university)'
    )


def test_notes_explain_title_only_terms() -> None:
    question = _question(
        Condition("burden", (Term.free("burden", FreeTextField.TITLE), Term.free("strain*"))),
    )
    dialect = OpenAlexDialect()
    assert dialect.render(question, StrategyOptions()) == "(burden OR strain*)"
    assert dialect.notes(question) == (
        "OpenAlex cannot mix fields in one search, so these title-only terms are searched "
        "in title and abstract: burden",
    )
    plain = _question(Condition("burden", (Term.free("burden"),)))
    assert dialect.notes(plain) == ()
