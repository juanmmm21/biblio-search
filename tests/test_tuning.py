from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from biblio_search.models import (
    ArticleSummary,
    ControlledScope,
    FreeTextField,
    SearchResult,
    StrategyOptions,
    StructuredQuestion,
    TermKind,
)
from biblio_search.openalex_query import OpenAlexDialect
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question, parse_question
from biblio_search.tuning import (
    AdviceKind,
    ConditionCount,
    Direction,
    VariantCount,
    advise,
    condition_alone,
    tune,
    variants,
    without_condition,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"


class CountingBackend:
    """Answers every count with a number taken from the equation, and records the calls."""

    def __init__(self, counts: Mapping[str, int] | None = None) -> None:
        self.counts = counts or {}
        self.asked: list[tuple[str, int]] = []

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        self.asked.append((term, retmax))
        return SearchResult(count=self.counts.get(term, len(term)), ids=(), query_translation="")

    async def summaries(self, ids: Sequence[str], /) -> tuple[ArticleSummary, ...]:
        raise AssertionError("tuning only counts")


def _example() -> tuple[StructuredQuestion, StrategyOptions]:
    return load_question(EXAMPLE)


def test_condition_alone_and_without_condition() -> None:
    question, options = _example()
    burden = question.conditions[3]
    alone = condition_alone(question, burden)
    assert alone.conditions == (burden,) and alone.exclusions == ()
    assert PubMedDialect().render(alone, options) == (
        "(burden*[tiab] OR overload*[tiab] OR strain*[tiab])"
    )
    reduced = without_condition(question, 1)
    assert [c.label for c in reduced.conditions] == [
        "anxiety or depression",
        "older people",
        "burden",
    ]
    assert reduced.text == question.text and reduced.format == question.format


def test_variants_move_one_lever_each() -> None:
    question, options = _example()
    found = {v.key: v for v in variants(question, options)}
    assert set(found) == {"repeat", "unindexed_only", "major", "any_topic", "explode", "title_only"}
    assert found["repeat"].direction is Direction.SENSITIVE
    assert found["repeat"].options.repeat_controlled_as_free
    assert found["repeat"].question == question
    scopes = [t.scope for t in found["major"].question.controlled_terms]
    assert (
        scopes
        == [ControlledScope.MAJOR_NO_EXPLODE, ControlledScope.MAJOR] + [ControlledScope.MAJOR] * 2
    )
    assert all(
        t.scope in (ControlledScope.HEADING, ControlledScope.HEADING_NO_EXPLODE)
        for t in found["any_topic"].question.controlled_terms
    )
    assert found["explode"].question.controlled_terms[0].scope is ControlledScope.MAJOR
    free = [
        t
        for c in found["title_only"].question.conditions
        for t in c.terms
        if t.kind is TermKind.FREE
    ]
    assert {t.free_field for t in free} == {FreeTextField.TITLE}


def test_variants_go_back_when_a_lever_is_already_pulled() -> None:
    question, _ = _example()
    options = StrategyOptions(repeat_controlled_as_free=True, free_text_unindexed_only=True)
    keys = {v.key for v in variants(question, options)}
    assert {"no_repeat", "indexed_too"} <= keys
    assert not {"repeat", "unindexed_only"} & keys


def test_tune_counts_conditions_and_variants_in_pubmed() -> None:
    question, options = _example()
    backend = CountingBackend()
    report = asyncio.run(tune(question, options, PubMedDialect(), backend))
    equation = PubMedDialect().render(question, options)
    assert report.database == "pubmed"
    assert report.equation == equation and report.total == len(equation)
    assert [c.label for c in report.conditions] == [c.label for c in question.conditions]
    assert all(c.without is not None for c in report.conditions)
    assert report.conditions[3].equation == "(burden*[tiab] OR overload*[tiab] OR strain*[tiab])"
    assert report.without_exclusions is None
    assert {v.key for v in report.variants} == {
        "repeat",
        "unindexed_only",
        "major",
        "any_topic",
        "explode",
        "title_only",
    }
    # Solo recuentos, y ninguna ecuación se manda dos veces.
    assert all(retmax == 0 for _, retmax in backend.asked)
    sent = [term for term, _ in backend.asked]
    assert len(sent) == len(set(sent)) == 1 + 4 + 4 + 6


def test_tune_in_openalex_drops_levers_it_does_not_have() -> None:
    question, options = _example()
    report = asyncio.run(tune(question, options, OpenAlexDialect(), CountingBackend()))
    # OpenAlex no tiene MeSH ni MEDLINE y busca siempre en título y resumen: nada que comparar.
    assert report.variants == ()
    assert len(report.conditions) == 4


def test_tune_with_one_condition_and_exclusions() -> None:
    question, options = parse_question(
        {
            "question": "q",
            "format": "concepts",
            "components": [
                {"description": "tutoring", "conditions": [{"label": "t", "free": ["tutor*"]}]}
            ],
            "exclusions": [{"label": "online", "free": ["online"]}],
        }
    )
    counts = {"(tutor*[tiab]) NOT (online[tiab])": 40, "(tutor*[tiab])": 55}
    report = asyncio.run(tune(question, options, PubMedDialect(), CountingBackend(counts)))
    assert report.total == 40
    assert report.conditions == (ConditionCount("t", "(tutor*[tiab])", 55, None),)
    assert report.without_exclusions == 55
    assert [a.kind for a in report.advice] == [AdviceKind.TOO_SPECIFIC]


def _conditions() -> tuple[ConditionCount, ...]:
    return (
        ConditionCount("anxiety", "(a)", 900_000, 60),
        ConditionCount("caregivers", "(c)", 80_000, 300),
        ConditionCount("burden", "(b)", 150_000, 45),
    )


def _variants() -> tuple[VariantCount, ...]:
    return (
        VariantCount("repeat", "repeat", Direction.SENSITIVE, "r", 70),
        VariantCount("any_topic", "any topic", Direction.SENSITIVE, "m", 95),
        VariantCount("major", "major", Direction.SPECIFIC, "j", 20),
        VariantCount("title_only", "title", Direction.SPECIFIC, "t", 35),
        VariantCount("explode", "explode", Direction.SENSITIVE, "e", 40),
    )


def test_few_records_suggest_the_widest_levers_and_the_tightest_condition() -> None:
    advice = advise(40, _conditions(), _variants())
    assert [a.kind for a in advice] == [AdviceKind.TOO_SPECIFIC]
    assert advice[0].message.startswith("Fewer than 100 records")
    assert advice[0].suggestions == (
        "any topic: 95 records (+55)",
        "repeat: 70 records (+30)",
        "«caregivers» cuts the most (300 records without it): add synonyms or truncation to "
        "it, or check that it is essential",
    )


def test_enough_records_need_no_advice_until_the_user_screens_them() -> None:
    assert advise(150, _conditions(), _variants()) == ()
    advice = advise(150, _conditions(), _variants(), none_relevant=True)
    assert [a.kind for a in advice] == [AdviceKind.TOO_SENSITIVE]
    assert advice[0].suggestions == (
        "major: 20 records (-130)",
        "title: 35 records (-115)",
        "«anxiety» is the least selective condition (900,000 records on its own): narrow its terms",
        "add a condition the relevant records must meet",
    )


def test_no_records_points_at_the_empty_condition_or_the_combination() -> None:
    empty = (ConditionCount("x", "(x)", 0, 12), ConditionCount("y", "(y)", 50, 0))
    advice = advise(0, empty, ())
    assert advice[0].kind is AdviceKind.NO_RESULTS
    assert advice[0].suggestions == ("«x» finds nothing on its own: check its terms",)
    apart = (ConditionCount("x", "(x)", 10, 12), ConditionCount("y", "(y)", 50, 3))
    advice = advise(0, apart, ())
    assert advice[0].suggestions == (
        "the conditions find records apart but not together; without «x» there are 12",
    )
    # Sin resultados no hay nada que cribar: el veredicto del usuario no añade nada.
    assert [a.kind for a in advise(0, apart, (), none_relevant=True)] == [AdviceKind.NO_RESULTS]
