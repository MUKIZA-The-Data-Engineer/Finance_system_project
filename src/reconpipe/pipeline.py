"""The whole pipeline: raw files -> parsed -> standardised -> validated ->
warehouse -> dbt matching cascade -> exception report.

    python -m reconpipe            # run end to end on data/raw/

Writes data/quarantine/quarantine_<run_id>.csv, loads warehouse.duckdb, runs
`dbt build`, and writes reports/exceptions.csv, matched.csv and run_summary.json.
Each stage reads only what the stage before it produced.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from reconpipe.extract import ExtractResult, default_registry
from reconpipe.extract.ledger import read_ledger
from reconpipe.load import LoadResult, RunInfo, connect, load_warehouse
from reconpipe.report import ReportResult, build_reports
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
    extracted = default_registry().parse_directory(Path(raw_dir))
    return validate_extracted(
        extracted, raw_dir, quarantine_dir, reports_dir, today, run_id, started
    )


def validate_extracted(
    extracted: ExtractResult,
    raw_dir: Path,
    quarantine_dir: Path,
    reports_dir: Path,
    today: date | None = None,
    run_id: str | None = None,
    started: float | None = None,
) -> IngestionResult:
    """Standardise and validate parsed rows, write the quarantine file and run summary."""
    started = time.perf_counter() if started is None else started
    now = datetime.now(UTC)
    run_id = run_id or now.strftime("%Y%m%dT%H%M%SZ")
    today = today or now.date()

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
    return load_ingestion(
        ingestion, ledger_file or Path(raw_dir) / "ledger.xlsx", db_path, started_at
    )


def load_ingestion(
    ingestion: IngestionResult,
    ledger_file: Path,
    db_path: Path,
    started_at: datetime | None = None,
) -> PipelineResult:
    """Read the ledger and load it with the validated statement rows into the warehouse."""
    started_at = started_at or datetime.now(UTC).replace(tzinfo=None)
    ledger = read_ledger(ledger_file)

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
        loaded = load_warehouse(con, ingestion.rows, ledger, run, ingestion.quarantined)

    s["warehouse"] = {
        "db_path": Path(db_path).as_posix(),
        "bank_rows_loaded": loaded.bank_rows,
        "ledger_rows_loaded": loaded.ledger_rows,
        "accounts": loaded.accounts,
        "counterparties": loaded.counterparties,
    }
    ingestion.summary_file.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return PipelineResult(ingestion, loaded, Path(db_path))


class DbtBuildError(RuntimeError):
    """dbt build failed: a model errored or a data test found bad rows."""


def run_dbt(db_path: Path, project_dir: Path = Path("dbt")) -> None:
    """`dbt build` (models + data tests) against db_path, in this process."""
    from dbt.cli.main import dbtRunner

    previous = os.environ.get("RECONPIPE_DB")
    os.environ["RECONPIPE_DB"] = str(Path(db_path).resolve())
    try:
        args = ["build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
        result = dbtRunner().invoke([*args, "--quiet"])
    finally:
        if previous is None:
            os.environ.pop("RECONPIPE_DB", None)
        else:
            os.environ["RECONPIPE_DB"] = previous
    if result.exception is not None:
        raise DbtBuildError(f"dbt build could not run: {result.exception}")
    if not result.success:
        failed = [r.node.name for r in result.result if r.status not in ("pass", "success")]
        raise DbtBuildError(f"dbt build failed: {', '.join(failed)}")


@dataclass
class RunAllResult:
    pipeline: PipelineResult
    report: ReportResult


def run_all(
    raw_dir: Path = Path("data/raw"),
    quarantine_dir: Path = Path("data/quarantine"),
    reports_dir: Path = Path("reports"),
    db_path: Path = Path("warehouse.duckdb"),
    dbt_dir: Path = Path("dbt"),
    expected_path: Path | None = Path("tests/expected_results.json"),
    today: date | None = None,
    run_id: str | None = None,
) -> RunAllResult:
    """Everything, end to end. expected_path, if it exists, adds an accuracy check."""
    started = time.perf_counter()
    pipeline = run_pipeline(raw_dir, quarantine_dir, reports_dir, db_path, None, today, run_id)
    run_dbt(db_path, dbt_dir)
    report = build_reports(
        db_path,
        reports_dir,
        pipeline.ingestion.summary,
        expected_path,
        duration_seconds=time.perf_counter() - started,
    )
    return RunAllResult(pipeline, report)


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
    result = run_all()
    summary = result.report.summary
    print(describe(summary))
    print(summary["headline"])
    if "accuracy" in summary:
        acc = summary["accuracy"]
        print(
            f"Accuracy vs ground truth: {acc['items_correct']}/{acc['items_checked']} "
            f"items ({acc['accuracy']:.1%})"
        )
    print(f"Exceptions: {result.report.exceptions_file}")
    print(f"Summary:    {result.report.summary_file}")
    print(f"Quarantine: {result.pipeline.ingestion.quarantine_file}")
    print(f"Warehouse:  {result.pipeline.db_path}")
    return 0
