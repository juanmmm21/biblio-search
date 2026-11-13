"""Snowballing: the references and the citations of the included records, through OpenAlex.

Del Pino's sixth step: once some records are included, the works they cite (backwards) and
the works that cite them (forwards) are candidates the equation may have missed. OpenAlex
knows both for every discipline, so each included record is first found there by its
OpenAlex id, its DOI or its PMID. A record with none of them (ERIC records carry no DOI) is
left out and reported: matching it by title could pick another work with the same title.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from biblio_search import __version__
from biblio_search.merge import MergedRecord, MergedResults, RecordSource, normalize_doi
from biblio_search.models import ArticleSummary, Database
from biblio_search.openalex import MAX_RESULTS, CitingWorks, WorkLinks

if TYPE_CHECKING:
    # Solo como tipo: el cribado importa este módulo para guardar las rondas.
    from biblio_search.screening import ScreeningEntry

DEFAULT_MAX_CITING = 100
MAX_CITING = MAX_RESULTS


class SnowballDirection(StrEnum):
    """Which way to follow the citations of an included record."""

    REFERENCES = "references"
    CITATIONS = "citations"


ALL_DIRECTIONS = (SnowballDirection.REFERENCES, SnowballDirection.CITATIONS)


class SnowballSource(Protocol):
    """What snowballing needs from OpenAlex."""

    async def links(
        self,
        *,
        work_ids: Sequence[str] = (),
        dois: Sequence[str] = (),
        pmids: Sequence[str] = (),
    ) -> tuple[WorkLinks, ...]: ...

    async def summaries(
        self, ids: Sequence[str], *, missing_ok: bool = False
    ) -> tuple[ArticleSummary, ...]: ...

    async def citing(self, work_id: str, *, limit: int) -> CitingWorks: ...


@dataclass(frozen=True, slots=True)
class SnowballSeed:
    """An included record found in OpenAlex, with how many works it led to."""

    entry_id: int
    title: str
    work_id: str
    references: int
    citing_total: int
    citing_kept: int


@dataclass(frozen=True, slots=True)
class UnmatchedSeed:
    """An included record that could not be found in OpenAlex, and why."""

    entry_id: int
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A work reached by snowballing and the included records that led to it.

    ``cited_by`` lists the records whose references hold it, ``cites`` those it cites.
    """

    record: ArticleSummary
    cited_by: tuple[int, ...] = ()
    cites: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SnowballRun:
    """One round of snowballing from a set of included records."""

    run_at: datetime
    directions: tuple[SnowballDirection, ...]
    max_citing: int
    seeds: tuple[SnowballSeed, ...]
    unmatched: tuple[UnmatchedSeed, ...]
    candidates: tuple[Candidate, ...]
    missing_references: int = 0
    version: str = __version__

    def __post_init__(self) -> None:
        if self.run_at.tzinfo is None:
            raise ValueError("the date of a snowballing round must carry its time zone")

    @property
    def merged(self) -> MergedResults:
        """The candidates as merged records from OpenAlex, ready for the screening."""
        records = tuple(
            MergedRecord(
                record=candidate.record,
                sources=(
                    RecordSource(
                        Database.OPENALEX, candidate.record.record_id, candidate.record.url
                    ),
                ),
            )
            for candidate in self.candidates
        )
        return MergedResults(records=records, identified=len(records))


@dataclass(slots=True)
class _Reached:
    record: ArticleSummary | None = None
    cited_by: list[int] = field(default_factory=list)
    cites: list[int] = field(default_factory=list)


def _openalex_id(entry: ScreeningEntry) -> str | None:
    return next((s.record_id for s in entry.sources if s.database is Database.OPENALEX), None)


def _usable_doi(entry: ScreeningEntry) -> str | None:
    doi = normalize_doi(entry.record.doi) if entry.record.doi else ""
    return doi if doi and "|" not in doi and "," not in doi else None


def _usable_pmid(entry: ScreeningEntry) -> str | None:
    pmid = (entry.record.pmid or "").strip()
    return pmid if pmid.isdigit() else None


def _match(entry: ScreeningEntry, works: Sequence[WorkLinks]) -> WorkLinks | None:
    work_id = _openalex_id(entry)
    doi = _usable_doi(entry)
    pmid = _usable_pmid(entry)
    for work in works:
        if work_id is not None and work.work_id == work_id:
            return work
    for work in works:
        if doi is not None and work.doi is not None and normalize_doi(work.doi) == doi:
            return work
    for work in works:
        if pmid is not None and work.pmid == pmid:
            return work
    return None


async def snowball(
    source: SnowballSource,
    seeds: Sequence[ScreeningEntry],
    *,
    directions: Sequence[SnowballDirection] = ALL_DIRECTIONS,
    max_citing: int = DEFAULT_MAX_CITING,
    run_at: datetime | None = None,
) -> SnowballRun:
    """Follow the references and/or citations of ``seeds`` and gather the works reached.

    Citations are capped at ``max_citing`` per record, most cited first: a classic paper
    can have thousands. The seeds themselves are never candidates.
    """
    if not directions:
        raise ValueError("choose at least one direction")
    if not 1 <= max_citing <= MAX_CITING:
        raise ValueError(f"max_citing must be between 1 and {MAX_CITING}")
    chosen = tuple(dict.fromkeys(directions))
    work_ids = [i for e in seeds if (i := _openalex_id(e)) is not None]
    dois = [d for e in seeds if _openalex_id(e) is None and (d := _usable_doi(e)) is not None]
    pmids = [
        p
        for e in seeds
        if _openalex_id(e) is None and _usable_doi(e) is None and (p := _usable_pmid(e)) is not None
    ]
    works = await source.links(
        work_ids=list(dict.fromkeys(work_ids)),
        dois=list(dict.fromkeys(dois)),
        pmids=list(dict.fromkeys(pmids)),
    )
    matched: list[tuple[ScreeningEntry, WorkLinks]] = []
    unmatched: list[UnmatchedSeed] = []
    for entry in seeds:
        work = _match(entry, works)
        if work is not None:
            matched.append((entry, work))
            continue
        known = _openalex_id(entry) or _usable_doi(entry) or _usable_pmid(entry)
        reason = (
            "OpenAlex does not know its identifiers"
            if known
            else "it has no DOI, PMID or OpenAlex id to find it by"
        )
        unmatched.append(UnmatchedSeed(entry.entry_id, entry.record.title, reason))

    seed_works = {work.work_id for _, work in matched}
    reached: dict[str, _Reached] = {}
    citing: dict[int, CitingWorks] = {}
    for entry, work in matched:
        if SnowballDirection.REFERENCES in chosen:
            for ref in work.referenced:
                if ref not in seed_works:
                    reached.setdefault(ref, _Reached()).cited_by.append(entry.entry_id)
        if SnowballDirection.CITATIONS in chosen:
            found = await source.citing(work.work_id, limit=max_citing)
            citing[entry.entry_id] = found
            for record in found.records:
                if record.record_id not in seed_works:
                    item = reached.setdefault(record.record_id, _Reached())
                    item.cites.append(entry.entry_id)
                    item.record = item.record or record

    to_fetch = [work_id for work_id, item in reached.items() if item.record is None]
    for record in await source.summaries(to_fetch, missing_ok=True):
        reached[record.record_id].record = record
    candidates = tuple(
        Candidate(item.record, tuple(item.cited_by), tuple(item.cites))
        for item in reached.values()
        if item.record is not None
    )
    seed_runs = tuple(
        SnowballSeed(
            entry_id=entry.entry_id,
            title=entry.record.title,
            work_id=work.work_id,
            references=len(work.referenced) if SnowballDirection.REFERENCES in chosen else 0,
            citing_total=citing[entry.entry_id].total if entry.entry_id in citing else 0,
            citing_kept=len(citing[entry.entry_id].records) if entry.entry_id in citing else 0,
        )
        for entry, work in matched
    )
    return SnowballRun(
        run_at=run_at or datetime.now(UTC),
        directions=chosen,
        max_citing=max_citing,
        seeds=seed_runs,
        unmatched=tuple(unmatched),
        candidates=candidates,
        missing_references=len(reached) - len(candidates),
    )


def render_snowball(run: SnowballRun) -> str:
    """The round as Markdown, to document it next to the search logs."""
    when = run.run_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    followed = " and ".join(direction.value for direction in run.directions)
    lines = [
        "# Snowballing log",
        "",
        f"- Date: {when}",
        f"- Tool: biblio-search {run.version}",
        "- Database: OpenAlex",
        f"- Followed: {followed}",
    ]
    if SnowballDirection.CITATIONS in run.directions:
        lines.append(f"- Citing works kept per record: {run.max_citing:,} (most cited first)")
    lines += ["", "## Included records followed", ""]
    if not run.seeds:
        lines.append("None could be found in OpenAlex.")
    for seed in run.seeds:
        parts = []
        if SnowballDirection.REFERENCES in run.directions:
            parts.append(f"{seed.references:,} references")
        if SnowballDirection.CITATIONS in run.directions:
            parts.append(f"{seed.citing_kept:,} of {seed.citing_total:,} citing works")
        lines.append(f"- #{seed.entry_id} {seed.title} ({seed.work_id}): {', '.join(parts)}")
    if run.unmatched:
        lines += ["", "## Included records left out", ""]
        lines += [f"- #{u.entry_id} {u.title}: {u.reason}" for u in run.unmatched]
    lines += ["", "## Records", "", f"- Candidates reached: {len(run.candidates):,}"]
    if run.missing_references:
        lines.append(
            f"- References OpenAlex no longer has (merged or withdrawn): {run.missing_references:,}"
        )
    lines.append("")
    return "\n".join(lines)
