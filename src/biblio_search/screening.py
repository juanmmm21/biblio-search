"""Screening of the records a search found, kept in a local SQLite file.

A screening project gathers the records of one or more searches, merged across databases
and across searches, and keeps one decision per record (include, exclude or maybe) with
its reason. Everything is written as soon as it happens, so screening can stop at any
record and resume later from the same file. The counts of a PRISMA-like flow diagram
(identified, duplicates, screened, excluded, included) come from the same file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from biblio_search.merge import MergedRecord, RecordSource, identity_keys
from biblio_search.models import ArticleSummary, Database
from biblio_search.question_file import dump_question
from biblio_search.search_log import SearchLog, render_log

SCHEMA_VERSION = 1
DEFAULT_SCREENING_FILE = "screening.sqlite"
MAX_REASON_LENGTH = 500
NO_REASON = "no reason given"

EntryFilter = Literal["all", "pending", "include", "exclude", "maybe"]

_SCHEMA = """
CREATE TABLE searches (
    id INTEGER PRIMARY KEY,
    searched_at TEXT NOT NULL,
    added_at TEXT NOT NULL,
    question TEXT NOT NULL,
    question_toml TEXT NOT NULL,
    log TEXT NOT NULL,
    identified INTEGER NOT NULL,
    new_records INTEGER NOT NULL
);
CREATE TABLE records (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    journal TEXT NOT NULL,
    year INTEGER,
    doi TEXT,
    pmid TEXT,
    publication_types TEXT NOT NULL,
    first_search INTEGER NOT NULL REFERENCES searches(id)
);
CREATE TABLE record_keys (
    key TEXT PRIMARY KEY,
    record INTEGER NOT NULL REFERENCES records(id)
);
CREATE TABLE sources (
    record INTEGER NOT NULL REFERENCES records(id),
    database TEXT NOT NULL,
    record_id TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (database, record_id)
);
CREATE TABLE decisions (
    record INTEGER PRIMARY KEY REFERENCES records(id),
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'maybe')),
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


class ScreeningError(Exception):
    """The screening file cannot be used, or a decision refers to an unknown record."""


class Decision(StrEnum):
    """What the reviewer decided about a record after reading its title and abstract."""

    INCLUDE = "include"
    EXCLUDE = "exclude"
    MAYBE = "maybe"


@dataclass(frozen=True, slots=True)
class ScreeningEntry:
    """One record of the project, where it was found and what was decided about it."""

    entry_id: int
    record: ArticleSummary
    sources: tuple[RecordSource, ...]
    decision: Decision | None = None
    reason: str = ""
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AddedSearch:
    """What adding a search changed: its records that were new and those already known."""

    search_number: int
    identified: int
    new_records: int

    @property
    def already_known(self) -> int:
        return self.identified - self.new_records


@dataclass(frozen=True, slots=True)
class FlowSummary:
    """The counts of a flow diagram of the screening.

    ``identified`` adds up every record every search retrieved, and ``duplicates`` those
    that turned out to be a work already found (in another database or another search), so
    ``screened`` (the unique records) is the difference. ``exclusion_reasons`` counts the
    excluded records by reason, most frequent first.
    """

    searches: int
    identified: int
    screened: int
    included: int
    excluded: int
    maybe: int
    exclusion_reasons: tuple[tuple[str, int], ...]

    @property
    def duplicates(self) -> int:
        return self.identified - self.screened

    @property
    def decided(self) -> int:
        return self.included + self.excluded + self.maybe

    @property
    def pending(self) -> int:
        return self.screened - self.decided


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _source_key(source: RecordSource) -> str:
    return f"{source.database.value}:{source.record_id}"


class ScreeningStore:
    """A screening project in a SQLite file. Close it (or use ``with``) when done."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open the project in ``path``, creating it when the file does not exist yet."""
        exists = path.exists()
        try:
            connection = sqlite3.connect(path)
        except sqlite3.Error as exc:
            raise ScreeningError(f"cannot open {path}: {exc}") from exc
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            if version == 0 and tables == 0:
                with connection:
                    connection.executescript(_SCHEMA)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ScreeningError(f"{path} is not a biblio-search screening file")
        except sqlite3.DatabaseError as exc:
            connection.close()
            # Un fichero que no es SQLite da «file is not a database» al primer acceso.
            raise ScreeningError(f"{path} is not a biblio-search screening file") from exc
        except ScreeningError:
            connection.close()
            if not exists:
                path.unlink(missing_ok=True)
            raise
        return cls(connection, path)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._db:
                yield self._db
        except sqlite3.Error as exc:
            raise ScreeningError(f"cannot write {self.path}: {exc}") from exc

    def add_search(self, log: SearchLog) -> AddedSearch:
        """Add the merged records of a search; works already in the project are not repeated.

        A record is the same work as one already kept when it shares its DOI, its PMID or
        its id in a database. Two records already kept are never fused, even if a new
        record links them: each may carry a decision, and the reviewer must not lose one.
        """
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT INTO searches (searched_at, added_at, question, question_toml, log,"
                " identified, new_records) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    log.searched_at.isoformat(timespec="seconds"),
                    _now(),
                    log.question.text,
                    dump_question(log.question, log.options),
                    render_log(log),
                    log.merged.identified,
                ),
            )
            search = cursor.lastrowid
            if search is None:
                raise ScreeningError(f"cannot write {self.path}: the search got no id")
            new_records = sum(self._add_record(db, search, merged) for merged in log.merged.records)
            db.execute("UPDATE searches SET new_records = ? WHERE id = ?", (new_records, search))
        number = self._db.execute("SELECT count(*) FROM searches").fetchone()[0]
        return AddedSearch(number, log.merged.identified, new_records)

    def _add_record(self, db: sqlite3.Connection, search: int, merged: MergedRecord) -> bool:
        record = merged.record
        keys = [*identity_keys(record), *(_source_key(s) for s in merged.sources)]
        placeholders = ", ".join("?" for _ in keys)
        found = db.execute(
            f"SELECT min(record) FROM record_keys WHERE key IN ({placeholders})", keys
        ).fetchone()[0]
        if found is None:
            cursor = db.execute(
                "INSERT INTO records (title, authors, journal, year, doi, pmid,"
                " publication_types, first_search) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.title,
                    json.dumps(list(record.authors)),
                    record.journal,
                    record.year,
                    record.doi,
                    record.pmid,
                    json.dumps(list(record.publication_types)),
                    search,
                ),
            )
            entry: int | None = cursor.lastrowid
            if entry is None:
                raise ScreeningError(f"cannot write {self.path}: the record got no id")
        else:
            entry = int(found)
            # Otra base puede saber el DOI o el PMID que le faltaba al registro guardado.
            db.execute(
                "UPDATE records SET doi = coalesce(doi, ?), pmid = coalesce(pmid, ?) WHERE id = ?",
                (record.doi, record.pmid, entry),
            )
        db.executemany(
            "INSERT OR IGNORE INTO record_keys (key, record) VALUES (?, ?)",
            [(key, entry) for key in keys],
        )
        db.executemany(
            "INSERT OR IGNORE INTO sources (record, database, record_id, url) VALUES (?, ?, ?, ?)",
            [(entry, s.database.value, s.record_id, s.url) for s in merged.sources],
        )
        return found is None

    def decide(self, entry_id: int, decision: Decision, reason: str = "") -> ScreeningEntry:
        """Record (or change) the decision about a record, with an optional reason."""
        reason = " ".join(reason.split())
        if len(reason) > MAX_REASON_LENGTH:
            raise ScreeningError(f"a reason can have at most {MAX_REASON_LENGTH} characters")
        self._require(entry_id)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO decisions (record, decision, reason, decided_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (record) DO UPDATE SET decision = excluded.decision,"
                " reason = excluded.reason, decided_at = excluded.decided_at",
                (entry_id, decision.value, reason, _now()),
            )
        return self.entry(entry_id)

    def undecide(self, entry_id: int) -> ScreeningEntry:
        """Take back the decision about a record, which goes back to pending."""
        self._require(entry_id)
        with self._transaction() as db:
            db.execute("DELETE FROM decisions WHERE record = ?", (entry_id,))
        return self.entry(entry_id)

    def _require(self, entry_id: int) -> None:
        row = self._db.execute("SELECT 1 FROM records WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ScreeningError(f"there is no record {entry_id} in this screening")

    def entry(self, entry_id: int) -> ScreeningEntry:
        entries = self._select("WHERE r.id = ?", (entry_id,))
        if not entries:
            raise ScreeningError(f"there is no record {entry_id} in this screening")
        return entries[0]

    def entries(self, which: EntryFilter = "all") -> tuple[ScreeningEntry, ...]:
        """The records in the order they were found, all of them or those in one state."""
        if which == "all":
            return self._select("", ())
        if which == "pending":
            return self._select("WHERE d.decision IS NULL", ())
        return self._select("WHERE d.decision = ?", (Decision(which).value,))

    def _select(self, where: str, params: Sequence[object]) -> tuple[ScreeningEntry, ...]:
        rows = self._db.execute(
            "SELECT r.id, r.title, r.authors, r.journal, r.year, r.doi, r.pmid,"
            " r.publication_types, d.decision, d.reason, d.decided_at"
            f" FROM records r LEFT JOIN decisions d ON d.record = r.id {where} ORDER BY r.id",
            params,
        ).fetchall()
        sources: dict[int, list[RecordSource]] = {}
        with closing(
            self._db.execute("SELECT record, database, record_id, url FROM sources ORDER BY rowid")
        ) as cursor:
            for record, database, record_id, url in cursor:
                sources.setdefault(record, []).append(
                    RecordSource(Database(database), record_id, url)
                )
        return tuple(self._entry(row, sources.get(row[0], [])) for row in rows)

    @staticmethod
    def _entry(row: tuple[object, ...], sources: list[RecordSource]) -> ScreeningEntry:
        entry_id, title, authors, journal, year, doi, pmid, types, decision, reason, at = row
        first = sources[0] if sources else None
        record = ArticleSummary(
            record_id=first.record_id if first else "",
            title=str(title),
            authors=tuple(json.loads(str(authors))),
            journal=str(journal),
            year=int(str(year)) if year is not None else None,
            doi=str(doi) if doi is not None else None,
            publication_types=tuple(json.loads(str(types))),
            url=first.url if first else "",
            pmid=str(pmid) if pmid is not None else None,
        )
        return ScreeningEntry(
            entry_id=int(str(entry_id)),
            record=record,
            sources=tuple(sources),
            decision=Decision(str(decision)) if decision is not None else None,
            reason=str(reason) if reason is not None else "",
            decided_at=datetime.fromisoformat(str(at)) if at is not None else None,
        )

    def summary(self) -> FlowSummary:
        db = self._db
        searches, identified = db.execute(
            "SELECT count(*), coalesce(sum(identified), 0) FROM searches"
        ).fetchone()
        screened = db.execute("SELECT count(*) FROM records").fetchone()[0]
        counts = dict(db.execute("SELECT decision, count(*) FROM decisions GROUP BY decision"))
        reasons = db.execute(
            "SELECT CASE WHEN reason = '' THEN ? ELSE reason END AS label, count(*) AS n"
            " FROM decisions WHERE decision = 'exclude' GROUP BY label ORDER BY n DESC, label",
            (NO_REASON,),
        ).fetchall()
        return FlowSummary(
            searches=int(searches),
            identified=int(identified),
            screened=int(screened),
            included=int(counts.get(Decision.INCLUDE.value, 0)),
            excluded=int(counts.get(Decision.EXCLUDE.value, 0)),
            maybe=int(counts.get(Decision.MAYBE.value, 0)),
            exclusion_reasons=tuple((str(label), int(n)) for label, n in reasons),
        )

    def search_logs(self) -> tuple[str, ...]:
        """The Markdown log of every search added, oldest first, to document the screening."""
        rows = self._db.execute("SELECT log FROM searches ORDER BY id").fetchall()
        return tuple(str(row[0]) for row in rows)


def merged_entries(entries: Sequence[ScreeningEntry]) -> tuple[MergedRecord, ...]:
    """The entries as merged records, to export them with the RIS and CSV writers."""
    return tuple(MergedRecord(record=e.record, sources=e.sources) for e in entries)


def render_flow(summary: FlowSummary) -> str:
    """The flow diagram counts as plain text, in the order of a PRISMA diagram."""
    lines = [
        f"Searches added: {summary.searches:,}",
        f"Records identified: {summary.identified:,}",
        f"Duplicates removed: {summary.duplicates:,}",
        f"Records screened: {summary.screened:,}",
        f"  Pending: {summary.pending:,}",
        f"  Excluded: {summary.excluded:,}",
    ]
    lines += [f"    {reason}: {count:,}" for reason, count in summary.exclusion_reasons]
    lines += [
        f"  Maybe (full text needed): {summary.maybe:,}",
        f"  Included: {summary.included:,}",
    ]
    return "\n".join(lines)
