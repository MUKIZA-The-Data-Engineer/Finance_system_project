import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from reconpipe import CleanTransaction, RawTransaction

HASH = "a" * 64


def raw(**overrides):
    fields = dict(
        date="15 Mar 2026",
        description="  POS PURCHASE  SIMBA SUPERMARKET ",
        ref_id="",
        debit="12,500.00",
        credit=None,
        balance="RWF 1,040,000.00",
        account_id="BK-A-001",
        source_file="bank_a_march.pdf",
        row_number=7,
    )
    return RawTransaction(**(fields | overrides))


def clean(**overrides):
    fields = dict(
        date=dt.date(2026, 3, 15),
        description_raw="POS PURCHASE  SIMBA SUPERMARKET",
        description_clean="pos purchase simba supermarket",
        debit="12500.00",
        amount="-12500.00",
        balance="1040000.00",
        account_id="BK-A-001",
        source_file="bank_a_march.pdf",
        row_number=7,
        txn_hash=HASH,
    )
    return CleanTransaction(**(fields | overrides))


def test_raw_keeps_text_and_normalises_blanks():
    row = raw()
    assert row.debit == "12,500.00"
    assert row.description == "POS PURCHASE  SIMBA SUPERMARKET"
    assert row.ref_id is None


def test_raw_is_immutable():
    with pytest.raises(ValidationError):
        raw().date = "16 Mar 2026"


def test_raw_rejects_unknown_fields_and_bad_row_number():
    with pytest.raises(ValidationError):
        raw(memo="typo field")
    with pytest.raises(ValidationError):
        raw(row_number=0)


def test_clean_types_money_as_decimal():
    row = clean()
    assert row.amount == Decimal("-12500.00")
    assert isinstance(row.debit, Decimal)


def test_clean_rejects_float_money():
    with pytest.raises(ValidationError, match="never float"):
        clean(debit=12500.0, amount="-12500.00")


def test_clean_rejects_amount_that_disagrees_with_columns():
    with pytest.raises(ValidationError, match="credit - debit"):
        clean(amount="12500.00")


def test_clean_signed_column_layout_needs_no_debit_credit():
    row = clean(debit=None, amount="-300.00")
    assert row.amount == Decimal("-300.00")


def test_clean_rejects_negative_debit_and_bad_hash():
    with pytest.raises(ValidationError):
        clean(debit="-12500.00", amount="12500.00")
    with pytest.raises(ValidationError):
        clean(txn_hash="not-a-hash")
