"""
seed_memory.py
==============
Optional CLI utility: BULK-SEED schema_memory.json with known-good column
mappings BEFORE ever running main.py on a new workbook.

Why this exists
----------------
pipeline.py's Fast Lane only helps once a schema has been approved at
least once (via the AI + confidence gate, or a human confirming/adjusting
it in main.py's interactive CLI). If you already KNOW how a recurring
source format maps -- e.g. "this is always our own standard export
template" or "Client X always sends this exact bank format" -- there's no
reason to wait for main.py to encounter it organically and pay for an AI
call (or a human prompt) the first time it shows up for real.
seed_memory.py lets you author that mapping once, in a small JSON file,
and load it straight into schema_memory.json ahead of time.

Seed file format (see sample_data/seed_mappings.example.json):
[
  {
    "raw_columns": ["Trans Date", "Details", "Debit Amount", "Credit Amount", "Running Balance"],
    "mapping": {
        "Trans Date": "Date",
        "Details": "Description",
        "Debit Amount": "Debit",
        "Credit Amount": "Credit",
        "Running Balance": "Balance"
    },
    "confidence_score": 100,
    "reasoning": "Manually authored seed mapping for our standard checking account export."
  }
]

Each entry's `raw_columns` is fingerprinted exactly the way a real sheet's
columns are in ingestion.py + memory.py (same clean/sort/hash), so any
workbook processed later by main.py whose sheet has this exact column set
(any order, case/whitespace-insensitive) hits the Fast Lane immediately --
no AI call, no CLI prompt.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import memory as memory_module


def load_seed_file(seed_path: str | Path) -> list[dict]:
    """Read and validate the seed JSON file's top-level shape."""
    path = Path(seed_path)
    with path.open("r", encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        raise ValueError(f"{seed_path} must contain a JSON list of seed entries.")
    return entries


def seed_memory_from_file(seed_path: str | Path, memory_path: str | Path = memory_module.DEFAULT_MEMORY_PATH) -> int:
    """Load every entry in the seed file into schema_memory.json, tagged
    with source='seeded' for audit purposes (so it's always clear a
    mapping was pre-authored rather than approved by the AI or a human
    reviewing a live file). Returns the number of entries written."""
    entries = load_seed_file(seed_path)
    memory = memory_module.load_memory(memory_path)

    for entry in entries:
        raw_columns = entry["raw_columns"]
        fingerprint = memory_module.compute_fingerprint(raw_columns)
        memory_module.remember_mapping(
            memory,
            fingerprint,
            raw_columns,
            entry["mapping"],
            entry.get("confidence_score", 100),
            entry.get("reasoning", "Seeded mapping."),
            source="seeded",
        )
        print(f"[SEEDED] fingerprint {fingerprint[:12]}... <- {raw_columns}")

    memory_module.save_memory(memory, memory_path)
    return len(entries)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk-seed schema_memory.json with known-good column mappings.")
    parser.add_argument("seed_file", type=Path, help="Path to a JSON file of seed mapping entries.")
    parser.add_argument(
        "--memory-path", type=Path, default=memory_module.DEFAULT_MEMORY_PATH,
        help="Path to the schema_memory.json cache to write into (default: ./schema_memory.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    count = seed_memory_from_file(args.seed_file, args.memory_path)
    print(f"\n[DONE] Seeded {count} schema mapping(s) into {args.memory_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
