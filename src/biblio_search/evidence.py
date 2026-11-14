"""Levels of evidence in health: Haynes' pyramid applied to a search (Del Pino, 2024).

To find the best evidence available on a topic that is not new, Del Pino looks first for
clinical practice guidelines, then for systematic reviews and only then for original
studies; on a new topic there has been no time for guidelines or reviews, so the search goes
straight to original studies. The ladder counts the question at each level in PubMed, where
publication types are indexed, so the user knows where to start.

The pyramid has more levels (computerised decision support systems, synopses of syntheses
and of studies), but they live in secondary resources, not in the free databases searched
here, so they are not part of the ladder.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from biblio_search.models import EvidenceLevel, StrategyOptions, StructuredQuestion
from biblio_search.pipeline import SearchBackend
from biblio_search.pubmed_query import PubMedDialect

# Orden de búsqueda de Del Pino (2024, figura 2): de lo más elaborado a lo original.
EVIDENCE_LADDER: tuple[EvidenceLevel, ...] = (
    EvidenceLevel.GUIDELINES,
    EvidenceLevel.SYSTEMATIC_REVIEWS,
    EvidenceLevel.ORIGINAL_STUDIES,
)
EVIDENCE_LABELS: dict[EvidenceLevel, str] = {
    EvidenceLevel.GUIDELINES: "Clinical practice guidelines",
    EvidenceLevel.SYSTEMATIC_REVIEWS: "Systematic reviews and meta-analyses",
    EvidenceLevel.ORIGINAL_STUDIES: "Original studies",
}


@dataclass(frozen=True, slots=True)
class LevelCount:
    """How many records of one level of evidence the question finds, with its equation."""

    level: EvidenceLevel
    equation: str
    count: int

    @property
    def label(self) -> str:
        return EVIDENCE_LABELS[self.level]


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """The question counted at every level of the ladder in PubMed.

    ``recommended`` is the highest level with records (``None`` when no level has any) and
    ``advice`` says why to start there.
    """

    equation: str
    total: int
    levels: tuple[LevelCount, ...]
    recommended: EvidenceLevel | None
    advice: str


def recommend(levels: tuple[LevelCount, ...]) -> tuple[EvidenceLevel | None, str]:
    """Pick the highest level of the ladder with records and explain the choice."""
    counts = {item.level: item.count for item in levels}
    guidelines = counts.get(EvidenceLevel.GUIDELINES, 0)
    reviews = counts.get(EvidenceLevel.SYSTEMATIC_REVIEWS, 0)
    originals = counts.get(EvidenceLevel.ORIGINAL_STUDIES, 0)
    if guidelines:
        return EvidenceLevel.GUIDELINES, (
            f"Start with the {guidelines:,} guidelines: prefer those based on systematic "
            "reviews, which state the level of evidence of every recommendation."
        )
    if reviews:
        return EvidenceLevel.SYSTEMATIC_REVIEWS, (
            f"There are no guidelines: start with the {reviews:,} systematic reviews, the "
            "studies with the most scientific evidence."
        )
    if originals:
        return EvidenceLevel.ORIGINAL_STUDIES, (
            f"There are no guidelines or systematic reviews (the topic may be too new for "
            f"them): search the {originals:,} original studies."
        )
    return None, "The question finds no records at any level: widen the search first."


async def evidence_ladder(
    question: StructuredQuestion,
    options: StrategyOptions,
    backend: SearchBackend,
) -> EvidenceReport:
    """Count the question in PubMed without a level filter and at every level of the ladder.

    Only counts are asked for (``retmax=0``). A level already chosen in ``options`` is
    ignored: the ladder is what helps choose it.
    """
    dialect = PubMedDialect()
    plain = replace(options, evidence_level=None)
    equation = dialect.render(question, plain)
    total = (await backend.search(equation, retmax=0)).count
    levels: list[LevelCount] = []
    for level in EVIDENCE_LADDER:
        filtered = dialect.render(question, replace(plain, evidence_level=level))
        count = (await backend.search(filtered, retmax=0)).count
        levels.append(LevelCount(level, filtered, count))
    found = tuple(levels)
    recommended, advice = recommend(found)
    return EvidenceReport(equation, total, found, recommended, advice)
