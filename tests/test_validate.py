import csv
import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from reconpipe.pipeline import describe, run_ingestion
from reconpipe.schemas import CleanTransaction
from reconpipe.validate import (
    AMOUNT_NON_ZERO,
    BALANCE_CONTINUITY,
    CONTRACT,
    DATE_NOT_IN_FUTURE,
    validate,
    write_quarantine,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
TODAY = date(2026, 10, 26)


def row(n: int, amount: str, balance: str | None, **overrides) -> CleanTransaction:
    fields = dict(
        date=date(2026, 3, n),
        description_raw=f"line {n}",
        description_clean=f"line {n}",
        amount=amount,
        balance=balance,
        account_id="ACC-1",
        source_file="s.pdf",
        row_number=n,
        txn_hash=f"{n:064x}",
    )
    return CleanTransaction(**(fields | overrides))


def statement(*lines: tuple[str, str | None]) -> list[CleanTransaction]:
    return [row(i, a, b) for i, (a, b) in enumerate(lines, start=1)]


def quarantined_rows(result) -> dict[int, tuple[str, ...]]:
    return {q.row.row_number: q.rules for q in result.quarantined}


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def test_clean_statement_passes():
    result = validate(statement(("100", "1100"), ("-50", "1050"), ("25", "1075")), TODAY)
    assert len(result.passed) == 3 and result.quarantined == []


def test_future_date_is_quarantined():
    result = validate([row(1, "10", None, date=date(2026, 10, 27))], TODAY)
    assert quarantined_rows(result) == {1: (DATE_NOT_IN_FUTURE,)}


def test_today_is_not_the_future():
    assert validate([row(1, "10", None, date=TODAY)], TODAY).quarantined == []


def test_zero_amount_is_quarantined():
    assert quarantined_rows(validate([row(1, "0", None)], TODAY)) == {1: (AMOUNT_NON_ZERO,)}


def test_misprinted_balance_fails_only_its_own_line():
    # Line 2 should read 1050; the next line follows from the correct value.
    lines = statement(("100", "1100"), ("-50", "1060"), ("25", "1075"), ("5", "1080"))
    assert quarantined_rows(validate(lines, TODAY)) == {2: (BALANCE_CONTINUITY,)}


def test_misread_amount_fails_only_its_own_line():
    # Line 2's amount should be -50; its printed balance (1050) is right.
    lines = statement(("100", "1100"), ("-40", "1050"), ("25", "1075"))
    assert quarantined_rows(validate(lines, TODAY)) == {2: (BALANCE_CONTINUITY,)}


def test_balance_is_checked_in_statement_order_per_file_and_account():
    lines = statement(("100", "1100"), ("-50", "1050"), ("25", "1075"))
    other = [row(1, "5", "5", source_file="t.pdf"), row(2, "5", "10", source_file="t.pdf")]
    assert validate(list(reversed(lines)) + other, TODAY).quarantined == []


def test_rows_without_a_printed_balance_skip_the_check():
    lines = statement(("100", None), ("-50", None), ("25", "999"), ("1", "1000"))
    assert validate(lines, TODAY).quarantined == []


def test_row_breaking_two_rules_is_quarantined_once_with_both():
    lines = [row(1, "100", "1100"), row(2, "0", "1200", date=date(2026, 12, 1))]
    (q,) = validate(lines, TODAY).quarantined
    assert set(q.rules) == {DATE_NOT_IN_FUTURE, AMOUNT_NON_ZERO, BALANCE_CONTINUITY}


# --------------------------------------------------------------------------- #
# Quarantine file
# --------------------------------------------------------------------------- #


def test_quarantine_file_keeps_row_rule_and_lineage(tmp_path):
    result = validate([row(1, "0", None, row_number=7)], TODAY)
    path = write_quarantine(result.quarantined, tmp_path, "RUN1")
    (line,) = csv.DictReader(path.open(encoding="utf-8"))
    assert (line["run_id"], line["rules"], line["source_file"], line["row_number"]) == (
        "RUN1",
        AMOUNT_NON_ZERO,
        "s.pdf",
        "7",
    )
    assert Decimal(json.loads(line["row_json"])["amount"]) == 0


def test_empty_quarantine_still_writes_a_header(tmp_path):
    path = write_quarantine([], tmp_path, "RUN1")
    assert path.read_text(encoding="utf-8").startswith("run_id,rules,reasons")


# --------------------------------------------------------------------------- #
# Whole run
# --------------------------------------------------------------------------- #


def run(tmp_path, raw_dir):
    return run_ingestion(
        raw_dir, tmp_path / "quarantine", tmp_path / "reports", today=TODAY, run_id="TEST"
    )


def test_generated_data_passes_end_to_end(tmp_path):
    result = run(tmp_path, RAW)
    s = result.summary
    assert (s["rows_in"], s["duplicates_removed"], s["rows_quarantined"], s["rows_out"]) == (
        291,
        1,
        0,
        290,
    )
    assert [f["file"] for f in s["files_skipped"]] == ["ledger.xlsx"]
    assert s["per_file"]["bank_b_statement_2026Q1.pdf"]["duplicates_removed"] == 1
    assert json.loads(result.summary_file.read_text(encoding="utf-8")) == s
    assert describe(s) == (
        "Of 291 rows, 290 passed, 1 duplicate(s) were removed "
        "and 0 were quarantined (no rule broken)."
    )


@pytest.fixture
def damaged_raw(tmp_path):
    """A copy of data/raw with three defects planted in the Bank C statement."""
    raw = tmp_path / "raw"
    shutil.copytree(RAW, raw)
    path = raw / "bank_c_statement_2026Q1.xlsx"
    wb = load_workbook(path)
    ws = wb.active
    ws.cell(row=10, column=6).value += 1  # misprinted balance
    ws.cell(row=20, column=1).value = "2027-01-15"  # future date
    ws.insert_rows(31)  # zero-value line, balance unchanged
    for col, value in enumerate(
        [ws.cell(30, 1).value, "Card authorisation", None, 0, None, ws.cell(30, 6).value],
        start=1,
    ):
        ws.cell(row=31, column=col).value = value
    wb.save(path)
    return raw


def test_planted_defects_are_quarantined_for_the_right_reasons(tmp_path, damaged_raw):
    result = run(tmp_path, damaged_raw)
    s = result.summary
    assert s["rows_in"] == 292
    assert s["rows_quarantined"] == 3
    assert s["rows_out"] == 292 - 1 - 3
    assert s["quarantine_reasons"] == {
        AMOUNT_NON_ZERO: 1,
        BALANCE_CONTINUITY: 1,
        DATE_NOT_IN_FUTURE: 1,
    }
    found = {(q.row.row_number, q.rules) for q in result.quarantined}
    assert found == {
        (10, (BALANCE_CONTINUITY,)),
        (20, (DATE_NOT_IN_FUTURE,)),
        (31, (AMOUNT_NON_ZERO,)),
    }
    lines = list(csv.DictReader(result.quarantine_file.open(encoding="utf-8")))
    assert len(lines) == 3 and {ln["source_file"] for ln in lines} == {
        "bank_c_statement_2026Q1.xlsx"
    }
    assert describe(s).startswith("Of 292 rows, 288 passed")


def test_unreadable_amount_is_quarantined_as_contract_failure(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    wb = Workbook()
    for line in [
        ["Account number: A-1"],
        ["Posting Date", "Narrative", "Txn Ref", "Money Out", "Money In", "Balance"],
        ["2026-01-05", "Card: Shop", "R1", "n/a", None, 90],
        ["2026-01-06", "Card: Cafe", "R2", 10, None, 80],
    ]:
        wb.active.append(line)
    wb.save(raw / "c.xlsx")

    result = run(tmp_path, raw)
    assert result.summary["quarantine_reasons"] == {CONTRACT: 1}
    (q,) = result.quarantined
    assert q.row.row_number == 3 and "not an amount" in q.violations[0].message
    assert [r.amount for r in result.rows] == [Decimal("-10.00")]
