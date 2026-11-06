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
)
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect, expand_condition, render_term
from biblio_search.question_file import load_question

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"


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
