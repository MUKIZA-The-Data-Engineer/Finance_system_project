"""The Dagster DAG runs end to end, and its checks fail a bad run instead of hiding it."""

from pathlib import Path

import pytest

dagster = pytest.importorskip("dagster")

from orchestration.assets import ASSETS, CHECKS, ReconPipeSettings  # noqa: E402
from orchestration.definitions import defs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def settings(tmp_path: Path, **overrides) -> ReconPipeSettings:
    return ReconPipeSettings(
        raw_dir=str(ROOT / "data" / "raw"),
        quarantine_dir=str(tmp_path / "quarantine"),
        reports_dir=str(tmp_path / "reports"),
        db_path=str(tmp_path / "warehouse.duckdb"),
        dbt_dir=str(ROOT / "dbt"),
        expected_path=str(ROOT / "tests" / "expected_results.json"),
        **overrides,
    )


def materialize(tmp_path, **overrides):
    return dagster.materialize(
        [*ASSETS, *CHECKS], resources={"reconpipe": settings(tmp_path, **overrides)}
    )


def checks(result) -> dict[str, bool]:
    return {e.check_name: e.passed for e in result.get_asset_check_evaluations()}


def test_definitions_load_with_six_assets_a_job_and_a_daily_schedule():
    graph = defs.resolve_asset_graph()
    assert len(list(graph.get_all_asset_keys())) == 6
    assert defs.resolve_job_def("reconcile") is not None
    (schedule,) = defs.schedules
    assert schedule.cron_schedule == "0 6 * * *"


def test_one_materialization_runs_raw_files_to_report(tmp_path):
    result = materialize(tmp_path)
    assert result.success
    assert checks(result) == {
        "no_statement_is_skipped": True,
        "quarantine_rate_is_low": True,
        "every_validated_row_is_loaded": True,
        "match_rate_above_threshold": True,
    }
    report = result.output_for_node("reconciliation_report")
    assert report["reconciliation"]["matched"] == 278
    assert (tmp_path / "reports" / "exceptions.csv").exists()


def test_a_bad_run_is_a_failed_check_not_a_quiet_one(tmp_path):
    result = materialize(tmp_path, min_match_rate=0.99, max_quarantine_rate=0.001)
    evaluations = checks(result)
    assert evaluations["match_rate_above_threshold"] is False
    assert evaluations["quarantine_rate_is_low"] is False
