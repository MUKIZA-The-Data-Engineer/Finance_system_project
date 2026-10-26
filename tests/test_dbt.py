"""dbt build must be green on a freshly loaded warehouse (see conftest.built_warehouse)."""

import duckdb


def query(db, sql):
    # Same config as the connection dbt-duckdb keeps open in this process.
    con = duckdb.connect(str(db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_dbt_build_is_green(built_warehouse):
    result, _ = built_warehouse
    assert result.exception is None
    assert result.success, [r.message for r in result.result if r.status not in ("pass", "success")]


def test_staging_views_exist_with_every_row(built_warehouse):
    _, db = built_warehouse
    assert query(db, "SELECT count(*) FROM stg_bank_transactions") == [(288,)]
    assert query(db, "SELECT count(*) FROM stg_ledger_entries") == [(296,)]
    assert query(db, "SELECT DISTINCT direction FROM stg_bank_transactions ORDER BY 1") == [
        ("in",),
        ("out",),
    ]
