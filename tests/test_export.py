from __future__ import annotations

import csv
import io
from dataclasses import replace

from conftest import load_fixture

from biblio_search.eutils import parse_summaries
from biblio_search.export import CSV_COLUMNS, ris_type, to_csv, to_ris
from biblio_search.merge import MergedRecord, RecordSource, merge_records
from biblio_search.models import ArticleSummary, Database
from biblio_search.openalex import parse_work


def _merged() -> tuple[MergedRecord, ...]:
    pubmed = parse_summaries(load_fixture("esummary_caregiver.json"))
    works = load_fixture("openalex_works_caregiver.json")["results"]
    assert isinstance(works, list)
    openalex = tuple(map(parse_work, works))
    return merge_records([(Database.PUBMED, pubmed), (Database.OPENALEX, openalex)]).records


def _single(record: ArticleSummary) -> MergedRecord:
    return MergedRecord(record, (RecordSource(Database.OPENALEX, "W1", record.url),))


def test_ris_entries() -> None:
    records = _merged()
    text = to_ris(records)
    entries = [e for e in text.split("ER  - \r\n") if e.strip()]
    assert len(entries) == len(records)
    first = entries[0].splitlines()
    assert first[0] == "TY  - JOUR"
    assert "TI  - Music-Based Interventions for People Living With Dementia." in first
    assert "AU  - Mandolang E" in first
    assert "PY  - 2026" in first
    assert "DO  - 10.1097/NHH.0000000000001471" in first
    assert "DB  - PubMed" in first
    assert "N1  - Found in: PMID 42970524" in first
    assert all(line.endswith("\r\n") for line in text.splitlines(keepends=True))


def test_ris_lists_every_source_of_a_duplicate() -> None:
    pubmed = parse_summaries(load_fixture("esummary_caregiver.json"))[0]
    twin = replace(pubmed, record_id="W9", url="https://openalex.org/W9")
    (merged,) = merge_records([(Database.PUBMED, [pubmed]), (Database.OPENALEX, [twin])]).records
    text = to_ris([merged])
    assert "DB  - PubMed, OpenAlex" in text
    assert f"N1  - Found in: PMID {pubmed.record_id}; OpenAlex W9" in text


def test_ris_type_follows_the_publication_type() -> None:
    record = parse_work({"id": "https://openalex.org/W1", "type": "preprint"})
    assert ris_type(_single(record)) == "UNPB"
    assert ris_type(_single(replace(record, publication_types=("book-chapter",)))) == "CHAP"
    assert ris_type(_single(replace(record, publication_types=("Review",)))) == "JOUR"


def test_ris_values_stay_on_one_line_and_skip_empty_fields() -> None:
    record = parse_work({"id": "https://openalex.org/W1", "display_name": "Two\nlines"})
    text = to_ris([_single(record)])
    assert "TI  - Two lines" in text
    assert "PY  -" not in text and "DO  -" not in text and "T2  -" not in text


def test_csv_rows() -> None:
    records = _merged()
    text = to_csv(records)
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == len(records) + 1
    first = dict(zip(CSV_COLUMNS, rows[1], strict=True))
    assert first["authors"] == "Mandolang E; Brush P; Petrovsky DV"
    assert first["pmid"] == "42970524"
    assert first["databases"] == "PubMed"
    assert first["ids"] == "PMID 42970524"
    openalex = dict(zip(CSV_COLUMNS, rows[-1], strict=True))
    assert openalex["databases"] == "OpenAlex" and openalex["pmid"].isdigit()


def test_csv_neutralises_formulas() -> None:
    record = parse_work({"id": "https://openalex.org/W1", "display_name": "=HYPERLINK(1)"})
    rows = list(csv.reader(io.StringIO(to_csv([_single(record)]).removeprefix("﻿"))))
    assert rows[1][0] == "'=HYPERLINK(1)"
