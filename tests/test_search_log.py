from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import load_fixture

from biblio_search.catalog import DIALECTS, SEARCHABLE
from biblio_search.eutils import parse_search, parse_summaries
from biblio_search.models import Database, SearchResult, StrategyOptions
from biblio_search.openalex import parse_search as parse_openalex_search
from biblio_search.openalex import parse_work
from biblio_search.pipeline import SearchReport
from biblio_search.question_file import load_question, parse_question_text
from biblio_search.search_log import SearchLog, build_log, render_log, rerun_command
from biblio_search.selection import select_databases

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"
WHEN = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)


def _reports() -> list[tuple[Database, SearchReport]]:
    question, options = load_question(EXAMPLE)
    works = load_fixture("openalex_works_caregiver.json")["results"]
    assert isinstance(works, list)
    pubmed = SearchReport(
        equation=DIALECTS[Database.PUBMED].render(question, options),
        result=parse_search(load_fixture("esearch_caregiver.json")),
        records=parse_summaries(load_fixture("esummary_caregiver.json")),
    )
    openalex = SearchReport(
        equation=DIALECTS[Database.OPENALEX].render(question, options),
        result=parse_openalex_search(load_fixture("openalex_search_caregiver.json")),
        records=tuple(map(parse_work, works)),
    )
    return [(Database.PUBMED, pubmed), (Database.OPENALEX, openalex)]


def _log() -> SearchLog:
    question, options = load_question(EXAMPLE)
    selection = select_databases(question, SEARCHABLE)
    return build_log(question, options, selection, _reports(), max_records=3, searched_at=WHEN)


def test_build_log_merges_and_keeps_notes() -> None:
    log = _log()
    assert [run.database for run in log.runs] == [Database.PUBMED, Database.OPENALEX]
    assert log.merged.identified == 6
    assert log.searched_at == WHEN
    assert log.runs[1].notes, "OpenAlex explains how it searches [ti] terms"


def test_log_needs_a_time_zone() -> None:
    question, options = load_question(EXAMPLE)
    selection = select_databases(question, SEARCHABLE)
    with pytest.raises(ValueError, match="time zone"):
        build_log(question, options, selection, [], max_records=0, searched_at=datetime(2026, 1, 1))


def test_render_log_documents_everything() -> None:
    log = _log()
    text = render_log(log)
    assert text.startswith("# Search log\n")
    assert "- Date: 2026-11-08 09:30 UTC" in text
    assert f"- Question: {log.question.text}" in text
    assert "Chosen automatically by fixed rules." in text
    for run in log.runs:
        assert run.report.equation in text
        assert f"- Records found: {run.report.result.count:,}" in text
    assert "## PubMed" in text and "## OpenAlex" in text
    assert "How PubMed understood it:" in text
    assert "- Retrieved: 6 (PubMed 3, OpenAlex 3)" in text
    assert "- Unique records: 6" in text
    assert rerun_command(log) in text


def test_render_log_converts_to_utc() -> None:
    question, options = load_question(EXAMPLE)
    selection = select_databases(question, SEARCHABLE)
    madrid = datetime(2026, 11, 8, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    log = build_log(question, options, selection, [], max_records=0, searched_at=madrid)
    assert "- Date: 2026-11-08 09:30 UTC" in render_log(log)


def test_the_question_in_the_log_reads_back_the_same() -> None:
    log = _log()
    text = render_log(log)
    toml = text.split("```\n")[-2]
    assert tomllib.loads(toml)
    question, options = parse_question_text(toml)
    assert (question, options) == (log.question, log.options)


def test_rerun_command_names_the_searched_databases() -> None:
    assert rerun_command(_log()) == (
        "biblio-search search question.toml --database pubmed --database openalex --max 3"
    )


def test_not_searched_databases_and_remarks_are_listed() -> None:
    question, _options = load_question(EXAMPLE)
    selection = select_databases(question, SEARCHABLE, [Database.PUBMED, Database.ERIC])
    result = SearchResult(
        count=0, ids=(), query_translation="", not_found=("qwz",), warnings=("odd",)
    )
    report = SearchReport(equation="qwz[tiab]", result=result, records=())
    log = build_log(
        question,
        StrategyOptions(repeat_controlled_as_free=True),
        selection,
        [(Database.PUBMED, report)],
        max_records=20,
        searched_at=WHEN,
    )
    text = render_log(log)
    assert "Chosen by hand." in text
    assert "- ERIC (not searched)" in text
    assert "- Not found: qwz" in text and "- Warning: odd" in text
    assert "How PubMed understood it:" not in text
    assert "- Headings repeated as free text: yes" in text


def test_fence_outgrows_backticks_in_the_equation() -> None:
    question, options = load_question(EXAMPLE)
    selection = select_databases(question, SEARCHABLE, [Database.PUBMED])
    result = SearchResult(count=1, ids=(), query_translation="")
    report = SearchReport(equation="a ``` b", result=result, records=())
    log = build_log(
        question, options, selection, [(Database.PUBMED, report)], max_records=0, searched_at=WHEN
    )
    assert "````\na ``` b\n````" in render_log(log)
