from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from biblio_search.catalog import SEARCHABLE
from biblio_search.models import ArticleSummary, Database, SearchResult
from biblio_search.pipeline import SearchReport
from biblio_search.question_file import parse_question_text
from biblio_search.screening import (
    MAX_REASON_LENGTH,
    NO_REASON,
    Decision,
    ScreeningError,
    ScreeningStore,
    merged_entries,
    render_flow,
)
from biblio_search.search_log import SearchLog, build_log
from biblio_search.selection import select_databases

QUESTION = """
question = "Does peer tutoring improve reading?"
format = "concepts"

[[components]]
description = "Peer tutoring"

[[components.conditions]]
label = "peer tutoring"
free = ["peer tutor*"]
"""
WHEN = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _record(
    record_id: str, *, doi: str | None = None, pmid: str | None = None, title: str = "A study"
) -> ArticleSummary:
    return ArticleSummary(
        record_id=record_id,
        title=title,
        authors=("Doe J", "Roe R"),
        journal="Journal",
        year=2024,
        doi=doi,
        publication_types=("Journal Article",),
        url=f"https://example.org/{record_id}",
        pmid=pmid,
    )


def _log(pubmed: Sequence[ArticleSummary], openalex: Sequence[ArticleSummary] = ()) -> SearchLog:
    question, options = parse_question_text(QUESTION)
    selection = select_databases(question, SEARCHABLE, [Database.PUBMED, Database.OPENALEX])
    reports = [
        (
            database,
            SearchReport(
                "peer tutoring[tiab]",
                SearchResult(len(records), tuple(r.record_id for r in records), ""),
                tuple(records),
            ),
        )
        for database, records in ((Database.PUBMED, pubmed), (Database.OPENALEX, openalex))
    ]
    return build_log(question, options, selection, reports, max_records=20, searched_at=WHEN)


@pytest.fixture
def store(tmp_path: Path) -> ScreeningStore:
    opened = ScreeningStore.open(tmp_path / "screening.sqlite")
    return opened


def test_add_search_keeps_merged_records_in_order(store: ScreeningStore) -> None:
    log = _log(
        [_record("1", doi="10.1/a", pmid="1", title="First"), _record("2", title="Second")],
        [_record("W1", doi="10.1/A", title="First again"), _record("W3", title="Third")],
    )
    added = store.add_search(log)
    assert (added.search_number, added.identified, added.new_records) == (1, 4, 3)
    entries = store.entries()
    assert [e.record.title for e in entries] == ["First", "Second", "Third"]
    first = entries[0]
    assert [s.database for s in first.sources] == [Database.PUBMED, Database.OPENALEX]
    assert first.record.record_id == "1" and first.record.url == "https://example.org/1"
    assert first.record.authors == ("Doe J", "Roe R")
    assert first.decision is None and first.decided_at is None
    store.close()


def test_a_second_search_only_adds_new_works(store: ScreeningStore) -> None:
    store.add_search(_log([_record("1", pmid="1"), _record("2")]))
    # El mismo trabajo vuelve por su PMID (desde OpenAlex) y por su id en PubMed.
    added = store.add_search(
        _log([_record("2"), _record("5")], [_record("W9", doi="10.9/z", pmid="1")])
    )
    assert (added.search_number, added.identified, added.new_records) == (2, 3, 1)
    assert added.already_known == 2
    entries = store.entries()
    assert len(entries) == 3
    assert entries[0].record.doi == "10.9/z", "the DOI learned later is kept"
    summary = store.summary()
    assert (summary.searches, summary.identified, summary.screened) == (2, 5, 3)
    assert summary.duplicates == 2
    store.close()


def test_decisions_are_kept_and_counted(tmp_path: Path) -> None:
    path = tmp_path / "screening.sqlite"
    with ScreeningStore.open(path) as store:
        store.add_search(_log([_record(str(n)) for n in range(1, 6)]))
        ids = [e.entry_id for e in store.entries()]
        store.decide(ids[0], Decision.INCLUDE)
        store.decide(ids[1], Decision.EXCLUDE, "  wrong   population ")
        store.decide(ids[2], Decision.EXCLUDE, "wrong population")
        store.decide(ids[3], Decision.EXCLUDE)
        changed = store.decide(ids[4], Decision.MAYBE, "abstract missing")
        assert changed.reason == "abstract missing" and changed.decided_at is not None
    # Reanudar: otro proceso abre el mismo fichero y lo encuentra todo.
    with ScreeningStore.open(path) as store:
        assert store.entry(ids[1]).reason == "wrong population"
        summary = store.summary()
        assert (summary.included, summary.excluded, summary.maybe) == (1, 3, 1)
        assert summary.pending == 0 and summary.decided == 5
        assert summary.exclusion_reasons == (("wrong population", 2), (NO_REASON, 1))
        assert [e.entry_id for e in store.entries("exclude")] == ids[1:4]
        assert [e.entry_id for e in store.entries("include")] == ids[:1]
        store.undecide(ids[4])
        assert [e.entry_id for e in store.entries("pending")] == [ids[4]]
        store.decide(ids[0], Decision.EXCLUDE, "not a trial")
        assert store.summary().included == 0


def test_unknown_records_and_long_reasons_are_refused(store: ScreeningStore) -> None:
    store.add_search(_log([_record("1")]))
    with pytest.raises(ScreeningError, match="no record 99"):
        store.decide(99, Decision.INCLUDE)
    with pytest.raises(ScreeningError, match="no record 99"):
        store.undecide(99)
    with pytest.raises(ScreeningError, match="no record 99"):
        store.entry(99)
    (entry,) = store.entries()
    with pytest.raises(ScreeningError, match="at most"):
        store.decide(entry.entry_id, Decision.EXCLUDE, "x" * (MAX_REASON_LENGTH + 1))
    store.close()


def test_a_file_that_is_not_a_screening_is_refused(tmp_path: Path) -> None:
    text = tmp_path / "notes.sqlite"
    text.write_text("just some notes, not a database\n" * 20, encoding="utf-8")
    with pytest.raises(ScreeningError, match="not a biblio-search screening file"):
        ScreeningStore.open(text)
    assert text.read_text(encoding="utf-8").startswith("just some notes")


def test_a_missing_folder_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ScreeningError, match="cannot open"):
        ScreeningStore.open(tmp_path / "missing" / "screening.sqlite")


def test_search_logs_and_export_entries(store: ScreeningStore) -> None:
    store.add_search(_log([_record("1", doi="10.1/a")]))
    (log,) = store.search_logs()
    assert log.startswith("# Search log")
    (merged,) = merged_entries(store.entries())
    assert merged.record.doi == "10.1/a" and merged.databases == (Database.PUBMED,)
    store.close()


def test_render_flow_lists_the_diagram_counts(store: ScreeningStore) -> None:
    store.add_search(
        _log([_record("1", pmid="1"), _record("2")], [_record("W1"), _record("W2", pmid="1")])
    )
    first, *_ = store.entries()
    store.decide(first.entry_id, Decision.EXCLUDE, "editorial")
    text = render_flow(store.summary())
    assert text.splitlines() == [
        "Searches added: 1",
        "Records identified: 4",
        "Duplicates removed: 1",
        "Records screened: 3",
        "  Pending: 2",
        "  Excluded: 1",
        "    editorial: 1",
        "  Maybe (full text needed): 0",
        "  Included: 0",
    ]
    store.close()
