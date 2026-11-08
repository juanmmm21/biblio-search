"""Documented record of a search, written so that anyone can repeat it.

Del Pino (2024) asks to document every search: when it was run, in which databases, with
which equation and how many records it returned. The log keeps all of that, how each
database understood the equation, and the question itself as TOML with the command that
runs it again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from biblio_search import __version__
from biblio_search.catalog import DATABASE_LABELS, DIALECTS, dialect_notes
from biblio_search.merge import MergedResults, merge_records
from biblio_search.models import Database, StrategyOptions, StructuredQuestion
from biblio_search.pipeline import SearchReport
from biblio_search.question_file import dump_question
from biblio_search.selection import DatabaseSelection, SelectionMode

QUESTION_FILE_NAME = "question.toml"


@dataclass(frozen=True, slots=True)
class DatabaseRun:
    """What one database returned and what the user must know about its equation."""

    database: Database
    report: SearchReport
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchLog:
    """One run of a question in every chosen database, with its merged records."""

    searched_at: datetime
    question: StructuredQuestion
    options: StrategyOptions
    selection: DatabaseSelection
    runs: tuple[DatabaseRun, ...]
    max_records: int
    merged: MergedResults
    version: str = __version__

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is None:
            raise ValueError("the date of a search must carry its time zone")


def build_log(
    question: StructuredQuestion,
    options: StrategyOptions,
    selection: DatabaseSelection,
    reports: Sequence[tuple[Database, SearchReport]],
    *,
    max_records: int,
    searched_at: datetime | None = None,
) -> SearchLog:
    """Gather the reports of a search, in database order, and merge their records."""
    runs = tuple(
        DatabaseRun(database, report, dialect_notes(question, DIALECTS[database]))
        for database, report in reports
    )
    return SearchLog(
        searched_at=searched_at or datetime.now(UTC),
        question=question,
        options=options,
        selection=selection,
        runs=runs,
        max_records=max_records,
        merged=merge_records([(run.database, run.report.records) for run in runs]),
    )


def _fence(text: str) -> str:
    # Una ecuación podría llevar acentos graves: la valla siempre es más larga que cualquiera.
    longest = 0
    run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def rerun_command(log: SearchLog) -> str:
    """The command line that repeats the search with the question saved as TOML."""
    parts = ["biblio-search", "search", QUESTION_FILE_NAME]
    for run in log.runs:
        parts += ["--database", run.database.value]
    parts += ["--max", str(log.max_records)]
    return " ".join(parts)


def _run_lines(run: DatabaseRun) -> list[str]:
    label = DATABASE_LABELS[run.database]
    result = run.report.result
    lines = [
        f"## {label}",
        "",
        f"- Records found: {result.count:,}",
        f"- Records retrieved: {len(run.report.records):,}",
        "",
        "Equation:",
        "",
        _fence(run.report.equation),
    ]
    if result.query_translation:
        lines += ["", f"How {label} understood it:", "", _fence(result.query_translation)]
    remarks = [f"- Not found: {term}" for term in result.not_found]
    remarks += [f"- Warning: {message}" for message in result.warnings]
    remarks += [f"- Note: {note}" for note in run.notes]
    if remarks:
        lines += ["", *remarks]
    return lines


def render_log(log: SearchLog) -> str:
    """The log as Markdown, readable as it is and easy to paste into a methods section."""
    question = log.question
    when = log.searched_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Search log",
        "",
        f"- Date: {when}",
        f"- Tool: biblio-search {log.version}",
        f"- Question: {question.text}",
        f"- Format: {question.format.value.upper()}",
    ]
    if question.discipline is not None:
        lines.append(f"- Discipline: {question.discipline.value.replace('_', ' ')}")
    lines += [
        f"- Headings repeated as free text: {_yes_no(log.options.repeat_controlled_as_free)}",
        f"- Free text limited to records outside MEDLINE: "
        f"{_yes_no(log.options.free_text_unindexed_only)}",
        "",
        "## Databases",
        "",
        "Chosen by hand."
        if log.selection.mode is SelectionMode.MANUAL
        else "Chosen automatically by fixed rules.",
        "",
    ]
    searched = {run.database for run in log.runs}
    for choice in log.selection.choices:
        state = "" if choice.database in searched else " (not searched)"
        reasons = "; ".join(choice.reasons)
        detail = f": {reasons}" if reasons else ""
        lines.append(f"- {DATABASE_LABELS[choice.database]}{state}{detail}")
    for run in log.runs:
        lines += ["", *_run_lines(run)]
    merged = log.merged
    per_database = ", ".join(
        f"{DATABASE_LABELS[run.database]} {len(run.report.records):,}" for run in log.runs
    )
    lines += [
        "",
        "## Records",
        "",
        f"- Retrieved: {merged.identified:,} ({per_database})",
        f"- Duplicates removed (same DOI or PMID): {merged.duplicates:,}",
        f"- Unique records: {len(merged.records):,}",
        "",
        "## Repeat this search",
        "",
        f"Save the question below as `{QUESTION_FILE_NAME}` and run:",
        "",
        _fence(rerun_command(log)),
        "",
        _fence(dump_question(question, log.options).rstrip("\n")),
        "",
    ]
    return "\n".join(lines)
