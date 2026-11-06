"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from biblio_search import __version__
from biblio_search.check import QuestionCheck, apply_check, check_question, scope_note
from biblio_search.eutils import MAX_RETMAX, EutilsClient, EutilsConfig, EutilsError
from biblio_search.mesh import MeshThesaurus
from biblio_search.models import (
    ArticleSummary,
    Database,
    QuestionError,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermCheck,
    TermStatus,
    Vocabulary,
)
from biblio_search.pipeline import SearchReport, run_search
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect, foreign_terms
from biblio_search.question_file import load_question
from biblio_search.selection import DatabaseSelection, SelectionMode, select_databases

DIALECTS: dict[Database, QueryDialect] = {Database.PUBMED: PubMedDialect()}
# Bases con cliente: de momento solo se puede consultar PubMed.
SEARCHABLE = frozenset({Database.PUBMED})
VOCABULARY_LABELS = {Vocabulary.MESH.value: "MeSH", Vocabulary.ERIC.value: "ERIC"}
DATABASE_LABELS = {
    Database.PUBMED: "PubMed",
    Database.ERIC: "ERIC",
    Database.ARXIV: "arXiv",
    Database.OPENALEX: "OpenAlex",
}


def _max_records(value: str) -> int:
    number = int(value)
    if not 0 <= number <= MAX_RETMAX:
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_RETMAX}")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biblio-search",
        description="Build rigorous bibliographic search strategies from structured questions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    question = argparse.ArgumentParser(add_help=False)
    question.add_argument("question_file", type=Path, help="TOML file with the structured question")
    question.add_argument(
        "--repeat-controlled-as-free",
        action="store_true",
        help="also search every heading as free text (more sensitive)",
    )
    question.add_argument(
        "--unindexed-only",
        action="store_true",
        help="limit free text to records outside MEDLINE (more specific)",
    )

    choose = argparse.ArgumentParser(add_help=False)
    choose.add_argument(
        "--database",
        action="append",
        choices=[database.value for database in Database],
        dest="databases",
        default=[],
        help="search this database (repeatable); overrides the file and the automatic choice",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build", parents=[question], help="print the search equation of a question file"
    )
    build.add_argument(
        "--dialect", choices=[database.value for database in DIALECTS], default=Database.PUBMED
    )

    commands.add_parser(
        "databases",
        parents=[question, choose],
        help="show which databases the question is searched in, why, and their equations",
    )

    search = commands.add_parser(
        "search",
        parents=[question, choose],
        help="run the equation in PubMed and list the first records",
    )
    search.add_argument(
        "--max", type=_max_records, default=20, dest="max_records", help="records to list"
    )
    search.add_argument(
        "--email", help="contact email sent to NCBI (default: NCBI_EMAIL environment variable)"
    )

    check = commands.add_parser(
        "check",
        parents=[question],
        help="confirm the MeSH headings of a question and print the corrected equation",
    )
    check.add_argument(
        "--email", help="contact email sent to NCBI (default: NCBI_EMAIL environment variable)"
    )
    return parser


def _merge_options(file_options: StrategyOptions, args: argparse.Namespace) -> StrategyOptions:
    # Los flags de la CLI solo activan opciones; nunca desactivan lo que pide el fichero.
    return replace(
        file_options,
        repeat_controlled_as_free=file_options.repeat_controlled_as_free
        or bool(args.repeat_controlled_as_free),
        free_text_unindexed_only=file_options.free_text_unindexed_only or bool(args.unindexed_only),
    )


def format_record(position: int, record: ArticleSummary) -> str:
    year = str(record.year) if record.year is not None else "n.d."
    types = f" [{', '.join(record.publication_types)}]" if record.publication_types else ""
    return f"{position:>3}. PMID {record.pmid} ({year}) {record.title} | {record.journal}{types}"


def format_report(report: SearchReport) -> str:
    result = report.result
    lines = [
        f"Equation: {report.equation}",
        f"PubMed understood: {result.query_translation}",
        f"Results: {result.count}",
    ]
    lines += [f"Not found: {term}" for term in result.not_found]
    lines += [f"Warning: {message}" for message in result.warnings]
    if report.records:
        lines.append("")
        lines += [format_record(i, r) for i, r in enumerate(report.records, start=1)]
    return "\n".join(lines)


def format_term_check(check: TermCheck) -> str:
    where = f"[{check.condition}]"
    if check.entry is None:
        return f"  not found   {check.term.text} {where}: searched as free text instead"
    heading = f"{check.entry.name} ({check.entry.identifier})"
    if check.status is TermStatus.ENTRY_TERM:
        line = f"  entry term  {check.term.text} -> {heading} {where}"
    else:
        line = f"  ok          {heading} {where}"
    note = scope_note(check)
    return f"{line}\n              {note}" if note else line


def _heading_label(term: Term) -> str:
    vocabulary = term.vocabulary.value if term.vocabulary is not None else ""
    return f"{term.text} ({VOCABULARY_LABELS.get(vocabulary, vocabulary)})"


def format_check(result: QuestionCheck, equation: str) -> str:
    checks = result.checks
    label = VOCABULARY_LABELS.get(result.vocabulary, result.vocabulary)
    lines = [f"{label} check of {len(checks)} controlled terms"]
    if not checks:
        lines.append(f"  (the question has no {label} terms)")
    lines += [format_term_check(c) for c in checks]
    if result.unchecked:
        lines += ["", f"Not checked (biblio-search cannot check them against {label}):"]
        lines += [f"  {_heading_label(term)}" for term in result.unchecked]
    suggested = [item for item in result.conditions if item.suggested_synonyms]
    if suggested:
        lines += ["", "Synonyms you could add as free text:"]
        lines += [
            f"  {item.condition.label}: {', '.join(item.suggested_synonyms)}" for item in suggested
        ]
    lines += ["", f"Equation after the check: {equation}"]
    return "\n".join(lines)


async def _check(
    question: StructuredQuestion, config: EutilsConfig
) -> tuple[QuestionCheck, StructuredQuestion]:
    async with EutilsClient(config) as client:
        result = await check_question(question, MeshThesaurus(client))
    return result, apply_check(question, result)


def foreign_note(question: StructuredQuestion, dialect: QueryDialect) -> str | None:
    """Warn that some headings are searched as free text because the database lacks them."""
    foreign = foreign_terms(question, dialect.vocabularies)
    if not foreign:
        return None
    label = DATABASE_LABELS.get(Database(dialect.name), dialect.name)
    headings = ", ".join(_heading_label(term) for term in foreign)
    return f"{label} does not index these headings, so they are searched as free text: {headings}"


def format_selection(
    selection: DatabaseSelection,
    question: StructuredQuestion,
    options: StrategyOptions,
) -> str:
    how = "chosen by hand" if selection.mode is SelectionMode.MANUAL else "chosen automatically"
    lines = [f"Databases ({how}):"]
    for choice in selection.choices:
        label = DATABASE_LABELS[choice.database]
        state = "" if choice.searchable else " (biblio-search cannot search it yet)"
        lines.append(f"  {label}{state}")
        lines += [f"    - {reason}" for reason in choice.reasons]
    if not selection.searchable:
        lines += ["", "None of them can be searched yet; add --database pubmed to use PubMed."]
    for database in selection.searchable:
        dialect = DIALECTS[database]
        lines += ["", f"{DATABASE_LABELS[database]} equation: {dialect.render(question, options)}"]
        note = foreign_note(question, dialect)
        if note:
            lines.append(f"Note: {note}")
    return "\n".join(lines)


def _config(args: argparse.Namespace) -> EutilsConfig:
    config = EutilsConfig.from_env()
    return replace(config, email=args.email) if args.email else config


def _searched_in(selection: DatabaseSelection) -> str:
    pending = selection.pending
    if Database.PUBMED not in selection.searchable:
        names = ", ".join(DATABASE_LABELS[d] for d in pending)
        raise QuestionError(
            f"none of the chosen databases can be searched yet ({names}); "
            "add --database pubmed to search PubMed"
        )
    line = "Searching in: PubMed"
    if pending:
        line += (
            f" (also chosen, not searchable yet: {', '.join(DATABASE_LABELS[d] for d in pending)})"
        )
    return line


async def _search(
    question: StructuredQuestion, options: StrategyOptions, args: argparse.Namespace
) -> SearchReport:
    async with EutilsClient(_config(args)) as client:
        return await run_search(
            question, options, DIALECTS[Database.PUBMED], client, max_records=args.max_records
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        question, file_options = load_question(args.question_file)
        options = _merge_options(file_options, args)
        if args.command == "build":
            dialect = DIALECTS[Database(args.dialect)]
            print(dialect.render(question, options))
            note = foreign_note(question, dialect)
            if note:
                print(f"biblio-search: {note}", file=sys.stderr)
            return 0
        if args.command == "check":
            result, fixed = asyncio.run(_check(question, _config(args)))
            equation = DIALECTS[Database.PUBMED].render(fixed, options)
            print(format_check(result, equation))
            return 0
        requested = [Database(name) for name in args.databases]
        selection = select_databases(question, SEARCHABLE, requested)
        if args.command == "databases":
            print(format_selection(selection, question, options))
            return 0
        searched_in = _searched_in(selection)
        report = asyncio.run(_search(question, options, args))
        print(f"{searched_in}\n{format_report(report)}")
        return 0
    except QuestionError as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 2
    except (EutilsError, ValueError) as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
