"""ReconPipe as six Dagster assets, one per pipeline stage, plus asset checks.

    raw_files -> parsed_transactions -> validated_transactions
              -> warehouse_tables -> dbt_models -> reconciliation_report

Each asset calls the same functions `python -m reconpipe` uses, so the DAG adds
dependencies, retries and observability, not a second implementation. The
checks make a bad run visibly fail instead of quietly producing a report.
"""

# No `from __future__ import annotations`: Dagster reads these type hints at runtime.
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    ConfigurableResource,
    MetadataValue,
    Output,
    asset,
    asset_check,
)

from reconpipe.extract import ExtractResult, default_registry
from reconpipe.pipeline import (
    IngestionResult,
    PipelineResult,
    load_ingestion,
    run_dbt,
    validate_extracted,
)
from reconpipe.report import build_reports

ROOT = Path(__file__).resolve().parent.parent
GROUP = "reconpipe"


class ReconPipeSettings(ConfigurableResource):
    """Where the pipeline reads and writes, and what counts as a healthy run."""

    raw_dir: str = str(ROOT / "data" / "raw")
    quarantine_dir: str = str(ROOT / "data" / "quarantine")
    reports_dir: str = str(ROOT / "reports")
    db_path: str = str(ROOT / "warehouse.duckdb")
    dbt_dir: str = str(ROOT / "dbt")
    expected_path: str = str(ROOT / "tests" / "expected_results.json")
    expected_skipped_files: list[str] = ["ledger.xlsx"]
    max_quarantine_rate: float = 0.05
    min_match_rate: float = 0.90


@asset(group_name=GROUP, description="Statement and ledger files in the raw folder.")
def raw_files(reconpipe: ReconPipeSettings) -> Output[list[str]]:
    files = sorted(
        p.name for p in Path(reconpipe.raw_dir).iterdir() if p.is_file() and p.name[0] != "."
    )
    return Output(files, metadata={"files": len(files), "names": ", ".join(files)})


@asset(group_name=GROUP, description="Every statement parsed by its layout's parser.")
def parsed_transactions(
    reconpipe: ReconPipeSettings, raw_files: list[str]
) -> Output[ExtractResult]:
    result = default_registry().parse_directory(Path(reconpipe.raw_dir))
    return Output(
        result,
        metadata={
            "rows": len(result.transactions),
            "files_parsed": MetadataValue.json(result.parsed),
            "files_skipped": len(result.skipped),
        },
    )


@asset(group_name=GROUP, description="Standardised, de-duplicated and validated rows.")
def validated_transactions(
    reconpipe: ReconPipeSettings, parsed_transactions: ExtractResult
) -> Output[IngestionResult]:
    result = validate_extracted(
        parsed_transactions,
        Path(reconpipe.raw_dir),
        Path(reconpipe.quarantine_dir),
        Path(reconpipe.reports_dir),
    )
    s = result.summary
    return Output(
        result,
        metadata={
            "rows_in": s["rows_in"],
            "rows_out": s["rows_out"],
            "duplicates_removed": s["duplicates_removed"],
            "rows_quarantined": s["rows_quarantined"],
            "quarantine_reasons": MetadataValue.json(s["quarantine_reasons"]),
            "quarantine_file": MetadataValue.path(s["quarantine_file"]),
        },
    )


@asset(group_name=GROUP, description="Bank and ledger facts loaded into DuckDB.")
def warehouse_tables(
    reconpipe: ReconPipeSettings, validated_transactions: IngestionResult
) -> Output[PipelineResult]:
    result = load_ingestion(
        validated_transactions,
        Path(reconpipe.raw_dir) / "ledger.xlsx",
        Path(reconpipe.db_path),
    )
    return Output(
        result,
        metadata={
            "bank_rows": result.load.bank_rows,
            "ledger_rows": result.load.ledger_rows,
            "warehouse": MetadataValue.path(str(result.db_path)),
        },
    )


@asset(group_name=GROUP, description="`dbt build`: staging, matching cascade, data tests.")
def dbt_models(
    context: AssetExecutionContext, reconpipe: ReconPipeSettings, warehouse_tables: PipelineResult
) -> str:
    run_dbt(warehouse_tables.db_path, Path(reconpipe.dbt_dir))
    context.add_output_metadata({"dbt_project": MetadataValue.path(reconpipe.dbt_dir)})
    return str(warehouse_tables.db_path)


@asset(group_name=GROUP, description="exceptions.csv, matched.csv and run_summary.json.")
def reconciliation_report(
    reconpipe: ReconPipeSettings, warehouse_tables: PipelineResult, dbt_models: str
) -> Output[dict]:
    report = build_reports(
        Path(dbt_models),
        Path(reconpipe.reports_dir),
        warehouse_tables.ingestion.summary,
        Path(reconpipe.expected_path),
    )
    rec = report.summary["reconciliation"]
    return Output(
        report.summary,
        metadata={
            "match_rate": rec["match_rate"],
            "matched_by_tier": MetadataValue.json(rec["matched_by_tier"]),
            "exceptions": rec["exceptions"],
            "value_in_exception": rec["value_in_exception"],
            "headline": report.summary["headline"],
            "exceptions_file": MetadataValue.path(str(report.exceptions_file)),
        },
    )


# --------------------------------------------------------------------------- #
# Asset checks
# --------------------------------------------------------------------------- #


@asset_check(asset=parsed_transactions, description="Only expected files are skipped.")
def no_statement_is_skipped(
    reconpipe: ReconPipeSettings, parsed_transactions: ExtractResult
) -> AssetCheckResult:
    unexpected = [s.file for s in parsed_transactions.skipped]
    unexpected = [f for f in unexpected if f not in reconpipe.expected_skipped_files]
    return AssetCheckResult(
        passed=not unexpected,
        severity=AssetCheckSeverity.ERROR,
        metadata={"unexpected_skips": ", ".join(unexpected) or "none"},
    )


@asset_check(asset=validated_transactions, description="Few rows are quarantined.")
def quarantine_rate_is_low(
    reconpipe: ReconPipeSettings, validated_transactions: IngestionResult
) -> AssetCheckResult:
    s = validated_transactions.summary
    rate = s["rows_quarantined"] / s["rows_in"] if s["rows_in"] else 1.0
    return AssetCheckResult(
        passed=rate <= reconpipe.max_quarantine_rate,
        severity=AssetCheckSeverity.WARN,
        metadata={"quarantine_rate": round(rate, 4), "limit": reconpipe.max_quarantine_rate},
    )


@asset_check(asset=warehouse_tables, description="Every validated row reached the warehouse.")
def every_validated_row_is_loaded(warehouse_tables: PipelineResult) -> AssetCheckResult:
    expected = len(warehouse_tables.ingestion.rows)
    return AssetCheckResult(
        passed=warehouse_tables.load.bank_rows == expected and expected > 0,
        severity=AssetCheckSeverity.ERROR,
        metadata={"validated": expected, "loaded": warehouse_tables.load.bank_rows},
    )


@asset_check(asset=reconciliation_report, description="Auto-match rate is above the threshold.")
def match_rate_above_threshold(
    reconpipe: ReconPipeSettings, reconciliation_report: dict
) -> AssetCheckResult:
    rate = reconciliation_report["reconciliation"]["match_rate"]
    return AssetCheckResult(
        passed=rate >= reconpipe.min_match_rate,
        severity=AssetCheckSeverity.ERROR,
        metadata={"match_rate": rate, "threshold": reconpipe.min_match_rate},
    )


ASSETS = [
    raw_files,
    parsed_transactions,
    validated_transactions,
    warehouse_tables,
    dbt_models,
    reconciliation_report,
]
CHECKS = [
    no_statement_is_skipped,
    quarantine_rate_is_low,
    every_validated_row_is_loaded,
    match_rate_above_threshold,
]
