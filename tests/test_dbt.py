"""dbt build must be green on a freshly loaded warehouse.

Runs the whole Python pipeline into a temporary DuckDB file, points the dbt
profile at it with RECONPIPE_DB, and runs `dbt build` (models + data tests).
"""

from datetime import date
from pathlib import Path

import pytest

from reconpipe.pipeline import run_pipeline

dbt_main = pytest.importorskip("dbt.cli.main")

ROOT = Path(__file__).resolve().parent.parent
DBT = ROOT / "dbt"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbt")
    db = tmp / "warehouse.duckdb"
    run_pipeline(
        ROOT / "data" / "raw",
        tmp / "quarantine",
        tmp / "reports",
        db_path=db,
        today=date(2026, 10, 26),
        run_id="DBT",
    )
    mp = pytest.MonkeyPatch()
    mp.setenv("RECONPIPE_DB", str(db))
    args = ["build", "--project-dir", str(DBT), "--profiles-dir", str(DBT), "--quiet"]
    result = dbt_main.dbtRunner().invoke(args)
    mp.undo()
    return result, db


def test_dbt_build_is_green(built):
    result, _ = built
    assert result.exception is None
    assert result.success, [r.message for r in result.result if r.status not in ("pass", "success")]


def test_staging_views_exist_with_every_row(built):
    import duckdb

    _, db = built
    con = duckdb.connect(str(db))  # same config as the connection dbt keeps open in-process
    try:
        bank = con.execute("SELECT count(*) FROM stg_bank_transactions").fetchone()[0]
        ledger = con.execute("SELECT count(*) FROM stg_ledger_entries").fetchone()[0]
        directions = con.execute(
            "SELECT DISTINCT direction FROM stg_bank_transactions ORDER BY 1"
        ).fetchall()
    finally:
        con.close()
    assert (bank, ledger) == (288, 296)
    assert directions == [("in",), ("out",)]
