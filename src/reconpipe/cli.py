"""Command line: `reconpipe <command>` (or `python -m reconpipe <command>`).

    reconpipe run             the full pipeline on data/raw/ (the default with no command)
    reconpipe pdf-to-excel    a PDF bank statement -> Excel
    reconpipe standardize     any messy PDF/Excel/CSV -> clean standard Excel + issues
    reconpipe reconcile       two sets of files (e.g. bank vs books) -> reconciliation workbook
    reconpipe odoo-export     any file -> Odoo-ready import workbook (settings: config/odoo.toml)
    reconpipe ai              prompt library answered by Claude (list, show, run, map-columns)

    reconpipe process FILE    any file(s) or a folder -> clean Excel + Odoo import, in one step
    reconpipe workflow        --bank FILE --books FILE -> clean, Odoo-ready, reconciled, reported

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


def _mapper(args: argparse.Namespace):
    """Remembered column mappings always apply; Claude is asked only with --ai."""
    from reconpipe.tools.ai import make_mapper

    def asking(table) -> None:
        print(f"Asking Claude which column is which in {table.source_file} [{table.sheet}]...")

    return make_mapper(args.ai, headers_only=args.headers_only, on_ask=asking)


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

    result = pdf_to_excel(
        args.file,
        args.output,
        parse_overrides(args.map),
        args.date_order,
        mapper=_mapper(args),
    )
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

    result = standardize_file(
        args.file, parse_overrides(args.map), args.date_order, mapper=_mapper(args)
    )
    out = write_standardized(result, args.output or default_output(args.file, "standardized"))
    _print_standardized(result.summary)
    print(f"Excel: {out}")
    return 0


@command("reconcile")
def _reconcile(args: argparse.Namespace) -> int:
    from reconpipe.tools.columns import parse_overrides
    from reconpipe.tools.reconcile import reconcile_files, write_report

    a_files, b_files = list(args.a or []), list(args.b or [])
    if len(args.files) == 2 and not a_files and not b_files:
        a_files, b_files = [args.files[0]], [args.files[1]]
    elif args.files:
        print("error: give exactly two files, or use --a and --b", file=sys.stderr)
        return 2
    if not a_files or not b_files:
        print(
            "error: need files on both sides (e.g. reconpipe reconcile bank.pdf books.xlsx)",
            file=sys.stderr,
        )
        return 2
    result = reconcile_files(
        a_files,
        b_files,
        _tolerances(args),
        args.a_label,
        args.b_label,
        parse_overrides(args.map_a),
        parse_overrides(args.map_b),
        args.date_order,
        mapper=_mapper(args),
    )
    out = (
        args.output
        or Path("output") / f"reconciliation_{a_files[0].stem}_vs_{b_files[0].stem}.xlsx"
    )
    write_report(result, out)
    s = result.summary
    by_status = s["items_by_status"]
    print(
        f"{s['matched_a_lines']} of {s['a_lines']} {args.a_label} lines matched "
        f"({s['match_rate']:.1%}); {by_status['amount_mismatch']} amount mismatches, "
        f"{by_status['missing_in_b']} missing in {args.b_label}, "
        f"{by_status['missing_in_a']} missing in {args.a_label}, "
        f"{by_status['duplicate_suspected']} suspected duplicates"
    )
    print(f"Excel: {out}")
    return 0


def _tolerances(args: argparse.Namespace):
    from decimal import Decimal

    from reconpipe.tools.reconcile import Tolerances

    return Tolerances(
        amount=Decimal(str(args.amount_tolerance)),
        days=args.date_tolerance,
        description_days=args.description_days,
        min_similarity=args.min_similarity,
        group_days=args.group_days,
    )


@command("odoo-export")
def _odoo_export(args: argparse.Namespace) -> int:
    from reconpipe.tools.columns import parse_overrides
    from reconpipe.tools.excel_out import default_output
    from reconpipe.tools.odoo import TARGETS, export_file, load_config, write_export, write_template

    if args.template:
        targets = [args.target] if args.target else list(TARGETS)
        for target in targets:
            path = write_template(target, Path("templates") / f"odoo_{target}_template.xlsx")
            print(f"Template: {path}")
        return 0
    if args.file is None:
        print("error: give a file to export, or --template", file=sys.stderr)
        return 2
    cfg = load_config(args.config, target=args.target, journal=args.journal)
    export = export_file(args.file, cfg, parse_overrides(args.map), args.date_order, _mapper(args))
    written = write_export(export, args.output or default_output(args.file, "odoo"), args.csv)
    errors = sum(i.severity == "error" for i in export.issues)
    warnings = sum(i.severity == "warning" for i in export.issues)
    print(
        f"{export.exported_rows} line(s) ready for Odoo ({cfg.target}); "
        f"{errors} error(s), {warnings} warning(s) (see the 'issues' sheet)"
    )
    if cfg.examples_in_use():
        print("note: config/odoo.toml still has EXAMPLE codes; set your real Odoo codes first")
    for path in written:
        print(f"Wrote: {path}")
    return 0


@command("process")
def _process(args: argparse.Namespace) -> int:
    from reconpipe.tools.columns import parse_overrides
    from reconpipe.tools.odoo import load_config
    from reconpipe.tools.reader import UnreadableFileError
    from reconpipe.tools.standardizer import MappingError
    from reconpipe.tools.workflow import find_files, process_file

    cfg = None if args.no_odoo else load_config(args.config, target=args.target)
    files = find_files(args.files)
    mapper, overrides = _mapper(args), parse_overrides(args.map)
    failed = 0
    for path in files:
        out_dir = args.out_dir / path.stem
        try:
            result = process_file(path, out_dir, cfg, overrides, args.date_order, mapper, args.csv)
        except (UnreadableFileError, MappingError) as exc:
            print(f"{path.name}: FAILED - {exc}", file=sys.stderr)
            failed += 1
            continue
        print(result.describe())
        for written in result.files:
            print(f"  wrote {written}")
    if cfg and cfg.examples_in_use():
        print("note: config/odoo.toml still has EXAMPLE codes; set your real Odoo codes first")
    print(f"Done: {len(files) - failed} of {len(files)} file(s) processed into {args.out_dir}/")
    return 0 if not failed else 2


@command("workflow")
def _workflow(args: argparse.Namespace) -> int:
    from reconpipe.tools.odoo import load_config
    from reconpipe.tools.workflow import find_files, run_workflow

    if not args.bank or not args.books:
        print(
            "error: give both sides, e.g. reconpipe workflow --bank stmt.pdf --books ledger.xlsx",
            file=sys.stderr,
        )
        return 2
    result = run_workflow(
        find_files(args.bank),
        find_files(args.books),
        args.out_dir,
        None if args.no_odoo else load_config(args.config, target=args.target),
        _tolerances(args),
        args.a_label,
        args.b_label,
        args.date_order,
        _mapper(args),
    )
    print("\n".join(result.summary_lines()))
    print(f"\nEverything is in {result.folder}/ (start with reconciliation.xlsx)")
    return 0


@command("ai")
def _ai(args: argparse.Namespace) -> int:
    from reconpipe.tools import ai

    if args.ai_command == "list":
        for name, prompt in ai.prompts().items():
            print(f"{name:18} {prompt.title}: {prompt.description}")
        return 0
    if args.ai_command == "show":
        text = ai.render(args.name, args.file, args.headers_only)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
            print(f"Prompt: {args.output}")
        else:
            print(text)
        return 0
    if args.ai_command == "run":
        answer = ai.run(args.name, args.file, args.headers_only, effort=args.effort)
        out = args.output or Path("output") / f"{args.file.stem}_{args.name}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(answer + "\n", encoding="utf-8")
        print(answer)
        print(f"\nSaved: {out}")
        return 0
    # map-columns: ask Claude for every table, show the result, remember it
    from reconpipe.tools.columns import map_columns
    from reconpipe.tools.reader import read_tables

    for table in read_tables(args.file).tables:
        rules = map_columns(table.header)
        suggestion = ai.suggest_mapping(table, headers_only=args.headers_only)
        fields = {c: f for c, f, conf in suggestion if conf >= ai.MIN_CONFIDENCE}
        merged = ai.merge(rules, fields)
        print(f"{table.source_file} [{table.sheet}]")
        confidence = {c: conf for c, _, conf in suggestion}
        for choice in merged.choices:
            conf = confidence.get(choice.source)
            note = f"  (Claude {conf:.0%} sure)" if conf is not None else ""
            print(f"  {choice.source:30} -> {choice.field:12} [{choice.how}]{note}")
        if merged.missing:
            print(f"  still missing: {', '.join(merged.missing)}; not remembered")
        else:
            saved = ai.remember(table.header, {c.source: c.field for c in merged.choices})
            print(f"  remembered in {saved}: files with these columns now map automatically")
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

    p = sub.add_parser("reconcile", help="side A files vs side B files -> reconciliation workbook")
    p.add_argument("files", type=Path, nargs="*", help="shortcut: A_FILE B_FILE")
    p.add_argument(
        "--a", type=Path, action="append", metavar="FILE", help="side A file (repeatable)"
    )
    p.add_argument(
        "--b", type=Path, action="append", metavar="FILE", help="side B file (repeatable)"
    )
    p.add_argument("--a-label", default="bank", help="name of side A in the report (default: bank)")
    p.add_argument("--b-label", default="books", help="name of side B (default: books)")
    _tolerance_args(p)
    p.add_argument("--map-a", action="append", metavar="COLUMN=FIELD", help="column hint for A")
    p.add_argument("--map-b", action="append", metavar="COLUMN=FIELD", help="column hint for B")
    p.add_argument("--date-order", choices=["auto", "day-first", "month-first"], default="auto")
    p.add_argument("-o", "--output", type=Path, help="Excel file to write (default: output/)")
    _ai_flags(p)

    p = sub.add_parser("odoo-export", help="any file -> Odoo-ready import workbook")
    p.add_argument("file", type=Path, nargs="?", help="the file to export")
    p.add_argument(
        "--target",
        choices=["bank_statement_lines", "journal_entries"],
        help="Odoo import to produce (default: from config/odoo.toml)",
    )
    p.add_argument("--journal", help="Odoo journal code, overriding config/odoo.toml")
    p.add_argument("--config", type=Path, help="settings file (default: config/odoo.toml)")
    p.add_argument("--csv", action="store_true", help="also write the import as CSV")
    p.add_argument(
        "--template",
        action="store_true",
        help="write empty import templates to templates/ and stop",
    )
    _common(p)

    p = sub.add_parser("process", help="file(s) or a folder -> clean Excel + Odoo import")
    p.add_argument("files", type=Path, nargs="+", help="PDF/Excel/CSV files, or a folder")
    p.add_argument("--out-dir", type=Path, default=Path("output"), help="default: output/")
    _odoo_args(p)
    _common(p, output=False)

    p = sub.add_parser("workflow", help="bank + books -> clean, Odoo-ready, reconciled")
    p.add_argument(
        "--bank",
        type=Path,
        action="append",
        metavar="FILE",
        help="statement file or folder (repeatable)",
    )
    p.add_argument(
        "--books",
        type=Path,
        action="append",
        metavar="FILE",
        help="ledger file or folder (repeatable)",
    )
    p.add_argument("--out-dir", type=Path, help="default: output/workflow_<bank>_vs_<books>/")
    p.add_argument("--a-label", default="bank", help=argparse.SUPPRESS)
    p.add_argument("--b-label", default="books", help=argparse.SUPPRESS)
    _tolerance_args(p)
    _odoo_args(p)
    _common(p, output=False, mapping=False)

    p = sub.add_parser("ai", help="prompt library answered by Claude")
    ai_sub = p.add_subparsers(dest="ai_command", metavar="<ai command>", required=True)
    ai_sub.add_parser("list", help="the prompts available")
    for name, text in [
        ("show", "print a prompt filled in from a file (nothing is sent)"),
        ("run", "send a prompt about a file to Claude and save the answer"),
    ]:
        q = ai_sub.add_parser(name, help=text)
        q.add_argument("name", help="prompt name (see `reconpipe ai list`)")
        q.add_argument("file", type=Path, help="the PDF, Excel or CSV file")
        q.add_argument("-o", "--output", type=Path, help="file to write")
        q.add_argument("--headers-only", action="store_true", help="share column names only")
        if name == "run":
            q.add_argument(
                "--effort",
                choices=["low", "medium", "high"],
                default="medium",
                help="how hard Claude thinks (default medium)",
            )
    q = ai_sub.add_parser("map-columns", help="let Claude map a file's columns, and remember it")
    q.add_argument("file", type=Path, help="the PDF, Excel or CSV file")
    q.add_argument("--headers-only", action="store_true", help="share column names only")
    return parser


def _tolerance_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--amount-tolerance",
        type=float,
        default=0.0,
        help="amounts this far apart still match (default 0)",
    )
    p.add_argument(
        "--date-tolerance",
        type=int,
        default=2,
        help="days apart for amount + date matches (default 2)",
    )
    p.add_argument(
        "--description-days",
        type=int,
        default=14,
        help="days apart for amount + description matches (default 14)",
    )
    p.add_argument(
        "--min-similarity",
        type=float,
        default=0.4,
        help="text similarity needed for description matches, 0-1 (default 0.4)",
    )
    p.add_argument(
        "--group-days",
        type=int,
        default=7,
        help="days apart for one-line-vs-several-lines matches (default 7)",
    )


def _ai_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--ai",
        action="store_true",
        help="ask Claude which column is which when the built-in rules cannot tell",
    )
    p.add_argument(
        "--headers-only",
        action="store_true",
        help="with --ai, share column names only (no sample rows)",
    )


def _odoo_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--target",
        choices=["bank_statement_lines", "journal_entries"],
        help="Odoo import to produce (default: from config/odoo.toml)",
    )
    p.add_argument("--config", type=Path, help="Odoo settings file (default: config/odoo.toml)")
    p.add_argument("--no-odoo", action="store_true", help="skip the Odoo import file")
    p.add_argument("--csv", action="store_true", help="also write the Odoo import as CSV")


def _common(p: argparse.ArgumentParser, output: bool = True, mapping: bool = True) -> None:
    if output:
        p.add_argument("-o", "--output", type=Path, help="Excel file to write (default: output/)")
    if mapping:
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
    _ai_flags(p)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command is None:
        logging.getLogger().setLevel(logging.INFO)
        return COMMANDS["run"](args)
    from reconpipe.tools.ai import AIError
    from reconpipe.tools.columns import MappingInputError
    from reconpipe.tools.odoo import OdooConfigError
    from reconpipe.tools.reader import UnreadableFileError
    from reconpipe.tools.standardizer import MappingError

    try:
        return COMMANDS[args.command](args)
    except (
        UnreadableFileError,
        MappingError,
        MappingInputError,
        OdooConfigError,
        AIError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
