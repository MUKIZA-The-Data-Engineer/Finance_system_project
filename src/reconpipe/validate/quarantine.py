"""Write quarantined rows to disk: failures are kept, never sent to /dev/null.

One CSV per run, data/quarantine/quarantine_<run_id>.csv, one line per failed
row: which rules it broke and why, where it came from (file and row), the key
values an accountant would look for, and the full row as JSON.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from reconpipe.validate.contracts import QuarantinedRow

COLUMNS = [
    "run_id",
    "rules",
    "reasons",
    "source_file",
    "row_number",
    "account_id",
    "date",
    "description",
    "amount",
    "balance",
    "row_json",
]


def _amount(row) -> str:
    """Clean rows have a signed amount; raw rows only have the printed columns."""
    if getattr(row, "amount", None) is not None:
        return str(row.amount)
    parts = [
        f"debit {row.debit}" if row.debit else "",
        f"credit {row.credit}" if row.credit else "",
    ]
    return " ".join(p for p in parts if p)


def write_quarantine(rows: list[QuarantinedRow], directory: Path, run_id: str) -> Path:
    """Write the run's quarantine file (header only if nothing failed) and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"quarantine_{run_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for q in rows:
            row = q.row
            writer.writerow(
                {
                    "run_id": run_id,
                    "rules": ";".join(q.rules),
                    "reasons": "; ".join(v.message for v in q.violations),
                    "source_file": row.source_file,
                    "row_number": row.row_number,
                    "account_id": row.account_id,
                    "date": str(row.date),
                    "description": getattr(row, "description_raw", None)
                    or getattr(row, "description", ""),
                    "amount": _amount(row),
                    "balance": "" if row.balance is None else str(row.balance),
                    "row_json": json.dumps(row.model_dump(mode="json"), sort_keys=True),
                }
            )
    return path
