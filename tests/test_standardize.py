import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from reconpipe.extract import default_registry
from reconpipe.schemas import RawTransaction
from reconpipe.transform import (
    StandardizeError,
    clean_description,
    infer_date_format,
    parse_money,
    standardize,
)

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "tests" / "expected_results.json").read_text(encoding="utf-8"))


def raw(**overrides) -> RawTransaction:
    fields = dict(
        date="15/03/2026",
        description="POS 2743 SIMBA SUPERMARKET",
        ref_id="FT1",
        debit="1,000.00",
        balance="9,000.00",
        account_id="ACC-1",
        source_file="s.pdf",
        row_number=1,
    )
    return RawTransaction(**(fields | overrides))


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("texts", "fmt"),
    [
        (["15/03/2026", "01/02/2026"], "%d/%m/%Y"),
        (["2026-03-15"], "%Y-%m-%d"),
        (["15 Mar 2026", "01 Feb 2026"], "%d %b %Y"),
    ],
)
def test_each_bank_date_style_is_recognised(texts, fmt):
    assert infer_date_format(texts) == fmt


def test_dates_that_read_two_ways_fail_loudly():
    # Every day is 12 or under: 05/01 could be 5 Jan or 1 May.
    with pytest.raises(StandardizeError, match="ambiguous"):
        infer_date_format(["05/01/2026", "12/02/2026"])


def test_one_bad_date_does_not_sink_the_file():
    assert infer_date_format(["15/03/2026", "31/02/2026", "20/03/2026"]) == "%d/%m/%Y"


def test_unknown_date_style_fails():
    with pytest.raises(StandardizeError, match="no known date format"):
        infer_date_format(["March 15th"])


def test_ambiguous_file_is_reported_row_by_row_not_dropped():
    rows = [raw(date="05/01/2026"), raw(date="06/02/2026", row_number=2)]
    result = standardize(rows)
    assert result.clean == [] and len(result.failed) == 2
    assert all("ambiguous" in f.reason for f in result.failed)


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("8,650.00", "8650.00"),
        ("2134850", "2134850.00"),
        ("RWF -702,650.00", "-702650.00"),
        ("RWF 4,659,550.00", "4659550.00"),
        ("-RWF 300", "-300.00"),
        ("6,622,850.00 CR", "6622850.00"),
        ("6,045.10 DR", "-6045.10"),
        ("(1,234.00)", "-1234.00"),
        ("  12.5 ", "12.50"),
    ],
)
def test_money_formats(text, value):
    assert parse_money(text) == Decimal(value)
    assert isinstance(parse_money(text), Decimal)


@pytest.mark.parametrize(
    "text",
    ["", "abc", "1,23.00", "12.345", "1.2.3", "(-5.00)", "-RWF -5", "5.00 CR DR", "1,000.00x"],
)
def test_bad_money_is_rejected(text):
    with pytest.raises(StandardizeError):
        parse_money(text)


# --------------------------------------------------------------------------- #
# Descriptions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "ref", "clean"),
    [
        ("POS 2743 SIMBA SUPERMARKET KIGALI RW", None, "pos simba supermarket kigali rw"),
        ("DD ISHEMA POWER UTILITY ACC 42591", None, "dd ishema power utility acc"),
        ("Receipt: Gisenyi Beach Resort / INV-1151", None, "receipt gisenyi beach resort inv 1151"),
        ("TRF  TO X   LTD REF FT26002VE2T2", "FT26002VE2T2", "trf to x ltd ref"),
    ],
)
def test_description_cleaning(text, ref, clean):
    assert clean_description(text, ref) == clean


def test_raw_description_is_kept_next_to_the_clean_one():
    (row,) = standardize([raw()]).clean
    assert row.description_raw == "POS 2743 SIMBA SUPERMARKET"
    assert row.description_clean == "pos simba supermarket"


# --------------------------------------------------------------------------- #
# Signed amounts
# --------------------------------------------------------------------------- #


def test_debit_and_credit_collapse_into_one_signed_amount():
    out = standardize([raw(), raw(debit=None, credit="500.00", row_number=2)]).clean
    assert [(r.debit, r.credit, r.amount) for r in out] == [
        (Decimal("1000.00"), None, Decimal("-1000.00")),
        (None, Decimal("500.00"), Decimal("500.00")),
    ]


def test_signed_amount_column_is_used_as_is():
    (row,) = standardize([raw(debit=None, amount="RWF -1,000.00")]).clean
    assert (row.debit, row.credit, row.amount) == (None, None, Decimal("-1000.00"))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"debit": None}, "no amount"),
        ({"amount": "500.00"}, "disagrees"),
        ({"debit": "lots"}, "not an amount"),
        ({"date": "31/02/2026"}, "date"),
    ],
)
def test_unusable_rows_fail_with_a_reason(overrides, reason):
    good = raw(date="20/03/2026", row_number=2)
    result = standardize([raw(**overrides), good])
    assert [r.row_number for r in result.clean] == [2]
    (failed,) = result.failed
    assert reason in failed.reason and failed.raw.row_number == 1


# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #


def test_reprinted_line_is_removed_and_points_at_the_original():
    result = standardize([raw(), raw(row_number=2)])
    assert [r.row_number for r in result.clean] == [1]
    (dup,) = result.duplicates
    assert (dup.row.row_number, dup.duplicate_of.row_number) == (2, 1)


def test_identical_purchases_with_different_balances_are_both_kept():
    second = raw(row_number=2, balance="8,000.00")
    assert len(standardize([raw(), second]).clean) == 2


def test_duplicates_are_found_across_files():
    overlap = raw(source_file="next_statement.pdf", row_number=7)
    result = standardize([raw(), overlap])
    assert len(result.clean) == 1 and len(result.duplicates) == 1


# --------------------------------------------------------------------------- #
# All three banks, end to end
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def result():
    raw_rows = default_registry().parse_directory(ROOT / "data" / "raw").transactions
    return standardize(raw_rows)


def test_one_clean_row_per_real_transaction(result):
    assert result.failed == []
    assert len(result.clean) == EXPECTED["totals"]["bank_transactions"] == 290
    (dup,) = result.duplicates
    statement_b = next(s for s in EXPECTED["statements"] if s["duplicate_printed_seqs"])
    (seq,) = statement_b["duplicate_printed_seqs"]
    assert dup.duplicate_of.row_number == seq and dup.row.row_number == seq + 1


def test_clean_rows_match_ground_truth(result):
    truth = {
        (t["account_id"], t["date"], Decimal(t["amount"])): t for t in EXPECTED["bank_transactions"]
    }
    for row in result.clean:
        t = truth[(row.account_id, row.date.isoformat(), row.amount)]
        assert row.balance == Decimal(t["balance"])
        assert row.ref_id == t["ref_id"]
        assert row.description_raw == t["description"]


def test_rows_from_all_banks_are_indistinguishable_by_shape(result):
    banks = {}
    for row in result.clean:
        shape = {
            name: type(value).__name__
            for name, value in row.model_dump().items()
            if name not in ("debit", "credit", "ref_id")  # optional by layout
        }
        banks.setdefault(row.source_file, shape)
    assert len(banks) == 3
    first, *rest = banks.values()
    assert all(shape == first for shape in rest)
    assert first["date"] == "date" and first["amount"] == "Decimal"
    assert all(isinstance(r.date, date) for r in result.clean)
