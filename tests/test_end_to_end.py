"""Raw fixtures in, final match rate out: the whole pipeline in one test.

This is the test that matters most to a reviewer. It runs exactly what
`python -m reconpipe` runs (parse, standardise, validate, load, dbt build,
reports) on the committed statements and checks the outcome against the
generator's ground truth.
"""

import csv
import json
from datetime import date

import pytest

import truth
from reconpipe.pipeline import run_all

pytest.importorskip("dbt.cli.main")


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    return run_all(
        raw_dir=truth.ROOT / "data" / "raw",
        quarantine_dir=tmp / "quarantine",
        reports_dir=tmp / "reports",
        db_path=tmp / "warehouse.duckdb",
        dbt_dir=truth.ROOT / "dbt",
        expected_path=truth.ROOT / "tests" / "expected_results.json",
        today=date(2026, 10, 26),
        run_id="E2E",
    )


def test_final_match_rate_equals_the_ground_truth(result):
    rec = result.report.summary["reconciliation"]
    expected = truth.EXPECTED["expected_matches"]["total_matched_bank_lines"]
    loaded = sum(t["expected_status"] != "quarantined" for t in truth.EXPECTED["bank_transactions"])
    assert rec["matched"] == expected
    assert rec["match_rate"] == round(expected / loaded, 4)


def test_every_item_is_classified_correctly(result):
    acc = result.report.summary["accuracy"]
    assert acc["accuracy"] == 1.0, acc["mismatches"]


def test_planted_statement_defects_are_quarantined(result):
    s = result.report.summary
    assert s["quarantine_reasons"] == truth.EXPECTED["expected_quarantine"]["by_rule"]
    lines = list(csv.DictReader(result.pipeline.ingestion.quarantine_file.open(encoding="utf-8")))
    assert len(lines) == truth.EXPECTED["expected_quarantine"]["rows"]


def test_reports_are_written(result):
    summary = json.loads(result.report.summary_file.read_text(encoding="utf-8"))
    assert summary["headline"].startswith("Reconciles 288 bank transactions")
    exceptions = list(csv.DictReader(result.report.exceptions_file.open(encoding="utf-8")))
    assert len(exceptions) == summary["reconciliation"]["exceptions"] == 16
