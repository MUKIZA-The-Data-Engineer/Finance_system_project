"""Load validated rows into the DuckDB warehouse (schema in schema.sql).

Idempotent by design: every load replaces, inside one transaction, all rows
that came from the source files in this run. Re-running the same files gives
the same tables, a regenerated file leaves no stale rows behind, and a failure
part-way rolls back to the previous state. Dimensions are upserted, and
pipeline_runs gets one row per run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

from reconpipe.schemas import CleanTransaction, LedgerEntry
from reconpipe.transform import clean_description
from reconpipe.validate import QuarantinedRow

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


@dataclass(frozen=True)
class RunInfo:
    """What pipeline_runs records about one run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    files_parsed: int
    files_skipped: int
    rows_in: int
    duplicates_removed: int
    rows_quarantined: int


@dataclass(frozen=True)
class LoadResult:
    bank_rows: int
    ledger_rows: int
    accounts: int
    counterparties: int


def connect(db_path: Path | str) -> duckdb.DuckDBPyConnection:
    """Open (or create) the warehouse and make sure every table exists."""
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)
    return con


def counterparty_key(name: str) -> str:
    return clean_description(name, None)


def find_counterparty(description_clean: str, keys: list[str]) -> str | None:
    """The longest known counterparty name that appears as whole words in the text."""
    padded = f" {description_clean} "
    hits = [k for k in keys if f" {k} " in padded]
    return max(hits, key=len) if hits else None


def _text(value: object) -> str:
    """'' for missing; never treat Decimal('0.00') as missing."""
    return "" if value is None else str(value)


def _replace_rows(con, table: str, files: set[str]) -> None:
    for name in sorted(files):
        con.execute(f"DELETE FROM {table} WHERE source_file = ?", [name])


def _fill_dates(con, days: Iterable[date]) -> None:
    days = list(days)
    if not days:
        return
    con.execute(
        """
        INSERT INTO dim_date
        SELECT d::DATE, year(d), quarter(d), month(d), monthname(d), day(d), dayname(d),
               dayofweek(d) IN (0, 6)
        FROM range(?::TIMESTAMP, ?::TIMESTAMP + INTERVAL 1 DAY, INTERVAL 1 DAY) AS t(d)
        ON CONFLICT DO NOTHING
        """,
        [min(days), max(days)],
    )


def load_warehouse(
    con: duckdb.DuckDBPyConnection,
    bank_rows: list[CleanTransaction],
    ledger_rows: list[LedgerEntry],
    run: RunInfo,
    quarantined: list[QuarantinedRow] | None = None,
) -> LoadResult:
    names = {
        counterparty_key(e.counterparty): e.counterparty for e in ledger_rows if e.counterparty
    }
    keys = sorted(names)
    statement_file = {r.account_id: r.source_file for r in bank_rows}
    ledger_accounts = {e.account_id for e in ledger_rows}

    con.begin()
    try:
        _replace_rows(con, "fact_bank_transactions", {r.source_file for r in bank_rows})
        _replace_rows(con, "fact_ledger_entries", {e.source_file for e in ledger_rows})

        _fill_dates(con, [r.date for r in bank_rows] + [e.date for e in ledger_rows])
        con.executemany(
            "INSERT OR REPLACE INTO dim_account VALUES (?, ?, ?)",
            [
                [account, statement_file.get(account), account in ledger_accounts]
                for account in sorted(statement_file.keys() | ledger_accounts)
            ],
        )
        if names:
            con.executemany(
                "INSERT OR REPLACE INTO dim_counterparty VALUES (?, ?)",
                [[k, names[k]] for k in keys],
            )

        if bank_rows:
            con.executemany(
                "INSERT INTO fact_bank_transactions VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        r.txn_hash,
                        r.account_id,
                        r.date,
                        r.amount,
                        r.debit,
                        r.credit,
                        r.balance,
                        r.ref_id,
                        r.description_raw,
                        r.description_clean,
                        find_counterparty(r.description_clean, keys),
                        r.source_file,
                        r.row_number,
                        run.run_id,
                    ]
                    for r in bank_rows
                ],
            )
        if ledger_rows:
            con.executemany(
                "INSERT INTO fact_ledger_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        e.entry_id,
                        e.account_id,
                        e.date,
                        e.amount,
                        e.ref_id,
                        e.description,
                        counterparty_key(e.counterparty) if e.counterparty else None,
                        e.gl_account,
                        e.source_file,
                        e.row_number,
                        run.run_id,
                    ]
                    for e in ledger_rows
                ],
            )

        con.execute("DELETE FROM quarantined_rows")  # always the latest run's view
        if quarantined:
            con.executemany(
                "INSERT INTO quarantined_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        run.run_id,
                        ";".join(q.rules),
                        "; ".join(v.message for v in q.violations),
                        q.row.source_file,
                        q.row.row_number,
                        q.row.account_id,
                        str(q.row.date),
                        getattr(q.row, "description_raw", None)
                        or getattr(q.row, "description", None),
                        _text(getattr(q.row, "amount", None)),
                        _text(q.row.balance),
                    ]
                    for q in quarantined
                ],
            )

        con.execute(
            "INSERT OR REPLACE INTO pipeline_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run.run_id,
                run.started_at,
                run.finished_at,
                (run.finished_at - run.started_at).total_seconds(),
                run.files_parsed,
                run.files_skipped,
                run.rows_in,
                run.duplicates_removed,
                run.rows_quarantined,
                len(bank_rows),
                len(ledger_rows),
            ],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return LoadResult(
        bank_rows=len(bank_rows),
        ledger_rows=len(ledger_rows),
        accounts=len(statement_file.keys() | ledger_accounts),
        counterparties=len(names),
    )
