"""Union of the records of several databases without duplicates.

Two records are the same work when they share a DOI or a PMID. Titles are not compared:
two different works can share a title (a protocol and its review, an erratum), and a wrong
merge hides a record from the screening while a missed one only shows it twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from biblio_search.models import ArticleSummary, Database

_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:")


@dataclass(frozen=True, slots=True)
class RecordSource:
    """Where a merged record was found: the database and its id and page there."""

    database: Database
    record_id: str
    url: str


@dataclass(frozen=True, slots=True)
class MergedRecord:
    """One work, as the first database that returned it describes it.

    ``record`` takes the DOI and PMID from any of its sources when the first one lacks them.
    """

    record: ArticleSummary
    sources: tuple[RecordSource, ...]

    @property
    def databases(self) -> tuple[Database, ...]:
        return tuple(dict.fromkeys(source.database for source in self.sources))


@dataclass(frozen=True, slots=True)
class MergedResults:
    """The records of every database, merged: what a flow diagram calls identified."""

    records: tuple[MergedRecord, ...]
    identified: int

    @property
    def duplicates(self) -> int:
        return self.identified - len(self.records)


def normalize_doi(doi: str) -> str:
    """A DOI as a comparable key: without its URL prefix and in lower case (DOIs ignore case)."""
    text = doi.strip()
    lowered = text.casefold()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip().casefold()


def _keys(record: ArticleSummary) -> tuple[str, ...]:
    keys: list[str] = []
    if record.doi and (doi := normalize_doi(record.doi)):
        keys.append(f"doi:{doi}")
    if record.pmid:
        keys.append(f"pmid:{record.pmid.strip()}")
    return tuple(keys)


@dataclass(slots=True)
class _Group:
    records: list[ArticleSummary]
    sources: list[RecordSource]


def merge_records(batches: Sequence[tuple[Database, Sequence[ArticleSummary]]]) -> MergedResults:
    """Merge the records of every database, in the given order of databases and records.

    A record joins the group of any earlier record that shares its DOI or its PMID; when it
    shares one with a group and the other with another (PubMed knows the PMID, OpenAlex the
    DOI and the PMID), both groups become one.
    """
    groups: dict[int, _Group] = {}
    owner: dict[str, int] = {}
    identified = 0
    for database, records in batches:
        for record in records:
            identified += 1
            source = RecordSource(database, record.record_id, record.url)
            keys = _keys(record)
            matches = sorted({owner[key] for key in keys if key in owner})
            if not matches:
                gid = identified
                groups[gid] = _Group([record], [source])
            else:
                gid = matches[0]
                groups[gid].records.append(record)
                groups[gid].sources.append(source)
                for other in matches[1:]:
                    absorbed = groups.pop(other)
                    groups[gid].records.extend(absorbed.records)
                    groups[gid].sources.extend(absorbed.sources)
                    for key, value in owner.items():
                        if value == other:
                            owner[key] = gid
            for key in keys:
                owner[key] = gid
    merged = tuple(_merged(group) for group in groups.values())
    return MergedResults(records=merged, identified=identified)


def _merged(group: _Group) -> MergedRecord:
    first = group.records[0]
    doi = next((r.doi for r in group.records if r.doi), None)
    pmid = next((r.pmid for r in group.records if r.pmid), None)
    return MergedRecord(record=replace(first, doi=doi, pmid=pmid), sources=tuple(group.sources))
