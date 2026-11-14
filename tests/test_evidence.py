from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_tuning import CountingBackend

from biblio_search.catalog import DIALECTS, EXPORT_DIALECTS, dialect_notes, export_notes
from biblio_search.evidence import (
    EVIDENCE_LADDER,
    LevelCount,
    evidence_ladder,
    recommend,
)
from biblio_search.models import Database, EvidenceLevel, ExportTarget, StrategyOptions
from biblio_search.pubmed_query import PubMedDialect, apply_evidence_level
from biblio_search.question_file import load_question

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"


def test_apply_evidence_level_wraps_the_equation() -> None:
    equation = "a[tiab] NOT b[tiab]"
    assert apply_evidence_level(equation, None) == equation
    assert apply_evidence_level(equation, EvidenceLevel.GUIDELINES) == (
        "(a[tiab] NOT b[tiab]) AND (Guideline[pt] OR Practice Guideline[pt])"
    )
    assert apply_evidence_level(equation, EvidenceLevel.SYSTEMATIC_REVIEWS) == (
        "(a[tiab] NOT b[tiab]) AND systematic[sb]"
    )
    originals = apply_evidence_level(equation, EvidenceLevel.ORIGINAL_STUDIES)
    assert originals.startswith("(a[tiab] NOT b[tiab]) NOT (systematic[sb] OR Review[pt] OR ")
    assert "Editorial[pt]" in originals and "Letter[pt]" in originals


def test_pubmed_dialect_applies_the_level_after_the_exclusions() -> None:
    question, options = load_question(EXAMPLE)
    plain = PubMedDialect().render(question, options)
    chosen = StrategyOptions(evidence_level=EvidenceLevel.SYSTEMATIC_REVIEWS)
    assert PubMedDialect().render(question, chosen) == f"({plain}) AND systematic[sb]"


def test_other_databases_ignore_the_level_and_say_so() -> None:
    question, _ = load_question(EXAMPLE)
    chosen = StrategyOptions(evidence_level=EvidenceLevel.GUIDELINES)
    for database in (Database.OPENALEX, Database.ERIC, Database.ARXIV):
        dialect = DIALECTS[database]
        assert dialect.render(question, chosen) == dialect.render(question, StrategyOptions())
        notes = dialect_notes(question, dialect, chosen)
        assert any("filters PubMed only" in note for note in notes)
    pubmed = dialect_notes(question, DIALECTS[Database.PUBMED], chosen)
    assert not any("filters PubMed only" in note for note in pubmed)
    for target in ExportTarget:
        assert "[pt]" not in EXPORT_DIALECTS[target].render(question, chosen)
        assert any("filters PubMed only" in n for n in export_notes(question, target, chosen))
    assert not any(
        "filters PubMed only" in n
        for n in dialect_notes(question, DIALECTS[Database.OPENALEX], StrategyOptions())
    )


def _levels(guidelines: int, reviews: int, originals: int) -> tuple[LevelCount, ...]:
    counts = (guidelines, reviews, originals)
    return tuple(LevelCount(level, "", n) for level, n in zip(EVIDENCE_LADDER, counts, strict=True))


@pytest.mark.parametrize(
    ("counts", "level", "phrase"),
    [
        ((2, 71, 2625), EvidenceLevel.GUIDELINES, "Start with the 2 guidelines"),
        ((0, 71, 2625), EvidenceLevel.SYSTEMATIC_REVIEWS, "start with the 71 systematic"),
        ((0, 0, 1234), EvidenceLevel.ORIGINAL_STUDIES, "search the 1,234 original studies"),
        ((0, 0, 0), None, "finds no records at any level"),
    ],
)
def test_recommend_takes_the_highest_level_with_records(
    counts: tuple[int, int, int], level: EvidenceLevel | None, phrase: str
) -> None:
    recommended, advice = recommend(_levels(*counts))
    assert recommended is level
    assert phrase in advice


def test_evidence_ladder_counts_every_level_ignoring_the_chosen_one() -> None:
    question, options = load_question(EXAMPLE)
    dialect = PubMedDialect()
    plain = dialect.render(question, options)
    guidelines = apply_evidence_level(plain, EvidenceLevel.GUIDELINES)
    backend = CountingBackend({plain: 2816, guidelines: 0})
    chosen = StrategyOptions(evidence_level=EvidenceLevel.ORIGINAL_STUDIES)

    report = asyncio.run(evidence_ladder(question, chosen, backend))

    assert report.equation == plain and report.total == 2816
    assert [item.level for item in report.levels] == list(EVIDENCE_LADDER)
    assert report.levels[0].count == 0
    assert report.levels[0].label == "Clinical practice guidelines"
    assert report.recommended is EvidenceLevel.SYSTEMATIC_REVIEWS
    assert backend.asked[0] == (plain, 0)
    assert all(retmax == 0 for _, retmax in backend.asked)
    assert len(backend.asked) == 4
