from __future__ import annotations

from pathlib import Path

from biblio_search.arxiv_query import ArxivDialect, render_term, render_text
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
from biblio_search.protocols import QueryDialect
from biblio_search.question_file import load_question

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _question(
    *conditions: Condition,
    exclusions: tuple[Condition, ...] = (),
    categories: tuple[str, ...] = (),
) -> StructuredQuestion:
    return StructuredQuestion(
        text="q",
        format=QuestionFormat.CONCEPTS,
        components=(Component(ComponentRole.CONCEPT, "concept", conditions),),
        exclusions=exclusions,
        arxiv_categories=categories,
    )


def test_render_text_keeps_words_bare_and_quotes_the_rest() -> None:
    assert render_text(Term.free("decoder")) == "decoder"
    assert render_text(Term.free("decod*")) == "decod*"
    assert render_text(Term.free("surface code")) == '"surface code"'
    # Sin comillas arXiv parte la palabra por el guion.
    assert render_text(Term.free("self-care")) == '"self-care"'
    assert render_text(Term.free("graph (theory)")) == '"graph (theory)"'
    assert render_text(Term.free("AND")) == '"AND"'


def test_truncated_phrase_drops_the_wildcard() -> None:
    assert render_text(Term.free("surface code*")) == '"surface code"'


def test_render_term_searches_title_and_abstract_or_title_only() -> None:
    assert render_term(Term.free("qubit*")) == "ti:qubit* OR abs:qubit*"
    assert render_term(Term.free("qubit", FreeTextField.TITLE)) == "ti:qubit"


def test_dialect_satisfies_protocol_and_indexes_no_thesaurus() -> None:
    dialect: QueryDialect = ArxivDialect()
    assert dialect.name == "arxiv"
    assert dialect.vocabularies == frozenset()


def test_example_equation_with_categories() -> None:
    question, options = load_question(EXAMPLES / "neural-decoders-qec.toml")
    equation = ArxivDialect().render(question, options)
    assert equation.startswith('(ti:"neural network" OR abs:"neural network" OR ')
    assert " AND (ti:decod* OR abs:decod*) AND (cat:quant-ph OR cat:cs.LG)" in equation
    assert equation.endswith("(cat:quant-ph OR cat:cs.LG)")


def test_headings_are_searched_as_free_text_and_exclusions_with_andnot() -> None:
    question = _question(
        Condition("tutoring", (Term.controlled("Tutoring", vocabulary=Vocabulary.ERIC),)),
        exclusions=(Condition("animals", (Term.controlled("Animals"),)),),
    )
    assert ArxivDialect().render(question, StrategyOptions()) == (
        "(ti:tutoring OR abs:tutoring) ANDNOT (ti:animals OR abs:animals)"
    )


def test_categories_come_before_exclusions() -> None:
    question = _question(
        Condition("x", (Term.free("qubit"),)),
        exclusions=(Condition("y", (Term.free("review", FreeTextField.TITLE),)),),
        categories=("quant-ph",),
    )
    assert ArxivDialect().render(question, StrategyOptions()) == (
        "(ti:qubit OR abs:qubit) AND (cat:quant-ph) ANDNOT (ti:review)"
    )


def test_options_do_not_change_the_equation() -> None:
    question, _ = load_question(EXAMPLES / "caregiver-burden.toml")
    dialect = ArxivDialect()
    sensitive = StrategyOptions(repeat_controlled_as_free=True, free_text_unindexed_only=True)
    assert dialect.render(question, sensitive) == dialect.render(question, StrategyOptions())


def test_notes_list_truncated_phrases_only() -> None:
    question = _question(Condition("x", (Term.free("surface code*"), Term.free("decod*"))))
    (note,) = ArxivDialect().notes(question)
    assert note.endswith(": surface code*")
    plain = _question(Condition("x", (Term.free("decod*"),)))
    assert ArxivDialect().notes(plain) == ()
