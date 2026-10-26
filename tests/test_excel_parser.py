import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from reconpipe.extract import (
    AmbiguousParserError,
    BaseStatementParser,
    ExcelStatementParser,
    ParseError,
    ParserRegistry,
    UnsupportedFileError,
    default_registry,
)
from reconpipe.extract.excel_parser import cell_text

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
BANK_C = RAW / "bank_c_statement_2026Q1.xlsx"
EXPECTED = json.loads((ROOT / "tests" / "expected_results.json").read_text(encoding="utf-8"))
EXPECTED_C = [t for t in EXPECTED["bank_transactions"] if t["file"] == BANK_C.name]


def write_statement(path: Path, rows: list[list]) -> Path:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    return path


HEADER = ["Posting Date", "Narrative", "Txn Ref", "Money Out", "Money In", "Balance"]


def test_row_count_and_first_row_match_the_generator():
    rows = ExcelStatementParser().parse(BANK_C)
    assert len(rows) == len(EXPECTED_C) == 85

    first, truth = rows[0], EXPECTED_C[0]
    assert first.date == truth["date"]
    assert first.description == truth["description"]
    assert first.ref_id == truth["ref_id"]
    assert first.credit == "2134850" and first.debit is None
    assert first.balance == "6045100"
    assert first.account_id == "NCB-7781-0042"
    assert first.source_file == BANK_C.name
    assert first.row_number == truth["excel_row"] == 8


def test_every_row_keeps_its_lineage():
    rows = ExcelStatementParser().parse(BANK_C)
    assert [r.row_number for r in rows] == [t["excel_row"] for t in EXPECTED_C]
    assert [r.ref_id for r in rows] == [t["ref_id"] for t in EXPECTED_C]


def test_registry_dispatches_without_naming_the_parser():
    rows = default_registry().parse(BANK_C)
    assert len(rows) == 85


def test_ledger_and_pdfs_are_not_claimed_by_the_excel_parser():
    parser = ExcelStatementParser()
    assert not parser.can_parse(RAW / "ledger.xlsx")
    assert not parser.can_parse(RAW / "bank_a_statement_2026Q1.pdf")
    with pytest.raises(UnsupportedFileError):
        default_registry().parse(RAW / "ledger.xlsx")


def test_can_parse_never_raises(tmp_path):
    parser = ExcelStatementParser()
    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"this is not a zip file")
    assert not parser.can_parse(corrupt)
    assert not parser.can_parse(tmp_path / "missing.xlsx")


def test_columns_are_found_by_name_not_position(tmp_path):
    path = write_statement(
        tmp_path / "reordered.xlsx",
        [
            ["Account No: X-1"],
            ["Balance", "Money In", "Money Out", "Txn Ref", "Narrative", "Posting Date"],
            [100, None, 50.25, "R1", "Card: Shop", "2026-01-05"],
        ],
    )
    (row,) = ExcelStatementParser().parse(path)
    assert (row.date, row.debit, row.credit, row.balance) == ("2026-01-05", "50.25", None, "100")
    assert row.account_id == "X-1"


def test_missing_account_number_is_a_parse_error(tmp_path):
    path = write_statement(tmp_path / "no_account.xlsx", [HEADER, ["2026-01-05", "x", "R", 1]])
    with pytest.raises(ParseError, match="Account number"):
        ExcelStatementParser().parse(path)


def test_bad_row_reports_its_row_number(tmp_path):
    path = write_statement(
        tmp_path / "bad.xlsx",
        [["Account number: A-1"], HEADER, ["2026-01-05", None, "R1", 10, None, 90]],
    )
    with pytest.raises(ParseError, match="row 3"):
        ExcelStatementParser().parse(path)


def test_cell_text_never_leaks_float_noise():
    assert cell_text(0.1) == "0.1"
    assert cell_text(1250.5) == "1250.5"
    assert cell_text("  ") is None


class _ClaimsEverything(BaseStatementParser):
    name = "greedy"

    def can_parse(self, path):
        return True

    def parse(self, path):
        return []


def test_overlapping_parsers_are_an_error_not_a_guess():
    registry = ParserRegistry([ExcelStatementParser(), _ClaimsEverything()])
    with pytest.raises(AmbiguousParserError, match="greedy"):
        registry.parse(BANK_C)


def test_parser_names_are_unique():
    with pytest.raises(ValueError, match="already registered"):
        ParserRegistry([ExcelStatementParser(), ExcelStatementParser()])
