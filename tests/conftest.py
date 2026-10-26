from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DBT = ROOT / "dbt"


@pytest.fixture(scope="session")
def built_warehouse(tmp_path_factory):
    """Run the Python pipeline and `dbt build` into a temporary warehouse, once per session.

    Returns (dbt result, warehouse path). Skips when dbt is not installed.
    """
    dbt_main = pytest.importorskip("dbt.cli.main")
    from reconpipe.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("built")
    db = tmp / "warehouse.duckdb"
    run_pipeline(
        ROOT / "data" / "raw",
        tmp / "quarantine",
        tmp / "reports",
        db_path=db,
        today=date(2026, 10, 26),
        run_id="TEST",
    )
    mp = pytest.MonkeyPatch()
    mp.setenv("RECONPIPE_DB", str(db))
    args = ["build", "--project-dir", str(DBT), "--profiles-dir", str(DBT), "--quiet"]
    result = dbt_main.dbtRunner().invoke(args)
    mp.undo()
    return result, db
