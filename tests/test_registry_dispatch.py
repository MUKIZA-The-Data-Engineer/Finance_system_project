import json
import logging
import shutil
from pathlib import Path

import pytest

from reconpipe.extract import (
    BankAPdfParser,
    BankBPdfParser,
    ExcelStatementParser,
    default_registry,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
EXPECTED = json.loads((ROOT / "tests" / "expected_results.json").read_text(encoding="utf-8"))
PRINTED = {Path(s["file"]).name: s["printed_rows"] for s in EXPECTED["statements"]}


@pytest.mark.parametrize(
    ("file_name", "parser"),
    [
        ("bank_a_statement_2026Q1.pdf", BankAPdfParser),
        ("bank_b_statement_2026Q1.pdf", BankBPdfParser),
        ("bank_c_statement_2026Q1.xlsx", ExcelStatementParser),
    ],
)
def test_each_statement_is_claimed_by_exactly_its_parser(file_name, parser):
    # parser_for raises AmbiguousParserError if two parsers claim one file.
    assert default_registry().parser_for(RAW / file_name).name == parser.name


def test_one_loop_combines_all_three_formats():
    result = default_registry().parse_directory(RAW)

    assert result.parsed == {
        "bank_a_statement_2026Q1.pdf": BankAPdfParser.name,
        "bank_b_statement_2026Q1.pdf": BankBPdfParser.name,
        "bank_c_statement_2026Q1.xlsx": ExcelStatementParser.name,
    }
    assert len(result.transactions) == sum(PRINTED.values()) == 291
    for name, count in PRINTED.items():
        assert sum(r.source_file == name for r in result.transactions) == count
    assert {r.account_id for r in result.transactions} == {
        s["account_id"] for s in EXPECTED["statements"]
    }


def test_ledger_is_skipped_not_parsed():
    result = default_registry().parse_directory(RAW)
    assert [s.file for s in result.skipped] == ["ledger.xlsx"]
    assert "no registered parser" in result.skipped[0].reason


def test_unparseable_file_is_logged_and_skipped_never_crashes(tmp_path, caplog):
    shutil.copy(RAW / "bank_c_statement_2026Q1.xlsx", tmp_path)
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 truncated garbage")
    (tmp_path / "notes.txt").write_text("not a statement")
    (tmp_path / ".gitkeep").touch()

    with caplog.at_level(logging.WARNING, logger="reconpipe.extract.registry"):
        result = default_registry().parse_directory(tmp_path)

    assert len(result.transactions) == 85
    assert sorted(s.file for s in result.skipped) == ["broken.pdf", "notes.txt"]
    assert "broken.pdf" in caplog.text and "notes.txt" in caplog.text
    assert ".gitkeep" not in caplog.text
