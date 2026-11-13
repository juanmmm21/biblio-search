from __future__ import annotations

from pathlib import Path

from biblio_search.eric_query import EricDialect, quote, render_term, render_text
from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    FreeTextField,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
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


def _eric(text: str, scope: ControlledScope = ControlledScope.HEADING) -> Term:
    return Term.controlled(text, scope, Vocabulary.ERIC)


def test_render_text_keeps_words_bare_and_quotes_the_rest() -> None:
    assert render_text(Term.free("tutoring")) == "tutoring"
    assert render_text(Term.free("tutor*")) == "tutor*"
    assert render_text(Term.free("peer assisted learning")) == '"peer assisted learning"'
    assert render_text(Term.free("self-regulation")) == '"self-regulation"'
    assert render_text(Term.free("AND")) == '"AND"'
    assert render_text(Term.free("C\\C++")) == '"C\\\\C++"'


def test_truncated_phrase_requires_its_words_together() -> None:
    assert render_text(Term.free("peer tutor*")) == "(peer AND tutor*)"
    # «or» sería un operador: va entre comillas.
    assert render_text(Term.free("read or writ*")) == '(read AND "or" AND writ*)'
    # El comodín solo vale tras una palabra simple.
    assert render_text(Term.free("self-regulat*")) == '"self-regulat"'


def test_render_term_by_field() -> None:
    assert render_term(Term.free("tutor*")) == "title:tutor* OR description:tutor*"
    assert render_term(Term.free("tutor*", FreeTextField.TITLE)) == "title:tutor*"
    assert render_term(_eric("Peer Teaching")) == 'subject_ss:"Peer Teaching"'


def test_quote_escapes_backslashes() -> None:
    assert quote("a\\b") == '"a\\\\b"'


def test_dialect_satisfies_protocol_and_indexes_eric() -> None:
    dialect: QueryDialect = EricDialect()
    assert dialect.name == "eric"
    assert dialect.vocabularies == frozenset({Vocabulary.ERIC})


def test_render_example_question() -> None:
    question, options = load_question(EXAMPLES / "peer-tutoring-reading.toml")
    equation = EricDialect().render(question, options)
    assert equation.startswith(
        '(subject_ss:"Peer Teaching" OR title:(peer AND tutor*) OR description:(peer AND tutor*)'
    )
    assert ' AND (subject_ss:"Reading Comprehension" OR ' in equation
    assert equation.endswith("description:(elementary AND school*))")


def test_mesh_headings_become_free_text_and_exclusions_use_not() -> None:
    question = _question(
        Condition("anxiety", (Term.controlled("Anxiety"), Term.free("anxious*"))),
        exclusions=(Condition("adults", (_eric("Adults"),)),),
    )
    equation = EricDialect().render(question, StrategyOptions())
    assert equation == (
        "(title:anxiety OR description:anxiety OR title:anxious* OR description:anxious*)"
        ' NOT (subject_ss:"Adults")'
    )


def test_repeating_descriptors_as_free_text_and_ignoring_medline() -> None:
    question = _question(Condition("tutoring", (_eric("Tutoring"),)))
    options = StrategyOptions(repeat_controlled_as_free=True, free_text_unindexed_only=True)
    assert EricDialect().render(question, options) == (
        '(subject_ss:"Tutoring" OR title:tutoring OR description:tutoring)'
    )


def test_notes_explain_phrases_wildcards_and_scopes() -> None:
    question = _question(
        Condition(
            "c",
            (
                _eric("Tutoring", ControlledScope.MAJOR),
                Term.free("peer tutor*"),
                Term.free("self-regulat*"),
            ),
        )
    )
    notes = EricDialect().notes(question)
    assert len(notes) == 3
    assert notes[0].endswith("in any order: peer tutor*")
    assert notes[1].endswith("searched without it: self-regulat*")
    assert notes[2].endswith("searched as plain descriptors: Tutoring")
    assert EricDialect().notes(_question(Condition("c", (Term.free("tutor*"),)))) == ()
