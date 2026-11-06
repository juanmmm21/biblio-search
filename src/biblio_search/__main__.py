"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from biblio_search import __version__
from biblio_search.models import QuestionError, StrategyOptions
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question

DIALECTS: dict[str, QueryDialect] = {PubMedDialect.name: PubMedDialect()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biblio-search",
        description="Build rigorous bibliographic search strategies from structured questions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="print the search equation of a question file")
    build.add_argument("question_file", type=Path, help="TOML file with the structured question")
    build.add_argument("--dialect", choices=sorted(DIALECTS), default=PubMedDialect.name)
    build.add_argument(
        "--repeat-controlled-as-free",
        action="store_true",
        help="also search every heading as free text (more sensitive)",
    )
    build.add_argument(
        "--unindexed-only",
        action="store_true",
        help="limit free text to records outside MEDLINE (more specific)",
    )
    return parser


def _merge_options(file_options: StrategyOptions, args: argparse.Namespace) -> StrategyOptions:
    # Los flags de la CLI solo activan opciones; nunca desactivan lo que pide el fichero.
    return replace(
        file_options,
        repeat_controlled_as_free=file_options.repeat_controlled_as_free
        or bool(args.repeat_controlled_as_free),
        free_text_unindexed_only=file_options.free_text_unindexed_only
        or bool(args.unindexed_only),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        question, file_options = load_question(args.question_file)
    except QuestionError as exc:
        print(f"biblio-search: {exc}", file=sys.stderr)
        return 2
    dialect = DIALECTS[args.dialect]
    print(dialect.render(question, _merge_options(file_options, args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
