from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from openpyxl import Workbook

import truth
from reconpipe.extract import ParseError
from reconpipe.extract.ledger import read_ledger
from reconpipe.load import RunInfo, connect, load_warehouse
from reconpipe.load.warehouse import find_counterparty
from reconpipe.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
TODAY = date(2026, 10, 26)
LOADED_BANK_ROWS = sum(
    t["expected_status"] != "quarantined" for t in truth.EXPECTED["bank_transactions"]
)


def run(tmp_path, run_id="RUN1"):
    return run_pipeline(
        RAW,
        tmp_path / "quarantine",
        tmp_path / "reports",
        db_path=tmp_path / "warehouse.duckdb",
        today=TODAY,
        run_id=run_id,
    )


def query(db: Path, sql: str, params=None) -> list[tuple]:
    with closing(duckdb.connect(str(db), read_only=True)) as con:
        return con.execute(sql, params or []).fetchall()


def count(db: Path, table: str) -> int:
    return query(db, f"SELECT count(*) FROM {table}")[0][0]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return run(tmp_path_factory.mktemp("wh")).db_path


# --------------------------------------------------------------------------- #
# Ledger reader
# --------------------------------------------------------------------------- #


def test_ledger_is_read_typed_and_complete():
    entries = read_ledger(RAW / "ledger.xlsx")
    assert len(entries) == truth.EXPECTED["ledger"]["entries"] == 296
    first = entries[0]
    assert first.entry_id == "JE-2026-0001" and first.row_number == 2
    assert isinstance(first.date, date) and isinstance(first.amount, Decimal)
    assert {e.account_id for e in entries} == {
        s["account_id"] for s in truth.EXPECTED["statements"]
    }


def test_ledger_missing_columns_is_a_parse_error(tmp_path):
    wb = Workbook()
    wb.active.append(["entry_id", "posting_date", "amount"])
    wb.save(tmp_path / "ledger.xlsx")
    with pytest.raises(ParseError, match="missing columns"):
        read_ledger(tmp_path / "ledger.xlsx")


def test_unreadable_ledger_is_a_parse_error(tmp_path):
    (tmp_path / "ledger.xlsx").write_bytes(b"not a workbook")
    with pytest.raises(ParseError, match="cannot open ledger"):
        read_ledger(tmp_path / "ledger.xlsx")


# --------------------------------------------------------------------------- #
# Warehouse contents
# --------------------------------------------------------------------------- #


def test_every_validated_row_and_ledger_entry_is_loaded(db):
    assert count(db, "fact_bank_transactions") == LOADED_BANK_ROWS == 288
    assert count(db, "fact_ledger_entries") == 296
    assert count(db, "dim_account") == 3
    assert count(db, "pipeline_runs") == 1


def test_quarantined_rows_never_reach_the_warehouse(db):
    for d in truth.defects():
        hits = query(
            db,
            "SELECT count(*) FROM fact_bank_transactions WHERE source_file = ? AND row_number = ?",
            [d["file"], d["row_number"]],
        )
        assert hits == [(0,)]


def test_every_fact_date_is_in_dim_date(db):
    for table in ("fact_bank_transactions", "fact_ledger_entries"):
        orphans = query(
            db, f"SELECT count(*) FROM {table} f ANTI JOIN dim_date d ON f.date_key = d.date_key"
        )
        assert orphans == [(0,)]


def test_grain_is_one_row_per_transaction_per_source(db):
    dupes = query(
        db,
        "SELECT source_file, row_number FROM fact_bank_transactions "
        "GROUP BY ALL HAVING count(*) > 1",
    )
    assert dupes == []


def test_total_debits_per_account_per_month_in_one_query(db):
    rows = query(
        db,
        """
        SELECT account_id, strftime(date_key, '%Y-%m') AS month,
               sum(-amount) FILTER (WHERE amount < 0) AS total_debits
        FROM fact_bank_transactions
        GROUP BY ALL ORDER BY ALL
        """,
    )
    expected: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for t in truth.EXPECTED["bank_transactions"]:
        if t["expected_status"] != "quarantined" and Decimal(t["amount"]) < 0:
            expected[(t["account_id"], t["date"][:7])] -= Decimal(t["amount"])
    assert {(a, m): v for a, m, v in rows} == dict(expected)


def test_counterparties_are_found_in_bank_text(db):
    rows = query(
        db,
        "SELECT description_clean, counterparty_key FROM fact_bank_transactions "
        "WHERE counterparty_key IS NOT NULL",
    )
    assert len(rows) > 200
    assert all(f" {key} " in f" {text} " for text, key in rows)


def test_longest_counterparty_name_wins():
    keys = ["kivu", "kivu hotels group ltd"]
    assert find_counterparty("trf from kivu hotels group ltd inv 1", keys) == keys[1]
    assert find_counterparty("monthly fee", keys) is None
    assert find_counterparty("kivuhotels", keys) is None  # whole words only


# --------------------------------------------------------------------------- #
# Idempotency and failure safety
# --------------------------------------------------------------------------- #


def snapshot(db: Path) -> list[tuple]:
    return query(
        db,
        "SELECT * EXCLUDE (run_id) FROM fact_bank_transactions ORDER BY source_file, row_number",
    )


def test_rerunning_does_not_double_load(tmp_path):
    first = run(tmp_path, "RUN1").db_path
    before = snapshot(first)
    run(tmp_path, "RUN2")
    assert snapshot(first) == before
    assert count(first, "fact_ledger_entries") == 296
    assert [r[0] for r in query(first, "SELECT run_id FROM pipeline_runs ORDER BY 1")] == [
        "RUN1",
        "RUN2",
    ]


def info(run_id: str = "R") -> RunInfo:
    now = datetime(2026, 10, 26, 8, 0)
    return RunInfo(run_id, now, now, 1, 0, 0, 0, 0)


def test_reloading_a_file_leaves_no_stale_rows(tmp_path, db):
    rows = run(tmp_path).ingestion.rows
    with closing(connect(tmp_path / "w.duckdb")) as con:
        load_warehouse(con, rows, [], info("A"))
        half = [r for r in rows if r.row_number % 2 == 0]
        load_warehouse(con, half, [], info("B"))
        assert con.execute("SELECT count(*) FROM fact_bank_transactions").fetchone() == (len(half),)


def test_failed_load_rolls_back_to_the_previous_state(tmp_path):
    rows = run(tmp_path).ingestion.rows
    with closing(connect(tmp_path / "w.duckdb")) as con:
        load_warehouse(con, rows, [], info("A"))
        with pytest.raises(duckdb.ConstraintException):
            load_warehouse(con, [*rows, rows[0]], [], info("B"))  # duplicate key mid-load
        assert con.execute("SELECT count(*) FROM fact_bank_transactions").fetchone() == (len(rows),)
        assert con.execute("SELECT run_id FROM pipeline_runs").fetchall() == [("A",)]
