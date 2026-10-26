"""The pipeline so far: raw files -> parsed -> standardised -> validated -> warehouse.

    python -m reconpipe            # run on data/raw/, print the one-line result

Writes data/quarantine/quarantine_<run_id>.csv, reports/run_summary.json and
loads warehouse.duckdb. Each stage reads only what the stage before it produced.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from reconpipe.extract import default_registry
from reconpipe.extract.ledger import read_ledger
from reconpipe.load import LoadResult, RunInfo, connect, load_warehouse
from reconpipe.schemas import CleanTransaction
from reconpipe.transform import standardize
from reconpipe.validate import CONTRACT, QuarantinedRow, Violation, validate, write_quarantine

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    rows: list[CleanTransaction]
    quarantined: list[QuarantinedRow]
    summary: dict
    quarantine_file: Path
    summary_file: Path


def run_ingestion(
    raw_dir: Path = Path("data/raw"),
    quarantine_dir: Path = Path("data/quarantine"),
    reports_dir: Path = Path("reports"),
    today: date | None = None,
    run_id: str | None = None,
) -> IngestionResult:
    started = time.perf_counter()
    now = datetime.now(UTC)
    run_id = run_id or now.strftime("%Y%m%dT%H%M%SZ")
    today = today or now.date()

    extracted = default_registry().parse_directory(Path(raw_dir))
    standardized = standardize(extracted.transactions)
    validated = validate(standardized.clean, today)

    contract_failures = [
        QuarantinedRow(f.raw, (Violation(CONTRACT, f.reason),)) for f in standardized.failed
    ]
    quarantined = contract_failures + validated.quarantined
    quarantine_file = write_quarantine(quarantined, Path(quarantine_dir), run_id)

    per_file: dict[str, dict[str, int]] = {}
    for name in extracted.parsed:
        per_file[name] = {
            "rows_in": sum(r.source_file == name for r in extracted.transactions),
            "duplicates_removed": sum(d.row.source_file == name for d in standardized.duplicates),
            "quarantined": sum(q.row.source_file == name for q in quarantined),
            "rows_out": sum(r.source_file == name for r in validated.passed),
        }

    reasons = Counter(rule for q in quarantined for rule in q.rules)
    summary = {
        "run_id": run_id,
        "run_date": today.isoformat(),
        "raw_dir": Path(raw_dir).as_posix(),
        "files_parsed": extracted.parsed,
        "files_skipped": [{"file": s.file, "reason": s.reason} for s in extracted.skipped],
        "rows_in": len(extracted.transactions),
        "duplicates_removed": len(standardized.duplicates),
        "rows_quarantined": len(quarantined),
        "rows_out": len(validated.passed),
        "quarantine_reasons": dict(sorted(reasons.items())),
        "per_file": per_file,
        "duplicates": [
            {
                "source_file": d.row.source_file,
                "row_number": d.row.row_number,
                "duplicate_of_row": d.duplicate_of.row_number,
                "duplicate_of_file": d.duplicate_of.source_file,
            }
            for d in standardized.duplicates
        ],
        "quarantine_file": quarantine_file.as_posix(),
        "duration_seconds": round(time.perf_counter() - started, 3),
    }

    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    summary_file = reports / "run_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return IngestionResult(validated.passed, quarantined, summary, quarantine_file, summary_file)


@dataclass
class PipelineResult:
    ingestion: IngestionResult
    load: LoadResult
    db_path: Path


def run_pipeline(
    raw_dir: Path = Path("data/raw"),
    quarantine_dir: Path = Path("data/quarantine"),
    reports_dir: Path = Path("reports"),
    db_path: Path = Path("warehouse.duckdb"),
    ledger_file: Path | None = None,
    today: date | None = None,
    run_id: str | None = None,
) -> PipelineResult:
    """Ingest the statements, read the ledger, and load both into the warehouse."""
    started_at = datetime.now(UTC).replace(tzinfo=None)
    ingestion = run_ingestion(raw_dir, quarantine_dir, reports_dir, today, run_id)
    ledger = read_ledger(ledger_file or Path(raw_dir) / "ledger.xlsx")

    s = ingestion.summary
    run = RunInfo(
        run_id=s["run_id"],
        started_at=started_at,
        finished_at=datetime.now(UTC).replace(tzinfo=None),
        files_parsed=len(s["files_parsed"]),
        files_skipped=len(s["files_skipped"]),
        rows_in=s["rows_in"],
        duplicates_removed=s["duplicates_removed"],
        rows_quarantined=s["rows_quarantined"],
    )
    with closing(connect(db_path)) as con:
        loaded = load_warehouse(con, ingestion.rows, ledger, run)

    s["warehouse"] = {
        "db_path": Path(db_path).as_posix(),
        "bank_rows_loaded": loaded.bank_rows,
        "ledger_rows_loaded": loaded.ledger_rows,
        "accounts": loaded.accounts,
        "counterparties": loaded.counterparties,
    }
    ingestion.summary_file.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return PipelineResult(ingestion, loaded, Path(db_path))


def describe(summary: dict) -> str:
    """'Of N rows, M passed, D duplicates were removed and K were quarantined (...)'."""
    reasons = summary["quarantine_reasons"]
    why = ", ".join(f"{n} {rule}" for rule, n in reasons.items()) or "no rule broken"
    return (
        f"Of {summary['rows_in']} rows, {summary['rows_out']} passed, "
        f"{summary['duplicates_removed']} duplicate(s) were removed and "
        f"{summary['rows_quarantined']} were quarantined ({why})."
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_pipeline()
    print(describe(result.ingestion.summary))
    print(
        f"Warehouse:  {result.db_path} ({result.load.bank_rows} bank rows, "
        f"{result.load.ledger_rows} ledger entries)"
    )
    print(f"Summary:    {result.ingestion.summary_file}")
    print(f"Quarantine: {result.ingestion.quarantine_file}")
    return 0
