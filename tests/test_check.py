from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from biblio_search.check import QuestionCheck, apply_check, check_question, scope_note
from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermCheck,
    TermKind,
    TermStatus,
    ThesaurusEntry,
    Vocabulary,
)
from biblio_search.pubmed_query import PubMedDialect

CAREGIVERS = ThesaurusEntry(
    "mesh",
    "D017028",
    "Caregivers",
    ("Caregiver", "Carers", "Family Caregivers", "Family Caregiver"),
)
ANXIETY = ThesaurusEntry("mesh", "D001007", "Anxiety", ("Angst", "Social Anxiety"), True)


class FakeThesaurus:
    name = "mesh"

    def __init__(self, entries: Mapping[str, ThesaurusEntry]) -> None:
        self.entries = {k.casefold(): v for k, v in entries.items()}
        self.asked: list[list[str]] = []

    async def lookup(self, texts: Sequence[str]) -> Mapping[str, ThesaurusEntry | None]:
        self.asked.append(list(texts))
        return {text: self.entries.get(text.casefold()) for text in texts}


THESAURUS = {"Caregivers": CAREGIVERS, "Carers": CAREGIVERS, "Anxiety": ANXIETY}


def _question() -> StructuredQuestion:
    population = Condition(
        "caregivers",
        (Term.controlled("Carers"), Term.controlled("Caregivers"), Term.free("caregiv*")),
    )
    situation = Condition(
        "anxiety",
        (
            Term.controlled("Anxiety", ControlledScope.MAJOR),
            Term.controlled("Worry Burden"),
            Term.free("worry burden"),
        ),
    )
    return StructuredQuestion(
        text="q",
        format=QuestionFormat.SPC,
        components=(
            Component(ComponentRole.POPULATION, "carers", (population,)),
            Component(ComponentRole.SITUATION, "anxiety", (situation,)),
        ),
        exclusions=(Condition("children", (Term.controlled("Anxiety"),)),),
    )


def _check(question: StructuredQuestion, thesaurus: FakeThesaurus) -> QuestionCheck:
    return asyncio.run(check_question(question, thesaurus))


def test_statuses_follow_the_thesaurus() -> None:
    thesaurus = FakeThesaurus(THESAURUS)
    result = _check(_question(), thesaurus)
    statuses = [(c.condition, c.term.text, c.status) for c in result.checks]
    assert statuses == [
        ("anxiety", "Anxiety", TermStatus.VALID),
        ("anxiety", "Worry Burden", TermStatus.NOT_FOUND),
        ("caregivers", "Carers", TermStatus.ENTRY_TERM),
        ("caregivers", "Caregivers", TermStatus.VALID),
        ("children", "Anxiety", TermStatus.VALID),
    ]
    assert thesaurus.asked == [["Anxiety", "Worry Burden", "Carers", "Caregivers"]]
    assert result.vocabulary == "mesh"
    assert not result.all_valid


def test_suggestions_skip_covered_plural_and_heading_terms() -> None:
    result = _check(_question(), FakeThesaurus(THESAURUS))
    by_label = {item.condition.label: item.suggested_synonyms for item in result.conditions}
    # «Caregiver» lo cubre caregiv*; «Family Caregiver» es el plural de una sugerencia previa.
    assert by_label["caregivers"] == ("Carers", "Family Caregivers")
    assert by_label["anxiety"] == ("Angst", "Social Anxiety")


def test_question_without_controlled_terms_does_not_call_the_thesaurus() -> None:
    question = StructuredQuestion(
        "q",
        QuestionFormat.SPC,
        (Component(ComponentRole.QUESTION, "d", (Condition("c", (Term.free("x"),)),)),),
    )
    thesaurus = FakeThesaurus({})
    result = _check(question, thesaurus)
    assert result.checks == ()
    assert result.all_valid
    assert thesaurus.asked == []


def test_apply_check_renames_entry_terms_and_demotes_unknown() -> None:
    question = _question()
    fixed = apply_check(question, _check(question, FakeThesaurus(THESAURUS)))
    anxiety, caregivers = fixed.conditions
    assert caregivers.terms == (Term.controlled("Caregivers"), Term.free("caregiv*"))
    assert anxiety.terms == (
        Term.controlled("Anxiety", ControlledScope.MAJOR),
        Term.free("Worry Burden"),
    )
    assert anxiety.free_terms[0].kind is TermKind.FREE
    assert fixed.exclusions == question.exclusions
    assert PubMedDialect().render(fixed, StrategyOptions()) == (
        "(Anxiety[mj] OR Worry Burden[tiab]) AND (Caregivers[mh] OR caregiv*[tiab])"
        " NOT (Anxiety[mh])"
    )


@pytest.mark.parametrize(
    ("scope", "narrower", "expected"),
    [
        (ControlledScope.HEADING, True, "narrower headings; use a no_explode"),
        (ControlledScope.MAJOR_NO_EXPLODE, False, "no_explode changes nothing"),
        (ControlledScope.HEADING_NO_EXPLODE, True, None),
        (ControlledScope.MAJOR, False, None),
    ],
)
def test_scope_note(scope: ControlledScope, narrower: bool, expected: str | None) -> None:
    entry = ThesaurusEntry("mesh", "D1", "Anxiety", has_narrower=narrower)
    check = TermCheck("c", Term.controlled("Anxiety", scope), TermStatus.VALID, entry)
    note = scope_note(check)
    assert note == expected if expected is None else expected in (note or "")


def test_scope_note_needs_an_entry() -> None:
    check = TermCheck("c", Term.controlled("X"), TermStatus.NOT_FOUND)
    assert scope_note(check) is None


def test_term_check_requires_entry_unless_not_found() -> None:
    with pytest.raises(ValueError):
        TermCheck("c", Term.controlled("X"), TermStatus.VALID)
    with pytest.raises(ValueError):
        TermCheck("c", Term.controlled("X"), TermStatus.NOT_FOUND, ANXIETY)


def test_only_terms_of_the_thesaurus_vocabulary_are_checked() -> None:
    eric = Term.controlled("Anxiety", vocabulary=Vocabulary.ERIC)
    question = StructuredQuestion(
        "q",
        QuestionFormat.CONCEPTS,
        (
            Component(
                ComponentRole.CONCEPT,
                "anxiety",
                (Condition("anxiety", (eric, Term.controlled("Carers"), Term.free("anxi*"))),),
            ),
        ),
        exclusions=(
            Condition("tests", (Term.controlled("Test Anxiety", vocabulary=Vocabulary.ERIC),)),
        ),
    )
    thesaurus = FakeThesaurus(THESAURUS)
    result = _check(question, thesaurus)
    assert thesaurus.asked == [["Carers"]]
    assert [c.term.text for c in result.checks] == ["Carers"]
    assert result.unchecked == (eric, Term.controlled("Test Anxiety", vocabulary=Vocabulary.ERIC))

    fixed = apply_check(question, result)
    # El descriptor ERIC homónimo de uno MeSH no se toca: solo se corrige lo comprobado.
    assert fixed.conditions[0].terms == (eric, Term.controlled("Caregivers"), Term.free("anxi*"))
    assert fixed.exclusions == question.exclusions


def test_spanish_term_found_through_decs_is_translated_and_renamed() -> None:
    translated = ThesaurusEntry("mesh", "D017028", "Caregivers", translated_from="decs")
    question = StructuredQuestion(
        text="q",
        format=QuestionFormat.CONCEPTS,
        components=(
            Component(
                ComponentRole.CONCEPT,
                "cuidadores",
                (Condition("cuidadores", (Term.controlled("Cuidadores"), Term.free("carer*"))),),
            ),
        ),
    )
    result = _check(question, FakeThesaurus({"Cuidadores": translated}))
    assert [c.status for c in result.checks] == [TermStatus.TRANSLATED]
    assert not result.all_valid
    fixed = apply_check(question, result)
    assert fixed.conditions[0].terms == (Term.controlled("Caregivers"), Term.free("carer*"))
