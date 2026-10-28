"""The review app renders from the warehouse and its filters work (Streamlit AppTest)."""

from pathlib import Path

import duckdb
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "app" / "dashboard.py"


@pytest.fixture
def warehouse_copy(built_warehouse, tmp_path, monkeypatch):
    # dbt keeps the built file open read-write in this process; the app opens
    # read-only, which DuckDB refuses on the same file, so the app gets a copy.
    # The copy goes through DuckDB, not the file system: on Windows the open
    # file is locked against plain reads, but this process's DuckDB can share it.
    _, db = built_warehouse
    copy = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))  # same config as dbt's connection
    try:
        (source,) = con.execute("SELECT current_database()").fetchone()
        con.execute(f"ATTACH '{copy.as_posix()}' AS app_copy")
        con.execute(f'COPY FROM DATABASE "{source}" TO app_copy')
        con.execute("DETACH app_copy")
    finally:
        con.close()
    monkeypatch.setenv("RECONPIPE_DB", str(copy))
    return copy


def run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    return at.run()


def test_app_renders_the_four_panels(warehouse_copy):
    at = run_app()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {
        "Bank lines": "288",
        "Auto-matched": "96.5%",
        "Need review": "16",
        "Value in exception": "RWF 19,450,550",
        "Quarantined rows": "3",
    }
    subheaders = [h.value for h in at.subheader]
    assert subheaders == [
        "Exceptions to review",
        "Auto-match rate by tier",
        "Data quality: quarantined rows",
    ]
    assert len(at.dataframe[0].value) == 16


def test_status_filter_narrows_the_exceptions(warehouse_copy):
    at = run_app()
    at.multiselect[0].set_value(["Missing in books"]).run()
    table = at.dataframe[0].value
    assert len(table) == 4 and set(table["status"]) == {"Missing in books"}


def test_search_finds_text_in_descriptions(warehouse_copy):
    at = run_app()
    at.text_input[0].input("gisenyi").run()
    table = at.dataframe[0].value
    assert len(table) > 0
    in_bank = table["bank_description"].str.contains("gisenyi", case=False)
    in_ledger = table["ledger_description"].str.contains("gisenyi", case=False)
    assert (in_bank | in_ledger).all()


def test_missing_warehouse_shows_an_instruction(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONPIPE_DB", str(tmp_path / "nope.duckdb"))
    at = run_app()
    assert "python -m reconpipe" in at.error[0].value
