from __future__ import annotations

import pytest

from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    Database,
    Discipline,
    QuestionError,
    QuestionFormat,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
)


def test_term_collapses_whitespace() -> None:
    assert Term.free("  older   people ").text == "older people"


@pytest.mark.parametrize("text", ["", "   "])
def test_term_rejects_empty_text(text: str) -> None:
    with pytest.raises(QuestionError, match="empty"):
        Term.free(text)


@pytest.mark.parametrize("text", ['"anxiety"', "anxiety[tiab]"])
def test_term_rejects_syntax_characters(text: str) -> None:
    with pytest.raises(QuestionError, match="forbidden"):
        Term.free(text)


def test_controlled_term_cannot_be_truncated() -> None:
    with pytest.raises(QuestionError, match="cannot be truncated"):
        Term.controlled("Caregiv*")


@pytest.mark.parametrize("text", ["care*giver", "care* giv*", "*"])
def test_truncation_must_be_a_single_trailing_asterisk(text: str) -> None:
    with pytest.raises(QuestionError):
        Term.free(text)


def test_truncated_free_term() -> None:
    term = Term.free("caregiv*")
    assert term.is_truncated
    assert term.kind is TermKind.FREE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Aged, 80 and over", True),
        ("Hospitals (Private)", True),
        ("Research and Development", True),
        ("Not Applicable", True),
        ("Caregivers", False),
        ("android", False),
        ("older people", False),
    ],
)
def test_needs_quotes(text: str, expected: bool) -> None:
    assert Term.controlled(text).needs_quotes is expected


def test_condition_needs_terms_and_label() -> None:
    with pytest.raises(QuestionError, match="no terms"):
        Condition(label="empty", terms=())
    with pytest.raises(QuestionError, match="label"):
        Condition(label=" ", terms=(Term.free("x"),))


def test_condition_rejects_repeated_terms_ignoring_case() -> None:
    with pytest.raises(QuestionError, match="repeats"):
        Condition(label="c", terms=(Term.free("Anxiety"), Term.free("anxiety")))


def test_condition_allows_same_text_as_heading_and_free_text() -> None:
    condition = Condition(label="c", terms=(Term.controlled("Anxiety"), Term.free("anxiety")))
    assert condition.controlled_terms == (Term.controlled("Anxiety"),)
    assert condition.free_terms == (Term.free("anxiety"),)


def _condition(label: str) -> Condition:
    return Condition(label=label, terms=(Term.free(label),))


def test_question_orders_conditions_by_format() -> None:
    question = StructuredQuestion(
        text="q",
        format=QuestionFormat.SPC,
        components=(
            Component(ComponentRole.QUESTION, "q", (_condition("question"),)),
            Component(ComponentRole.SITUATION, "s", (_condition("situation"),)),
            Component(ComponentRole.POPULATION, "p", (_condition("population"),)),
        ),
    )
    assert [c.label for c in question.conditions] == ["situation", "population", "question"]


def test_question_rejects_role_from_another_format() -> None:
    with pytest.raises(QuestionError, match="does not belong"):
        StructuredQuestion(
            text="q",
            format=QuestionFormat.SPC,
            components=(Component(ComponentRole.INTERVENTION, "i", (_condition("i"),)),),
        )


def test_question_rejects_duplicated_roles() -> None:
    with pytest.raises(QuestionError, match="only once"):
        StructuredQuestion(
            text="q",
            format=QuestionFormat.PICO,
            components=(
                Component(ComponentRole.POPULATION, "a", (_condition("a"),)),
                Component(ComponentRole.POPULATION, "b", (_condition("b"),)),
            ),
        )


def test_pico_component_can_be_described_but_not_searched() -> None:
    question = StructuredQuestion(
        text="q",
        format=QuestionFormat.PICO,
        components=(
            Component(ComponentRole.POPULATION, "adults", (_condition("adults"),)),
            Component(ComponentRole.COMPARISON, "usual care"),
        ),
    )
    assert [c.label for c in question.conditions] == ["adults"]


def test_question_needs_something_to_search() -> None:
    with pytest.raises(QuestionError, match="at least one condition"):
        StructuredQuestion(
            text="q",
            format=QuestionFormat.PICO,
            components=(Component(ComponentRole.COMPARISON, "usual care"),),
        )


def test_question_needs_text_and_component_description() -> None:
    with pytest.raises(QuestionError, match="question text"):
        StructuredQuestion(text="", format=QuestionFormat.SPC, components=())
    with pytest.raises(QuestionError, match="description"):
        Component(ComponentRole.SITUATION, "  ")


def test_default_scope_is_most_sensitive_heading() -> None:
    assert Term.controlled("Aged").scope is ControlledScope.HEADING


def test_controlled_terms_default_to_mesh() -> None:
    assert Term.controlled("Anxiety").vocabulary is Vocabulary.MESH
    assert Term(text="Anxiety", kind=TermKind.CONTROLLED).vocabulary is Vocabulary.MESH
    assert Term.free("anxiety").vocabulary is None


def test_free_terms_have_no_vocabulary() -> None:
    with pytest.raises(QuestionError, match="cannot belong to a vocabulary"):
        Term(text="anxiety", kind=TermKind.FREE, vocabulary=Vocabulary.MESH)


def test_same_heading_in_two_vocabularies_is_not_a_repetition() -> None:
    condition = Condition(
        "reading",
        (
            Term.controlled("Reading"),
            Term.controlled("Reading", vocabulary=Vocabulary.ERIC),
        ),
    )
    assert condition.controlled_in("eric") == (condition.terms[1],)
    assert condition.controlled_in(Vocabulary.MESH) == (condition.terms[0],)


def _concept(description: str, *terms: Term) -> Component:
    return Component(ComponentRole.CONCEPT, description, (Condition(description, terms),))


def test_concepts_repeat_their_role_and_keep_input_order() -> None:
    question = StructuredQuestion(
        "Does peer tutoring improve reading?",
        QuestionFormat.CONCEPTS,
        (_concept("tutoring", Term.free("tutor*")), _concept("reading", Term.free("reading"))),
        discipline=Discipline.EDUCATION,
    )
    assert [c.label for c in question.conditions] == ["tutoring", "reading"]


def test_concept_role_does_not_belong_to_spc() -> None:
    with pytest.raises(QuestionError, match="does not belong to the spc format"):
        StructuredQuestion("q", QuestionFormat.SPC, (_concept("x", Term.free("x")),))


def test_question_rejects_repeated_databases() -> None:
    with pytest.raises(QuestionError, match="database can be chosen only once"):
        StructuredQuestion(
            "q",
            QuestionFormat.CONCEPTS,
            (_concept("x", Term.free("x")),),
            databases=(Database.PUBMED, Database.PUBMED),
        )


@pytest.mark.parametrize(
    "category", ["cs.LG", "quant-ph", "cond-mat.str-el", "q-fin.ST", "math.*", "physics.bio-ph"]
)
def test_question_accepts_arxiv_categories(category: str) -> None:
    question = StructuredQuestion(
        "q", QuestionFormat.CONCEPTS, (_concept("x", Term.free("x")),), arxiv_categories=(category,)
    )
    assert question.arxiv_categories == (category,)


@pytest.mark.parametrize(
    ("categories", "message"),
    [
        (("cs LG",), "not an arXiv category"),
        (("cs.LG OR ti:x",), "not an arXiv category"),
        (("CS.LG",), "not an arXiv category"),
        (("",), "not an arXiv category"),
        (("cs.LG", "cs.LG"), "category can be given only once"),
    ],
)
def test_question_rejects_bad_arxiv_categories(categories: tuple[str, ...], message: str) -> None:
    with pytest.raises(QuestionError, match=message):
        StructuredQuestion(
            "q",
            QuestionFormat.CONCEPTS,
            (_concept("x", Term.free("x")),),
            arxiv_categories=categories,
        )


def test_question_lists_controlled_terms_of_conditions_and_exclusions() -> None:
    animals = Term.controlled("Animals")
    question = StructuredQuestion(
        "q",
        QuestionFormat.CONCEPTS,
        (_concept("x", Term.controlled("Anxiety"), Term.free("x")),),
        exclusions=(Condition("animals", (animals,)),),
    )
    assert question.controlled_terms == (Term.controlled("Anxiety"), animals)
