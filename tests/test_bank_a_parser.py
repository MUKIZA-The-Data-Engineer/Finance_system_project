from decimal import Decimal
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

import truth
from reconpipe.extract import BankAPdfParser, ParseError, default_registry

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
BANK_A = RAW / "bank_a_statement_2026Q1.pdf"
STATEMENT_A = truth.statement(BANK_A.name)
PRINTED_A = truth.printed(BANK_A.name)


@pytest.fixture(scope="module")
def rows():
    return BankAPdfParser().parse(BANK_A)


def money_text(amount: str) -> str:
    return f"{abs(Decimal(amount)):,.2f}"


def test_row_count_matches_the_generator(rows):
    # 110 transactions plus the planted 0.00 reversal line
    assert len(rows) == STATEMENT_A["printed_rows"] == STATEMENT_A["transactions"] + 1 == 111
    assert STATEMENT_A["pages"] > 1  # the count spans a multi-page statement


def test_first_row_values(rows):
    first = rows[0]
    assert (first.date, first.ref_id, first.debit, first.credit, first.balance) == (
        "01/01/2026",
        "FT2600186JSF",
        "8,650.00",
        None,
        "18,441,350.00",
    )
    assert first.description == "POS 2743 SIMBA SUPERMARKET KIGALI RW"
    assert first.account_id == "0040-1187342-01"
    assert first.source_file == BANK_A.name
    assert first.row_number == 1


def test_wrapped_descriptions_are_joined(rows):
    assert STATEMENT_A["wrapped_descriptions"] > 0
    assert [r.description for r in rows] == [t["description"] for t in PRINTED_A]
    assert rows[1].description == "TRF TO AKAGERA OFFICE SUPPLIES LTD INV 1041"


def test_every_row_matches_ground_truth(rows):
    for position, (row, line) in enumerate(zip(rows, PRINTED_A, strict=True), start=1):
        assert row.row_number == position
        assert row.ref_id == line["ref_id"]
        assert row.balance == money_text(line["balance"])
        amount = row.credit if Decimal(line["amount"]) > 0 else row.debit
        assert amount == money_text(line["amount"])
        assert (row.debit is None) != (row.credit is None)


def test_planted_zero_line_is_read_as_printed(rows):
    (defect,) = truth.defects(BANK_A.name)
    row = rows[defect["row_number"] - 1]
    assert (row.description, row.debit, row.credit) == ("CARD AUTHORISATION REVERSAL", "0.00", None)


def test_last_row_on_the_last_page_is_read(rows):
    assert rows[-1].balance == money_text(STATEMENT_A["closing_balance"])


def test_registry_dispatches_bank_a():
    registry = default_registry()
    assert registry.parser_for(BANK_A).name == BankAPdfParser.name
    assert len(registry.parse(BANK_A)) == STATEMENT_A["printed_rows"]


def test_only_bank_a_is_claimed(tmp_path):
    parser = BankAPdfParser()
    assert not parser.can_parse(RAW / "bank_b_statement_2026Q1.pdf")
    assert not parser.can_parse(RAW / "bank_c_statement_2026Q1.xlsx")
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really")
    assert not parser.can_parse(corrupt)
    assert not parser.can_parse(tmp_path / "missing.pdf")


def ruled_pdf(path: Path, *pages: list[list[str]], account_line: str = "Account number: X-9"):
    """A minimal Bank A style statement: one ruled table per page, header repeated."""
    c = Canvas(str(path), pagesize=A4)
    xs = [40, 100, 300, 380, 440, 500, 560]
    top, h = 760, 14
    for rows in pages:
        c.drawString(40, 800, account_line)
        lines = [["Date", "Description", "Reference", "Debit", "Credit", "Balance"], *rows]
        for i in range(len(lines) + 1):
            c.line(xs[0], top - i * h, xs[-1], top - i * h)
        for x in xs:
            c.line(x, top, x, top - len(lines) * h)
        for i, cells in enumerate(lines):
            for x, text in zip(xs, cells, strict=False):
                c.drawString(x + 3, top - i * h - 10, text)
        c.showPage()
    c.save()
    return path


def test_description_wrapped_across_a_page_break_is_joined(tmp_path):
    path = ruled_pdf(
        tmp_path / "s.pdf",
        [["01/01/2026", "TRF TO SOME VERY LONG", "R1", "10.00", "", "90.00"]],
        [["", "SUPPLIER NAME", "", "", "", ""], ["02/01/2026", "FEE", "", "1.00", "", "89.00"]],
    )
    first, second = BankAPdfParser().parse(path)
    assert first.description == "TRF TO SOME VERY LONG SUPPLIER NAME"
    assert (second.row_number, second.description) == (2, "FEE")


def test_continuation_before_any_transaction_is_an_error(tmp_path):
    path = ruled_pdf(tmp_path / "s.pdf", [["", "orphan text", "", "", "", ""]])
    with pytest.raises(ParseError, match="continuation"):
        BankAPdfParser().parse(path)


def test_undated_row_with_amounts_is_an_error(tmp_path):
    path = ruled_pdf(tmp_path / "s.pdf", [["", "no date", "R1", "10.00", "", "90.00"]])
    with pytest.raises(ParseError, match="undated"):
        BankAPdfParser().parse(path)


def test_missing_account_number_is_an_error(tmp_path):
    rows = [["01/01/2026", "x", "R1", "10.00", "", "90.00"]]
    path = ruled_pdf(tmp_path / "s.pdf", rows, account_line="no account here")
    with pytest.raises(ParseError, match="Account number"):
        BankAPdfParser().parse(path)
