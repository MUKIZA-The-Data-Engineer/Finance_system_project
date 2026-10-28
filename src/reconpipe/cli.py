"""Command line: `reconpipe <command>` (or `python -m reconpipe <command>`).

    reconpipe run             the full pipeline on data/raw/ (the default with no command)
    reconpipe pdf-to-excel    a PDF bank statement -> Excel

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


@command("pdf-to-excel")
def _pdf_to_excel(args: argparse.Namespace) -> int:
    from reconpipe.tools.pdf_to_excel import pdf_to_excel

    result = pdf_to_excel(args.file, args.output)
    print(f"Read {result.rows} rows from {args.file.name} ({result.read.tables[0].method})")
    print(f"Excel: {result.output}")
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
    p.add_argument("-o", "--output", type=Path, help="Excel file to write (default: output/)")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command is None:
        logging.getLogger().setLevel(logging.INFO)
        return COMMANDS["run"](args)
    from reconpipe.tools.reader import UnreadableFileError

    try:
        return COMMANDS[args.command](args)
    except UnreadableFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
