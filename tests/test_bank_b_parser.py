from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

import truth
from reconpipe.extract import BankBPdfParser, ParseError

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
BANK_B = RAW / "bank_b_statement_2026Q1.pdf"
STATEMENT_B = truth.statement(BANK_B.name)
EXPECTED_B = truth.transactions(BANK_B.name)


def amount_text(amount: str) -> str:
    value = Decimal(amount)
    return f"RWF -{abs(value):,.2f}" if value < 0 else f"RWF {value:,.2f}"


@pytest.fixture(scope="module")
def rows():
    return BankBPdfParser().parse(BANK_B)


def test_every_printed_row_is_read(rows):
    assert len(rows) == STATEMENT_B["printed_rows"] == STATEMENT_B["transactions"] + 1 == 96


def test_rows_match_ground_truth_in_printed_order(rows):
    for row, line in zip(rows, truth.printed(BANK_B.name), strict=True):
        assert row.date == date.fromisoformat(line["date"]).strftime("%d %b %Y")
        assert row.ref_id == line["ref_id"]
        assert row.description == line["description"]
        assert row.amount == amount_text(line["amount"])
        assert row.balance == f"{Decimal(line['balance']):,.2f} CR"
        assert row.debit is None and row.credit is None
    assert [r.row_number for r in rows] == list(range(1, 97))


def test_first_row_and_account(rows):
    first = rows[0]
    assert first.account_id == "200-554-781-903"
    assert first.source_file == BANK_B.name
    assert (first.date, first.amount) == ("05 Jan 2026", "RWF -702,650.00")


def test_wrapped_details_are_joined(rows):
    assert STATEMENT_B["wrapped_descriptions"] > 0
    assert rows[0].description == "Outward transfer Kigali Fresh Logistics Ltd ref inv 1100"


def test_page_break_duplicate_is_kept_for_the_transform_layer(rows):
    (seq,) = STATEMENT_B["duplicate_printed_seqs"]
    copies = [r for r in rows if r.ref_id == EXPECTED_B[seq - 1]["ref_id"]]
    assert len(copies) == 2
    assert copies[0].model_dump(exclude={"row_number"}) == copies[1].model_dump(
        exclude={"row_number"}
    )


def test_balance_marker_lines_are_not_transactions(rows):
    assert not any("BALANCE" in r.description for r in rows)
    assert rows[-1].balance == f"{Decimal(STATEMENT_B['closing_balance']):,.2f} CR"


def unruled_pdf(path: Path, lines: list[tuple[str, str, str, str, str]], account=True):
    """A minimal Bank B style page: (date, ref, details, amount, balance) per line."""
    c = Canvas(str(path), pagesize=A4)
    if account:
        c.drawString(400, 800, "A/C No. 1-2-3")
    y = 740
    for date_, ref, details, amount, balance in [
        ("Value Date", "Ref No.", "Transaction Details", "Amount", "Balance"),
        *lines,
    ]:
        c.drawString(40, y, date_)
        c.drawString(110, y, ref)
        c.drawString(185, y, details)
        c.drawRightString(470, y, amount)
        c.drawRightString(560, y, balance)
        y -= 12
    c.save()
    return path


GOOD = ("05 Jan 2026", "R1", "Card purchase", "RWF -10.00", "90.00 CR")
END = ("", "", "CLOSING BALANCE", "", "90.00 CR")


def test_minimal_statement_parses(tmp_path):
    (row,) = BankBPdfParser().parse(unruled_pdf(tmp_path / "s.pdf", [GOOD, END]))
    assert (row.date, row.ref_id, row.amount, row.account_id) == (
        "05 Jan 2026",
        "R1",
        "RWF -10.00",
        "1-2-3",
    )


def test_continuation_before_any_transaction_is_an_error(tmp_path):
    path = unruled_pdf(tmp_path / "s.pdf", [("", "", "orphan", "", ""), GOOD, END])
    with pytest.raises(ParseError, match="continuation"):
        BankBPdfParser().parse(path)


def test_dated_line_without_amount_is_an_error(tmp_path):
    path = unruled_pdf(tmp_path / "s.pdf", [("05 Jan 2026", "R1", "no amount", "", ""), END])
    with pytest.raises(ParseError, match="unrecognised line"):
        BankBPdfParser().parse(path)


def test_page_without_closing_line_is_an_error(tmp_path):
    with pytest.raises(ParseError, match="closing balance"):
        BankBPdfParser().parse(unruled_pdf(tmp_path / "s.pdf", [GOOD]))


def test_missing_account_is_an_error(tmp_path):
    with pytest.raises(ParseError, match="A/C No"):
        BankBPdfParser().parse(unruled_pdf(tmp_path / "s.pdf", [GOOD, END], account=False))
