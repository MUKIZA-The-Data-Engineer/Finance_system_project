"""Turn recon_results into the handoff an accountant acts on, plus the run's numbers.

Writes, into reports/:

    exceptions.csv    every item that needs a person: what is wrong, the amounts on
                      both sides, and the file and row to open on each side
    matched.csv       every auto-matched item with its tier and confidence
    run_summary.json  the ingestion summary plus match rate by tier, value
                      reconciled and in exception, accuracy against the ground
                      truth (when given), duration, and the one-line headline
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb

REASONS = {
    "amount_mismatch": "Same reference on both sides, but the amounts differ",
    "missing_in_ledger": "On the bank statement, but not in the books",
    "missing_in_bank": "In the books, but not on any bank statement (e.g. uncleared cheque)",
    "duplicate_suspected": "Posted twice in the books: identical to an earlier entry",
}
TIERS = ("tier1_exact", "tier2_amount_date", "tier3_fuzzy")

EXCEPTION_COLUMNS = [
    "status",
    "reason",
    "detail",
    "account_id",
    "amount",
    "difference",
    "bank_date",
    "bank_amount",
    "bank_ref",
    "bank_description",
    "bank_source_file",
    "bank_source_row",
    "ledger_entry_ids",
    "ledger_date",
    "ledger_amount",
    "ledger_description",
    "ledger_source_file",
    "ledger_source_rows",
]
MATCHED_COLUMNS = [
    "match_tier",
    "confidence",
    "detail",
    "account_id",
    "bank_date",
    "bank_amount",
    "bank_ref",
    "bank_description",
    "bank_source_file",
    "bank_source_row",
    "ledger_entry_ids",
    "ledger_date",
    "ledger_description",
    "ledger_source_rows",
]


@dataclass(frozen=True)
class ReportResult:
    exceptions_file: Path
    matched_file: Path
    summary_file: Path
    summary: dict


def _fetch(db_path: Path) -> list[dict]:
    # Default connection config: dbt-duckdb may hold the same file open in-process.
    with closing(duckdb.connect(str(db_path))) as con:
        cur = con.execute(
            "SELECT * FROM recon_results "
            "ORDER BY account_id, coalesce(bank_date, ledger_date), result_id"
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _cell(value: object) -> object:
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return "" if value is None else value


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c)) for c in columns})


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def check_accuracy(rows: list[dict], expected: dict) -> dict:
    """Compare every item's classification with the generator's ground truth."""
    truth = {}
    for t in expected["bank_transactions"]:
        if t["expected_status"] == "quarantined":
            continue
        tier = t["expected_tier"]
        if t["expected_status"] == "amount_mismatch":
            tier = "tier1_exact"  # paired by reference; the amounts are what differ
        truth[("bank", t["file"], t["row_number"])] = (t["expected_status"], tier)
    for e in expected["ledger_only"]:
        truth[("ledger", e["entry_id"])] = (e["expected_status"], None)

    got = {}
    for r in rows:
        if r["bank_source_file"] is not None:
            key = ("bank", r["bank_source_file"], r["bank_source_row"])
        else:
            key = ("ledger", r["ledger_entry_ids"][0])
        got[key] = (r["status"], r["match_tier"])

    wrong = [
        {"item": " ".join(map(str, k)), "expected": truth.get(k), "got": got.get(k)}
        for k in sorted(truth.keys() | got.keys(), key=str)
        if truth.get(k) != got.get(k)
    ]
    checked = len(truth.keys() | got.keys())
    return {
        "items_checked": checked,
        "items_correct": checked - len(wrong),
        "accuracy": round((checked - len(wrong)) / checked, 4) if checked else None,
        "mismatches": wrong[:20],
    }


def headline(summary: dict) -> str:
    r = summary["reconciliation"]
    return (
        f"Reconciles {r['bank_lines']} bank transactions across "
        f"{len(summary['files_parsed'])} bank formats in {summary['duration_seconds']:.1f} s, "
        f"auto-matching {r['match_rate']:.1%}, and surfaces the other "
        f"{r['exceptions']} items as a reviewable exception report."
    )


def build_reports(
    db_path: Path,
    reports_dir: Path,
    summary: dict,
    expected_path: Path | None = None,
    duration_seconds: float | None = None,
) -> ReportResult:
    rows = _fetch(Path(db_path))
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)

    matched = [r for r in rows if r["status"] == "matched"]
    exceptions = [
        {
            **r,
            "reason": REASONS[r["status"]],
            "amount": r["bank_amount"] if r["bank_amount"] is not None else r["ledger_amount"],
        }
        for r in rows
        if r["status"] != "matched"
    ]
    exceptions_file = reports / "exceptions.csv"
    matched_file = reports / "matched.csv"
    _write(exceptions_file, EXCEPTION_COLUMNS, exceptions)
    _write(matched_file, MATCHED_COLUMNS, matched)

    bank_lines = sum(r["bank_txn_id"] is not None for r in rows)
    by_tier = Counter(r["match_tier"] for r in matched)
    statuses = Counter(r["status"] for r in rows)
    value_matched = sum((abs(r["bank_amount"]) for r in matched), Decimal("0"))
    value_exception = sum((abs(r["amount"]) for r in exceptions), Decimal("0"))

    summary = dict(summary)
    if duration_seconds is not None:
        summary["duration_seconds"] = round(duration_seconds, 2)
    summary["reconciliation"] = {
        "bank_lines": bank_lines,
        "ledger_lines": sum(len(r["ledger_entry_ids"]) for r in rows),
        "matched": len(matched),
        "match_rate": round(len(matched) / bank_lines, 4) if bank_lines else 0.0,
        "match_rate_by_tier": {
            t: round(by_tier[t] / bank_lines, 4) if bank_lines else 0.0 for t in TIERS
        },
        "matched_by_tier": {t: by_tier[t] for t in TIERS},
        "exceptions": len(exceptions),
        "exceptions_by_status": {s: statuses[s] for s in REASONS},
        "value_reconciled": _money(value_matched),
        "value_in_exception": _money(value_exception),
        "currency": "RWF",
    }
    if expected_path is not None and Path(expected_path).exists():
        expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
        summary["accuracy"] = check_accuracy(rows, expected)
    summary["headline"] = headline(summary)
    summary["exceptions_file"] = exceptions_file.as_posix()
    summary["matched_file"] = matched_file.as_posix()

    summary_file = reports / "run_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return ReportResult(exceptions_file, matched_file, summary_file, summary)
