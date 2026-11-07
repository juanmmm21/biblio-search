"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from pathlib import Path

from biblio_search import __version__
from biblio_search.catalog import (
    DATABASE_LABELS,
    DIALECTS,
    RECORD_ID_LABELS,
    SEARCHABLE,
    VOCABULARY_LABELS,
    dialect_notes,
    heading_label,
)
from biblio_search.check import QuestionCheck, apply_check, check_question, scope_note
from biblio_search.decs import (
    LICENCE_URL,
    DecsClient,
    DecsConfig,
    DecsMeshThesaurus,
    DecsSuggestion,
    suggest_mesh,
)
from biblio_search.eutils import MAX_RETMAX, EutilsClient, EutilsConfig
from biblio_search.mesh import MeshThesaurus
from biblio_search.models import (
    ArticleSummary,
    Database,
    DatabaseError,
    QuestionError,
    StrategyOptions,
    StructuredQuestion,
    TermCheck,
    TermStatus,
)
from biblio_search.openalex import OpenAlexClient, OpenAlexConfig
from biblio_search.pipeline import SearchBackend, SearchReport, run_search
from biblio_search.question_file import load_question, question_to_data
from biblio_search.selection import DatabaseSelection, SelectionMode, select_databases
from biblio_search.tuning import SCREENING_WINDOW, Direction, TuningReport, tune


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
        help="run the equation in every chosen database and list the first records",
    )
    search.add_argument(
        "--max", type=_max_records, default=20, dest="max_records", help="records to list"
    )
    search.add_argument(
        "--email", help="contact email sent to NCBI (default: NCBI_EMAIL environment variable)"
    )

    tune_parser = commands.add_parser(
        "tune",
        parents=[question, choose],
        help="count each condition and each sensitivity/specificity lever, and get advice",
    )
    tune_parser.add_argument(
        "--none-relevant",
        action="store_true",
        help=f"none of the first {SCREENING_WINDOW} records was relevant (asks how to narrow)",
    )
    tune_parser.add_argument(
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
    decs = commands.add_parser(
        "decs",
        help="find the MeSH descriptors for a Spanish health term through DeCS "
        "(needs DECS_API_KEY)",
    )
    decs.add_argument("words", nargs="+", help="Spanish term, e.g. cuidadores familiares")
    decs.add_argument(
        "--email", help="contact email sent to NCBI (default: NCBI_EMAIL environment variable)"
    )
    ui = commands.add_parser(
        "ui", help="open the web interface in the browser (local only, nobody else can reach it)"
    )
    ui.add_argument("question_file", type=Path, nargs="?", help="TOML question to open (optional)")
    ui.add_argument(
        "--port", type=_port, default=0, help="port on 127.0.0.1 (default: any free port)"
    )
    ui.add_argument(
        "--no-browser", action="store_true", help="print the address instead of opening it"
    )
    ui.add_argument(
        "--email", help="contact email sent to NCBI (default: NCBI_EMAIL environment variable)"
    )
    return parser


def _port(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return number


def _ui(args: argparse.Namespace) -> int:
    # Importación tardía: la CLI de siempre no carga FastAPI ni uvicorn si no hace falta.
    from biblio_search.web import serve

    initial = None
    if args.question_file is not None:
        initial = question_to_data(*load_question(args.question_file))
    try:
        serve(
            _config(args),
            port=args.port,
            open_browser=not args.no_browser,
            initial_question=initial,
        )
    except OSError as exc:
        raise ValueError(f"cannot start the web interface: {exc.strerror or exc}") from exc
    return 0


def _merge_options(file_options: StrategyOptions, args: argparse.Namespace) -> StrategyOptions:
    # Los flags de la CLI solo activan opciones; nunca desactivan lo que pide el fichero.
    return replace(
        file_options,
        repeat_controlled_as_free=file_options.repeat_controlled_as_free
        or bool(args.repeat_controlled_as_free),
        free_text_unindexed_only=file_options.free_text_unindexed_only or bool(args.unindexed_only),
    )


def format_record(position: int, record: ArticleSummary, id_label: str = "PMID") -> str:
    year = str(record.year) if record.year is not None else "n.d."
    types = f" [{', '.join(record.publication_types)}]" if record.publication_types else ""
    journal = f" | {record.journal}" if record.journal else ""
    return f"{position:>3}. {id_label} {record.record_id} ({year}) {record.title}{journal}{types}"


def format_report(database: Database, report: SearchReport) -> str:
    result = report.result
    label = DATABASE_LABELS[database]
    lines = [label, f"Equation: {report.equation}"]
    if result.query_translation:
        lines.append(f"{label} understood: {result.query_translation}")
    lines.append(f"Results: {result.count}")
    lines += [f"Not found: {term}" for term in result.not_found]
    lines += [f"Warning: {message}" for message in result.warnings]
    if report.records:
        id_label = RECORD_ID_LABELS[database]
        lines.append("")
        lines += [format_record(i, r, id_label) for i, r in enumerate(report.records, start=1)]
    return "\n".join(lines)


def format_term_check(check: TermCheck) -> str:
    where = f"[{check.condition}]"
    if check.entry is None:
        return f"  not found   {check.term.text} {where}: searched as free text instead"
    heading = f"{check.entry.name} ({check.entry.identifier})"
    if check.status is TermStatus.ENTRY_TERM:
        line = f"  entry term  {check.term.text} -> {heading} {where}"
    elif check.status is TermStatus.TRANSLATED:
        line = f"  in Spanish  {check.term.text} -> {heading} {where}: found through DeCS"
    else:
        line = f"  ok          {heading} {where}"
    note = scope_note(check)
    return f"{line}\n              {note}" if note else line


_DECS_HINT = (
    f"Spanish terms are looked up in DeCS when DECS_API_KEY is set (free licence: {LICENCE_URL})."
)


def format_check(result: QuestionCheck, equation: str, *, decs: bool = False) -> str:
    checks = result.checks
    label = VOCABULARY_LABELS.get(result.vocabulary, result.vocabulary)
    lines = [f"{label} check of {len(checks)} controlled terms"]
    if not checks:
        lines.append(f"  (the question has no {label} terms)")
    lines += [format_term_check(c) for c in checks]
    if result.unchecked:
        lines += ["", f"Not checked (biblio-search cannot check them against {label}):"]
        lines += [f"  {heading_label(term)}" for term in result.unchecked]
    suggested = [item for item in result.conditions if item.suggested_synonyms]
    if suggested:
        lines += ["", "Synonyms you could add as free text:"]
        lines += [
            f"  {item.condition.label}: {', '.join(item.suggested_synonyms)}" for item in suggested
        ]
    lines += ["", f"Equation after the check: {equation}"]
    missing = any(c.status is TermStatus.NOT_FOUND for c in checks)
    if missing and not decs and result.vocabulary == MeshThesaurus.name:
        lines += ["", _DECS_HINT]
    return "\n".join(lines)


async def _check(
    question: StructuredQuestion, config: EutilsConfig, decs_config: DecsConfig | None
) -> tuple[QuestionCheck, StructuredQuestion]:
    async with EutilsClient(config) as client:
        mesh = MeshThesaurus(client)
        if decs_config is None:
            result = await check_question(question, mesh)
        else:
            async with DecsClient(decs_config) as decs:
                result = await check_question(question, DecsMeshThesaurus(mesh, decs))
    return result, apply_check(question, result)


def format_suggestion(suggestion: DecsSuggestion) -> str:
    descriptor = suggestion.descriptor
    match = "exact" if suggestion.exact else "related"
    spanish = descriptor.spanish[0] if descriptor.spanish else descriptor.english
    if suggestion.entry is None:
        return (
            f"  {match:<8}{spanish} -> {descriptor.english} ({descriptor.mesh_id}): "
            "not confirmed in MeSH, not used"
        )
    return f"  {match:<8}{spanish} -> {suggestion.entry.name} ({suggestion.entry.identifier})"


def format_suggestions(text: str, suggestions: Sequence[DecsSuggestion]) -> str:
    if not suggestions:
        return f"DeCS has no MeSH descriptor for {text!r}"
    lines = [f"MeSH descriptors for {text!r} (through DeCS, confirmed in MeSH):"]
    lines += [format_suggestion(s) for s in suggestions]
    return "\n".join(lines)


def _decs_config() -> DecsConfig:
    config = DecsConfig.from_env()
    if config is None:
        raise QuestionError(f"DeCS needs a licence key in DECS_API_KEY (free: {LICENCE_URL})")
    return config


async def _suggest(
    text: str, config: EutilsConfig, decs_config: DecsConfig
) -> tuple[DecsSuggestion, ...]:
    async with EutilsClient(config) as client, DecsClient(decs_config) as decs:
        return await suggest_mesh(text, decs, MeshThesaurus(client))


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
        lines += ["", f"None of them can be searched yet; {_SEARCHABLE_HINT}."]
    for database in selection.searchable:
        dialect = DIALECTS[database]
        lines += ["", f"{DATABASE_LABELS[database]} equation: {dialect.render(question, options)}"]
        lines += [f"Note: {note}" for note in dialect_notes(question, dialect)]
    return "\n".join(lines)


_SEARCHABLE_HINT = "add --database pubmed or --database openalex to search one that can be"


def _config(args: argparse.Namespace) -> EutilsConfig:
    config = EutilsConfig.from_env()
    return replace(config, email=args.email) if args.email else config


def _searched_in(selection: DatabaseSelection) -> str:
    pending = ", ".join(DATABASE_LABELS[d] for d in selection.pending)
    if not selection.searchable:
        raise QuestionError(
            f"none of the chosen databases can be searched yet ({pending}); {_SEARCHABLE_HINT}"
        )
    line = f"Searching in: {', '.join(DATABASE_LABELS[d] for d in selection.searchable)}"
    if pending:
        line += f" (also chosen, not searchable yet: {pending})"
    return line


def _open_backend(
    database: Database, args: argparse.Namespace
) -> AbstractAsyncContextManager[SearchBackend]:
    if database is Database.PUBMED:
        return EutilsClient(_config(args))
    if database is Database.OPENALEX:
        return OpenAlexClient(OpenAlexConfig.from_env())
    raise QuestionError(f"biblio-search cannot search {DATABASE_LABELS[database]} yet")


async def _search(
    question: StructuredQuestion,
    options: StrategyOptions,
    databases: Sequence[Database],
    args: argparse.Namespace,
) -> list[tuple[Database, SearchReport]]:
    reports: list[tuple[Database, SearchReport]] = []
    for database in databases:
        async with _open_backend(database, args) as backend:
            report = await run_search(
                question, options, DIALECTS[database], backend, max_records=args.max_records
            )
        reports.append((database, report))
    return reports


_DIRECTION_LABELS = {Direction.SENSITIVE: "more sensitive", Direction.SPECIFIC: "more specific"}


def format_tuning(database: Database, report: TuningReport, *, none_relevant: bool) -> str:
    lines = [f"{DATABASE_LABELS[database]}: {report.total:,} records", f"  {report.equation}"]
    lines += ["", "Conditions (records on its own / total without it):"]
    for item in report.conditions:
        without = "" if item.without is None else f" / {item.without:,} without it"
        lines.append(f"  {item.label}: {item.alone:,} on its own{without}")
    if report.without_exclusions is not None:
        lines.append(f"  without the NOT conditions: {report.without_exclusions:,}")
    if report.variants:
        lines += ["", "Levers, one at a time:"]
        ordered = sorted(report.variants, key=lambda v: v.direction is not Direction.SENSITIVE)
        for variant in ordered:
            change = f"{variant.count - report.total:+,d}"
            label = _DIRECTION_LABELS[variant.direction]
            lines.append(f"  {label:<15}{variant.description}: {variant.count:,} ({change})")
    for advice in report.advice:
        lines += ["", advice.message]
        lines += [f"  - {suggestion}" for suggestion in advice.suggestions]
    if not none_relevant and report.total > 0:
        lines += [
            "",
            f"Screen the first {SCREENING_WINDOW} records; if none is relevant, run again "
            "with --none-relevant.",
        ]
    return "\n".join(lines)


async def _tune(
    question: StructuredQuestion,
    options: StrategyOptions,
    databases: Sequence[Database],
    args: argparse.Namespace,
) -> list[tuple[Database, TuningReport]]:
    reports: list[tuple[Database, TuningReport]] = []
    for database in databases:
        async with _open_backend(database, args) as backend:
            report = await tune(
                question, options, DIALECTS[database], backend, none_relevant=args.none_relevant
            )
        reports.append((database, report))
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "ui":
            return _ui(args)
        if args.command == "decs":
            text = " ".join(args.words)
            suggestions = asyncio.run(_suggest(text, _config(args), _decs_config()))
            print(format_suggestions(text, suggestions))
            return 0
        question, file_options = load_question(args.question_file)
        options = _merge_options(file_options, args)
        if args.command == "build":
            dialect = DIALECTS[Database(args.dialect)]
            print(dialect.render(question, options))
            for note in dialect_notes(question, dialect):
                print(f"biblio-search: {note}", file=sys.stderr)
            return 0
        if args.command == "check":
            decs_config = DecsConfig.from_env()
            result, fixed = asyncio.run(_check(question, _config(args), decs_config))
            equation = DIALECTS[Database.PUBMED].render(fixed, options)
            print(format_check(result, equation, decs=decs_config is not None))
            return 0
        requested = [Database(name) for name in args.databases]
        selection = select_databases(question, SEARCHABLE, requested)
        if args.command == "databases":
            print(format_selection(selection, question, options))
            return 0
        searched_in = _searched_in(selection)
        if args.command == "tune":
            tunings = asyncio.run(_tune(question, options, selection.searchable, args))
            blocks = [format_tuning(db, r, none_relevant=args.none_relevant) for db, r in tunings]
            print("\n\n".join([searched_in, *blocks]))
            return 0
        reports = asyncio.run(_search(question, options, selection.searchable, args))
        print("\n\n".join([searched_in] + [format_report(db, r) for db, r in reports]))
        return 0
    except QuestionError as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 2
    except (DatabaseError, ValueError) as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
