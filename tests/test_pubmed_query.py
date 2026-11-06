from __future__ import annotations

from pathlib import Path

import pytest

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
from biblio_search.pubmed_query import (
    PubMedDialect,
    expand_condition,
    foreign_terms,
    localize_condition,
    render_term,
)
from biblio_search.question_file import load_question

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE = EXAMPLES / "caregiver-burden.toml"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (ControlledScope.HEADING, "Anxiety[mh]"),
        (ControlledScope.MAJOR, "Anxiety[mj]"),
        (ControlledScope.HEADING_NO_EXPLODE, "Anxiety[mh:noexp]"),
        (ControlledScope.MAJOR_NO_EXPLODE, "Anxiety[mj:noexp]"),
    ],
)
def test_controlled_tags(scope: ControlledScope, expected: str) -> None:
    assert render_term(Term.controlled("Anxiety", scope)) == expected


def test_free_text_tags() -> None:
    assert render_term(Term.free("caregiv*")) == "caregiv*[tiab]"
    assert render_term(Term.free("burden", FreeTextField.TITLE)) == "burden[ti]"


def test_quotes_only_when_needed() -> None:
    assert render_term(Term.controlled("Aged, 80 and over")) == '"Aged, 80 and over"[mh]'
    assert render_term(Term.free("older people")) == "older people[tiab]"
    assert render_term(Term.free("family caregiv*")) == '"family caregiv*"[tiab]'


def _question(*conditions: Condition, exclusions: tuple[Condition, ...] = ()) -> StructuredQuestion:
    return StructuredQuestion(
        text="q",
        format=QuestionFormat.SPC,
        components=(Component(ComponentRole.SITUATION, "s", conditions),),
        exclusions=exclusions,
    )


ANXIETY = Condition(
    "anxiety",
    (Term.controlled("Anxiety", ControlledScope.MAJOR_NO_EXPLODE), Term.free("anxi*")),
)
BURDEN = Condition("burden", (Term.free("burden*"), Term.free("overload*")))


def test_conditions_are_parenthesised_and_joined_with_and() -> None:
    equation = PubMedDialect().render(_question(ANXIETY, BURDEN), StrategyOptions())
    assert equation == ("(Anxiety[mj:noexp] OR anxi*[tiab]) AND (burden*[tiab] OR overload*[tiab])")


def test_single_term_condition_keeps_parentheses() -> None:
    condition = Condition("aged", (Term.controlled("Aged"),))
    assert PubMedDialect().render(_question(condition), StrategyOptions()) == "(Aged[mh])"


def test_exclusions_are_appended_with_not() -> None:
    animals = Condition("animals", (Term.controlled("Animals"), Term.free("mice")))
    equation = PubMedDialect().render(_question(BURDEN, exclusions=(animals,)), StrategyOptions())
    assert equation == "(burden*[tiab] OR overload*[tiab]) NOT (Animals[mh] OR mice[tiab])"


def test_repeat_controlled_as_free_skips_covered_and_quoted_headings() -> None:
    condition = Condition(
        "c",
        (
            Term.controlled("Caregivers"),
            Term.controlled("Aged, 80 and over"),
            Term.controlled("Depression"),
            Term.free("caregiv*"),
        ),
    )
    expanded = expand_condition(condition, StrategyOptions(repeat_controlled_as_free=True))
    assert expanded.free_terms == (Term.free("caregiv*"), Term.free("depression"))


def test_repeat_controlled_as_free_is_off_by_default() -> None:
    assert expand_condition(ANXIETY, StrategyOptions()) is ANXIETY


def test_unindexed_only_wraps_free_text_with_medline_filter() -> None:
    options = StrategyOptions(free_text_unindexed_only=True)
    equation = PubMedDialect().render(_question(ANXIETY, BURDEN), options)
    assert equation == (
        "(Anxiety[mj:noexp] OR ((anxi*[tiab]) NOT MEDLINE[sb]))"
        " AND (burden*[tiab] OR overload*[tiab])"
    )


def test_del_pino_example() -> None:
    question, options = load_question(EXAMPLE)
    assert PubMedDialect().render(question, options) == (
        "(Anxiety[mj:noexp] OR Depression[mj] OR anxiety[tiab] OR depress*[tiab])"
        " AND (Caregivers[mj] OR caregiv*[tiab] OR carer*[tiab])"
        " AND (Aged[mh] OR elder*[tiab] OR older people[tiab])"
        " AND (burden*[tiab] OR overload*[tiab] OR strain*[tiab])"
    )


def test_pubmed_dialect_satisfies_protocol() -> None:
    dialect: QueryDialect = PubMedDialect()
    assert dialect.name == "pubmed"


def _eric(text: str) -> Term:
    return Term.controlled(text, vocabulary=Vocabulary.ERIC)


def test_foreign_headings_become_free_text_unless_covered() -> None:
    condition = Condition(
        "reading",
        (
            _eric("Reading Comprehension"),
            _eric("Peer Teaching"),
            Term.controlled("Comprehension"),
            Term.free("reading comprehen*"),
        ),
    )
    local = localize_condition(condition, PubMedDialect.vocabularies)
    assert local.terms == (
        Term.free("peer teaching"),
        Term.controlled("Comprehension"),
        Term.free("reading comprehen*"),
    )


def test_foreign_headings_repeated_across_vocabularies_are_searched_once() -> None:
    condition = Condition("x", (_eric("Tutoring"), Term(text="Tutoring", kind=_eric("a").kind)))
    assert condition.terms[1].vocabulary is Vocabulary.MESH
    local = localize_condition(condition, frozenset())
    assert local.terms == (Term.free("tutoring"),)


def test_foreign_exclusions_and_terms_are_reported() -> None:
    exclusion = Condition("adults", (_eric("Adult Students"),))
    question = _question(BURDEN, exclusions=(exclusion,))
    equation = PubMedDialect().render(question, StrategyOptions())
    assert equation == "(burden*[tiab] OR overload*[tiab]) NOT (adult students[tiab])"
    assert foreign_terms(question, PubMedDialect.vocabularies) == (_eric("Adult Students"),)
    assert foreign_terms(_question(ANXIETY), PubMedDialect.vocabularies) == ()


def test_education_example_in_pubmed() -> None:
    question, options = load_question(EXAMPLES / "peer-tutoring-reading.toml")
    # «Reading Comprehension» y «Elementary School Students» ya los cubre el texto libre.
    assert PubMedDialect().render(question, options) == (
        '(peer teaching[tiab] OR "peer tutor*"[tiab] OR peer assisted learning[tiab]'
        ' OR "cross age tutor*"[tiab])'
        ' AND (reading comprehension[tiab] OR "reading skill*"[tiab])'
        ' AND ("primary school*"[tiab] OR "elementary school*"[tiab])'
    )
