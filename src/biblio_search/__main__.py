"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from biblio_search import __version__
from biblio_search.eutils import MAX_RETMAX, EutilsClient, EutilsConfig, EutilsError
from biblio_search.models import ArticleSummary, QuestionError, StrategyOptions
from biblio_search.pipeline import SearchReport, run_search
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question

DIALECTS: dict[str, QueryDialect] = {PubMedDialect.name: PubMedDialect()}


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

    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build", parents=[question], help="print the search equation of a question file"
    )
    build.add_argument("--dialect", choices=sorted(DIALECTS), default=PubMedDialect.name)

    search = commands.add_parser(
        "search", parents=[question], help="run the equation in PubMed and list the first records"
    )
    search.add_argument(
        "--max", type=_max_records, default=20, dest="max_records", help="records to list"
    )
    search.add_argument(
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


async def _search(args: argparse.Namespace, config: EutilsConfig) -> SearchReport:
    question, file_options = load_question(args.question_file)
    async with EutilsClient(config) as client:
        return await run_search(
            question,
            _merge_options(file_options, args),
            PubMedDialect(),
            client,
            max_records=args.max_records,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            question, file_options = load_question(args.question_file)
            print(DIALECTS[args.dialect].render(question, _merge_options(file_options, args)))
            return 0
        config = EutilsConfig.from_env()
        if args.email:
            config = replace(config, email=args.email)
        print(format_report(asyncio.run(_search(args, config))))
        return 0
    except QuestionError as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 2
    except (EutilsError, ValueError) as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
