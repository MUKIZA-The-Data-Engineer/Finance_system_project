"""
main.py
=======
CLI entry point for the Confidence-Driven Finance ETL Pipeline.

Usage:
    python main.py path/to/workbook.xlsx
    python main.py path/to/workbook.xlsx --output-dir output --memory-path schema_memory.json

Pipeline:
    ingestion.py   -> discover every genuine financial sheet in the workbook
    pipeline.py    -> map each sheet's columns (memory Fast Lane / AI / human gate)
    transformer.py -> apply mappings, standardize types, consolidate into one ledger
    exporter.py    -> write an Odoo-ready Journal Entries CSV + Excel file
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import exporter
import ingestion
import pipeline
import transformer
from memory import DEFAULT_MEMORY_PATH


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert an unpredictable multi-sheet Excel export into an Odoo-ready Universal General Ledger."
    )
    parser.add_argument("file_path", type=Path, help="Path to the source .xlsx workbook to ingest.")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).parent / "output",
        help="Directory to write the consolidated ledger and Odoo export into (default: ./output).",
    )
    parser.add_argument(
        "--memory-path", type=Path, default=DEFAULT_MEMORY_PATH,
        help="Path to the schema_memory.json cache (default: ./schema_memory.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.file_path.exists():
        print(f"[ERROR] File not found: {args.file_path}", file=sys.stderr)
        return 1

    print(f"\n=== Stage 1: Discovering financial sheets in '{args.file_path.name}' ===")
    financial_sheets, skipped_sheets = ingestion.discover_financial_sheets(args.file_path)

    for sheet_name, reason in skipped_sheets.items():
        print(f"  [SKIPPED] '{sheet_name}': {reason}")
    for sheet_name, df in financial_sheets.items():
        print(f"  [KEPT] '{sheet_name}': {len(df)} data row(s), columns: {list(df.columns)}")

    if not financial_sheets:
        print("[ERROR] No financial sheets were found in this workbook. Nothing to process.", file=sys.stderr)
        return 1

    print(f"\n=== Stage 2-4: Schema fingerprinting, AI mapping & routing ===")
    mapping_results = pipeline.process_workbook(financial_sheets, memory_path=args.memory_path)

    for sheet_name, result in mapping_results.items():
        print(
            f"  '{sheet_name}': source={result['source']}, "
            f"confidence={result['confidence_score']}, mapping={result['mapping']}"
        )

    print(f"\n=== Stage 5: Consolidating into the master Universal General Ledger ===")
    master_df = transformer.consolidate(financial_sheets, mapping_results)
    print(f"  Master ledger: {len(master_df)} row(s) across {len(financial_sheets)} sheet(s).")

    integrity_report = transformer.check_debit_credit_integrity(master_df)

    print(f"\n=== Stage 6: Exporting Odoo-ready Journal Entries ===")
    paths = exporter.generate_odoo_export(master_df, args.output_dir)
    ledger_path = args.output_dir / "master_general_ledger.csv"
    master_df.to_csv(ledger_path, index=False)

    print(f"\n[DONE]")
    print(f"  Master ledger:        {ledger_path.resolve()}")
    print(f"  Odoo CSV:             {paths['csv'].resolve()}")
    print(f"  Odoo Excel:           {paths['xlsx'].resolve()}")
    print(f"  Debit/Credit balanced: {integrity_report['is_balanced']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
