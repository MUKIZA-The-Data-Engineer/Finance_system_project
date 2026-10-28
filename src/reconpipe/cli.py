"""Command line: `reconpipe <command>` (or `python -m reconpipe <command>`).

    reconpipe run             the full pipeline on data/raw/ (the default with no command)
    reconpipe pdf-to-excel    a PDF bank statement -> Excel
    reconpipe standardize     any messy PDF/Excel/CSV -> clean standard Excel + issues

Run `reconpipe <command> --help` for each command's options.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {}


def command(name: str):
    def register(fn: Callable[[argparse.Namespace], int]):
        COMMANDS[name] = fn
        return fn

    return register


@command("run")
def _run(args: argparse.Namespace) -> int:
    from reconpipe.pipeline import main as pipeline_main

    return pipeline_main()


def _print_standardized(summary: dict) -> None:
    print(
        f"{summary['clean_rows']} clean rows from {summary['rows_read']} read; "
        f"{summary['rows_excluded']} excluded, {summary['duplicates_removed']} duplicates "
        f"removed, {summary['rows_merged_into_previous']} wrapped lines joined, "
        f"{summary['warnings']} warnings (see the 'issues' sheet)"
    )


@command("pdf-to-excel")
def _pdf_to_excel(args: argparse.Namespace) -> int:
    from reconpipe.tools.columns import parse_overrides
    from reconpipe.tools.pdf_to_excel import pdf_to_excel

    result = pdf_to_excel(args.file, args.output, parse_overrides(args.map), args.date_order)
    print(f"Read {args.file.name} ({result.read.tables[0].method})")
    if result.standardized:
        _print_standardized(result.standardized.summary)
    else:
        print(f"Raw rows only: {result.mapping_problem}")
    print(f"Excel: {result.output}")
    return 0


@command("standardize")
def _standardize(args: argparse.Namespace) -> int:
    from reconpipe.tools.columns import parse_overrides
    from reconpipe.tools.excel_out import default_output
    from reconpipe.tools.standardizer import standardize_file, write_standardized

    result = standardize_file(args.file, parse_overrides(args.map), args.date_order)
    out = write_standardized(result, args.output or default_output(args.file, "standardized"))
    _print_standardized(result.summary)
    print(f"Excel: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconpipe",
        description="Bank statement and ledger toolkit. With no command, runs the full pipeline.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("run", help="full pipeline on data/raw/: parse, validate, load, dbt, reports")

    p = sub.add_parser("pdf-to-excel", help="a PDF bank statement -> Excel")
    p.add_argument("file", type=Path, help="the PDF to read")
    _common(p)

    p = sub.add_parser("standardize", help="any messy PDF/Excel/CSV -> clean standard Excel")
    p.add_argument("file", type=Path, help="the file to clean")
    _common(p)
    return parser


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--output", type=Path, help="Excel file to write (default: output/)")
    p.add_argument(
        "--map",
        action="append",
        metavar="COLUMN=FIELD",
        help='say which column is which, e.g. --map "Txn Date=date" (repeatable)',
    )
    p.add_argument(
        "--date-order",
        choices=["auto", "day-first", "month-first"],
        default="auto",
        help="how to read dates like 05/01/2026 when the file itself cannot tell",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command is None:
        logging.getLogger().setLevel(logging.INFO)
        return COMMANDS["run"](args)
    from reconpipe.tools.columns import MappingInputError
    from reconpipe.tools.reader import UnreadableFileError
    from reconpipe.tools.standardizer import MappingError

    try:
        return COMMANDS[args.command](args)
    except (UnreadableFileError, MappingError, MappingInputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
