"""ReconPipe review app: what an accountant needs to act on, read straight from DuckDB.

    streamlit run app/dashboard.py        # after `python -m reconpipe`

Four things only: the run's headline numbers, a filterable exceptions table you
can download, the match rate by tier, and the rows quarantined for data quality.
Set RECONPIPE_DB to point at another warehouse file.
"""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from reconpipe.report import REASONS

DB_PATH = Path(os.environ.get("RECONPIPE_DB", "warehouse.duckdb"))
SERIES = "#2a78d6"  # one series, one hue
TIER_LABELS = {
    "tier1_exact": "1 · exact reference",
    "tier2_amount_date": "2 · amount + date",
    "tier3_fuzzy": "3 · fuzzy description",
}
STATUS_LABELS = {
    "amount_mismatch": "Amount mismatch",
    "missing_in_ledger": "Missing in books",
    "missing_in_bank": "Missing on statement",
    "duplicate_suspected": "Suspected double posting",
}


def query(db_path: Path, sql: str) -> pd.DataFrame:
    with closing(duckdb.connect(str(db_path), read_only=True)) as con:
        return con.execute(sql).df()


def latest_run(db_path: Path) -> dict:
    df = query(db_path, "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
    return df.iloc[0].to_dict() if len(df) else {}


def headline_numbers(db_path: Path) -> dict:
    row = query(
        db_path,
        """
        SELECT
            count(*) FILTER (WHERE bank_txn_id IS NOT NULL)            AS bank_lines,
            count(*) FILTER (WHERE status = 'matched')                 AS matched,
            count(*) FILTER (WHERE status <> 'matched')                AS exceptions,
            coalesce(sum(abs(coalesce(bank_amount, ledger_amount)))
                FILTER (WHERE status <> 'matched'), 0)                 AS value_in_exception
        FROM recon_results
        """,
    ).iloc[0]
    quarantined = int(query(db_path, "SELECT count(*) AS n FROM quarantined_rows").iloc[0]["n"])
    bank = int(row["bank_lines"])
    return {
        "bank_lines": bank,
        "matched": int(row["matched"]),
        "match_rate": row["matched"] / bank if bank else 0.0,
        "exceptions": int(row["exceptions"]),
        "value_in_exception": float(row["value_in_exception"]),
        "quarantined": quarantined,
    }


def exceptions(db_path: Path) -> pd.DataFrame:
    df = query(
        db_path,
        """
        SELECT status, account_id, coalesce(bank_amount, ledger_amount) AS amount, difference,
               bank_date, bank_ref, bank_description,
               bank_source_file || ' row ' || bank_source_row AS bank_source,
               array_to_string(ledger_entry_ids, ', ')         AS ledger_entries,
               ledger_date, ledger_description,
               ledger_source_file || ' row ' || ledger_source_rows AS ledger_source,
               detail
        FROM recon_results
        WHERE status <> 'matched'
        ORDER BY status, account_id, coalesce(bank_date, ledger_date)
        """,
    )
    df.insert(1, "reason", df["status"].map(REASONS))
    df["status"] = df["status"].map(STATUS_LABELS)
    for col in ("bank_date", "ledger_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    dates = {"bank_date", "ledger_date"}
    text = [c for c in df.columns if c not in dates and pd.api.types.is_string_dtype(df[c])]
    df[text] = df[text].fillna("")  # a blank cell, not the word "None"
    return df


def match_rate_by_tier(db_path: Path) -> pd.DataFrame:
    df = query(
        db_path,
        """
        SELECT match_tier, count(*) AS matched
        FROM recon_results WHERE status = 'matched' GROUP BY match_tier
        """,
    )
    bank = headline_numbers(db_path)["bank_lines"]
    df["tier"] = df["match_tier"].map(TIER_LABELS)
    df["share_of_bank_lines"] = df["matched"] / bank if bank else 0.0
    return df.sort_values("tier")[["tier", "matched", "share_of_bank_lines"]]


def quarantine_by_rule(db_path: Path) -> pd.DataFrame:
    return query(
        db_path,
        """
        SELECT rule, count(*) AS rows
        FROM (SELECT unnest(string_split(rules, ';')) AS rule FROM quarantined_rows)
        GROUP BY rule ORDER BY rows DESC, rule
        """,
    )


def quarantined_rows(db_path: Path) -> pd.DataFrame:
    return query(
        db_path,
        "SELECT rules, reasons, source_file, row_number, account_id, date, description, "
        "amount, balance FROM quarantined_rows ORDER BY source_file, row_number",
    )


def tier_chart(df: pd.DataFrame) -> alt.LayerChart:
    base = alt.Chart(df).encode(
        y=alt.Y("tier:N", title=None, sort=None),
        x=alt.X(
            "share_of_bank_lines:Q",
            title="Share of bank lines",
            axis=alt.Axis(format="%", grid=True, gridOpacity=0.3),
            scale=alt.Scale(domain=[0, 1]),
        ),
        tooltip=[
            alt.Tooltip("tier:N", title="Tier"),
            alt.Tooltip("matched:Q", title="Lines matched"),
            alt.Tooltip("share_of_bank_lines:Q", title="Share", format=".1%"),
        ],
    )
    bars = base.mark_bar(color=SERIES, cornerRadiusEnd=4, height=22)
    labels = base.mark_text(align="left", dx=6).encode(
        text=alt.Text("share_of_bank_lines:Q", format=".1%")
    )
    return (bars + labels).properties(height=150)


def main() -> None:
    st.set_page_config(page_title="ReconPipe review", layout="wide")
    st.title("Bank reconciliation review")

    if not DB_PATH.exists():
        st.error(f"No warehouse at {DB_PATH}. Run `python -m reconpipe` first.")
        st.stop()

    run = latest_run(DB_PATH)
    if run:
        st.caption(f"Run {run['run_id']} · {run['started_at']:%d %b %Y %H:%M} UTC")

    # 1. Summary header
    n = headline_numbers(DB_PATH)
    cols = st.columns(5)
    cols[0].metric("Bank lines", f"{n['bank_lines']:,}")
    cols[1].metric("Auto-matched", f"{n['match_rate']:.1%}", help=f"{n['matched']} lines")
    cols[2].metric("Need review", f"{n['exceptions']:,}")
    cols[3].metric("Value in exception", f"RWF {n['value_in_exception']:,.0f}")
    cols[4].metric("Quarantined rows", f"{n['quarantined']:,}")

    # 2. Exceptions: filters in one row above the table, then the table, then download
    st.subheader("Exceptions to review")
    table = exceptions(DB_PATH)
    f1, f2, f3 = st.columns([2, 2, 3])
    statuses = f1.multiselect("Status", sorted(table["status"].unique()))
    accounts = f2.multiselect("Account", sorted(table["account_id"].unique()))
    search = f3.text_input("Search descriptions and references")
    if statuses:
        table = table[table["status"].isin(statuses)]
    if accounts:
        table = table[table["account_id"].isin(accounts)]
    if search:
        text = table[["bank_description", "ledger_description", "bank_ref", "ledger_entries"]]
        hit = text.fillna("").apply(lambda c: c.str.contains(search, case=False, regex=False))
        table = table[hit.any(axis=1)]
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "amount": st.column_config.NumberColumn("amount (RWF)", format="%.2f"),
            "difference": st.column_config.NumberColumn("ledger − bank", format="%.2f"),
            "bank_date": st.column_config.DateColumn("bank date", format="YYYY-MM-DD"),
            "ledger_date": st.column_config.DateColumn("ledger date", format="YYYY-MM-DD"),
        },
    )
    st.download_button(
        f"Download {len(table)} rows as CSV",
        table.to_csv(index=False).encode("utf-8"),
        file_name="exceptions.csv",
        mime="text/csv",
    )

    left, right = st.columns(2)

    # 3. Match rate by tier
    with left:
        st.subheader("Auto-match rate by tier")
        tiers = match_rate_by_tier(DB_PATH)
        st.altair_chart(tier_chart(tiers), use_container_width=True)
        with st.expander("Table view"):
            st.dataframe(tiers, hide_index=True, use_container_width=True)

    # 4. Data quality
    with right:
        st.subheader("Data quality: quarantined rows")
        st.caption("Statement lines that broke a validation rule never reach matching.")
        st.dataframe(quarantine_by_rule(DB_PATH), hide_index=True, use_container_width=True)
        st.dataframe(quarantined_rows(DB_PATH), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
