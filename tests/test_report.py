import copy
import csv
import json
from decimal import Decimal

import pytest

import truth
from reconpipe.report import REASONS, build_reports, check_accuracy
from reconpipe.report.builder import _fetch

SUMMARY = {"files_parsed": {"a": "x", "b": "y", "c": "z"}, "duration_seconds": 1.0}


@pytest.fixture(scope="module")
def report(built_warehouse, tmp_path_factory):
    _, db = built_warehouse
    out = tmp_path_factory.mktemp("reports")
    return build_reports(db, out, SUMMARY, truth.ROOT / "tests" / "expected_results.json")


def read(path):
    return list(csv.DictReader(path.open(encoding="utf-8")))


def test_exceptions_csv_lists_every_item_that_needs_a_person(report):
    rows = read(report.exceptions_file)
    expected = dict(truth.EXPECTED["expected_classification"])
    for status in ("matched", "quarantined"):
        expected.pop(status)
    assert {s: sum(r["status"] == s for r in rows) for s in expected} == expected
    assert all(r["reason"] == REASONS[r["status"]] for r in rows)


def test_every_exception_points_back_to_its_source(report):
    for r in read(report.exceptions_file):
        if r["status"] in ("missing_in_ledger", "amount_mismatch"):
            assert r["bank_source_file"].startswith("bank_") and int(r["bank_source_row"]) >= 1
        if r["status"] != "missing_in_ledger":
            assert r["ledger_source_file"] == "ledger.xlsx" and r["ledger_source_rows"]


def test_amount_mismatch_shows_both_sides_and_the_difference(report):
    for r in read(report.exceptions_file):
        if r["status"] == "amount_mismatch":
            bank, ledger = Decimal(r["bank_amount"]), Decimal(r["ledger_amount"])
            assert bank != ledger and Decimal(r["difference"]) == ledger - bank


def test_matched_csv_and_match_rates(report):
    assert len(read(report.matched_file)) == 278
    rec = report.summary["reconciliation"]
    assert (rec["bank_lines"], rec["matched"], rec["exceptions"]) == (288, 278, 16)
    assert rec["match_rate"] == round(278 / 288, 4)
    assert rec["matched_by_tier"] == {
        k: truth.EXPECTED["expected_matches"][k]
        for k in ("tier1_exact", "tier2_amount_date", "tier3_fuzzy")
    }


def test_value_reconciled_matches_ground_truth(report):
    expected = sum(
        abs(Decimal(t["amount"]))
        for t in truth.EXPECTED["bank_transactions"]
        if t["expected_status"] == "matched"
    )
    assert Decimal(report.summary["reconciliation"]["value_reconciled"]) == expected


def test_accuracy_is_perfect_against_the_ground_truth(report):
    acc = report.summary["accuracy"]
    assert acc["accuracy"] == 1.0 and acc["mismatches"] == []
    assert acc["items_checked"] == 288 + len(truth.EXPECTED["ledger_only"])


def test_accuracy_check_catches_a_wrong_answer(built_warehouse):
    _, db = built_warehouse
    wrong = copy.deepcopy(truth.EXPECTED)
    first = next(t for t in wrong["bank_transactions"] if t["expected_status"] == "matched")
    first["expected_status"], first["expected_tier"] = "missing_in_ledger", None
    acc = check_accuracy(_fetch(db), wrong)
    assert acc["items_correct"] == acc["items_checked"] - 1
    assert acc["mismatches"][0]["item"].endswith(f"{first['file']} {first['row_number']}")


def test_headline_and_summary_file(report):
    s = json.loads(report.summary_file.read_text(encoding="utf-8"))
    assert s["headline"] == (
        "Reconciles 288 bank transactions across 3 bank formats in 1.0 s, "
        "auto-matching 96.5%, and surfaces the other 16 items as a reviewable exception report."
    )
