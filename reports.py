"""
reports.py
==========
Phase 3: LOADING & FINANCIAL REPORTING.

The core philosophy of this whole project lives here: there is exactly ONE
data source (the validated Universal General Ledger DataFrame) and every
financial statement below is nothing more than a pandas groupby/pivot/
filter layered on top of it. No report keeps its own copy of the data --
change a transaction in the ledger and every statement below reflects it
automatically, exactly like a real accounting engine's reporting layer.

Accounting refresher used throughout this file:
  - Assets & Expenses have a NORMAL DEBIT balance (they grow with debits).
  - Liabilities, Equity & Revenue have a NORMAL CREDIT balance.
  - Net Income = Total Revenue - Total Expenses (Income Statement).
  - Assets = Liabilities + Equity, where Equity includes current-period
    Net Income folded in as Retained Earnings (Balance Sheet).
  - Cash Flow Statement explains WHY the Cash account's balance changed,
    bucketed into Operating / Investing / Financing activity.
"""

import pandas as pd

import config
from config import CASH_CODE, NORMAL_BALANCE, RETAINED_EARNINGS_CODE

# CF_CATEGORY_MAP is intentionally NOT imported by name (`from config import
# CF_CATEGORY_MAP`): config.add_account() rebinds that dict on the config
# module at runtime, and a `from ... import` here would keep pointing at
# the stale dict that existed at import time. Accessing it as
# config.CF_CATEGORY_MAP inside cash_flow_statement() always sees the
# current version. CASH_CODE / NORMAL_BALANCE / RETAINED_EARNINGS_CODE are
# fixed constants (never reassigned), so importing them directly is safe.


def _signed_amount(account_type: str, debit: float, credit: float) -> float:
    """Convert raw Debit/Credit totals into one signed 'natural balance'
    number, using config.NORMAL_BALANCE instead of hard-coding which
    account types are which. This is what makes trial_balance/
    income_statement/balance_sheet generic to ANY account a user adds via
    config.add_account() -- they never need to special-case a new type."""
    return (debit - credit) if NORMAL_BALANCE[account_type] == "Debit" else (credit - debit)


def _parse_dates(gl: pd.DataFrame) -> pd.Series:
    """The GL's Date column is intentionally stored as a 'DD-MM-YYYY'
    STRING (per the Universal Ledger schema), which is great for display
    but not directly sortable/filterable. dayfirst-aware parsing recovers
    a real datetime64 column for range filtering without mutating the
    ledger's canonical string representation."""
    return pd.to_datetime(gl["Date"], format="%d-%m-%Y")


def filter_date_range(gl: pd.DataFrame, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """Filter the ledger to rows within [start_date, end_date] (inclusive).
    Dates are accepted in 'DD-MM-YYYY' to match the ledger's own format."""
    if start_date is None and end_date is None:
        return gl

    dates = _parse_dates(gl)
    mask = pd.Series(True, index=gl.index)
    if start_date is not None:
        mask &= dates >= pd.to_datetime(start_date, format="%d-%m-%Y")
    if end_date is not None:
        mask &= dates <= pd.to_datetime(end_date, format="%d-%m-%Y")
    return gl.loc[mask]


# ---------------------------------------------------------------------------
# 1. TRIAL BALANCE
# ---------------------------------------------------------------------------


def trial_balance(gl: pd.DataFrame) -> pd.DataFrame:
    """A Trial Balance is simply every account's total Debits and total
    Credits, side by side, proving the ledger is internally consistent.

    groupby(['Account_Code', 'Account_Name', 'Account_Type']) collapses the
    (potentially thousands of) individual ledger lines down to one row per
    account; .sum() on the numeric Debit/Credit columns gives each
    account's lifetime activity. We append a TOTAL row at the bottom --
    by the nature of double-entry bookkeeping (every transaction balances
    itself), grand total Debits must equal grand total Credits exactly.
    """
    grouped = (
        gl.groupby(["Account_Code", "Account_Name", "Account_Type"], as_index=False)[["Debit", "Credit"]]
        .sum()
        .sort_values("Account_Code")
    )
    grouped["Balance"] = grouped["Debit"] - grouped["Credit"]

    totals = pd.DataFrame(
        [{
            "Account_Code": "",
            "Account_Name": "TOTAL",
            "Account_Type": "",
            "Debit": grouped["Debit"].sum(),
            "Credit": grouped["Credit"].sum(),
            "Balance": grouped["Balance"].sum(),
        }]
    )
    result = pd.concat([grouped, totals], ignore_index=True)

    total_debit, total_credit = grouped["Debit"].sum(), grouped["Credit"].sum()
    assert abs(total_debit - total_credit) < 0.01, (
        f"Trial Balance does not balance! Debits={total_debit}, Credits={total_credit}"
    )
    return result


# ---------------------------------------------------------------------------
# 2. INCOME STATEMENT (Profit & Loss)
# ---------------------------------------------------------------------------


def income_statement(gl: pd.DataFrame, start_date: str | None = None, end_date: str | None = None) -> tuple[pd.DataFrame, float]:
    """Revenue and Expense accounts only, over an optional date window.
    Revenue's normal balance is Credit, so its net contribution is
    (Credit - Debit); Expense's normal balance is Debit, so its net
    contribution is (Debit - Credit). Net Income is the difference between
    the two account-type subtotals.

    Returns (statement_df, net_income) -- net_income is returned separately
    (not just embedded in the DataFrame) because balance_sheet() below
    needs the raw number to inject as Retained Earnings.
    """
    window = filter_date_range(gl, start_date, end_date)
    pnl_gl = window[window["Account_Type"].isin(["Revenue", "Expense"])]

    by_account = (
        pnl_gl.groupby(["Account_Type", "Account_Name"], as_index=False)[["Debit", "Credit"]]
        .sum()
    )
    by_account["Amount"] = by_account.apply(
        lambda r: _signed_amount(r["Account_Type"], r["Debit"], r["Credit"]), axis=1
    )
    by_account = by_account.sort_values(["Account_Type", "Account_Name"])[["Account_Type", "Account_Name", "Amount"]]

    total_revenue = by_account.loc[by_account["Account_Type"] == "Revenue", "Amount"].sum()
    total_expense = by_account.loc[by_account["Account_Type"] == "Expense", "Amount"].sum()
    net_income = total_revenue - total_expense

    summary_rows = pd.DataFrame(
        [
            {"Account_Type": "Revenue", "Account_Name": "TOTAL REVENUE", "Amount": total_revenue},
            {"Account_Type": "Expense", "Account_Name": "TOTAL EXPENSES", "Amount": total_expense},
            {"Account_Type": "", "Account_Name": "NET INCOME", "Amount": net_income},
        ]
    )
    statement = pd.concat([by_account, summary_rows], ignore_index=True)
    return statement, net_income


# ---------------------------------------------------------------------------
# 3. BALANCE SHEET
# ---------------------------------------------------------------------------


def balance_sheet(gl: pd.DataFrame, net_income: float, as_of_date: str | None = None) -> pd.DataFrame:
    """Asset, Liability and Equity accounts as of a point in time (a
    Balance Sheet is a SNAPSHOT, unlike the Income Statement's date RANGE
    -- so we only ever filter with end_date here).

    The current period's Net Income (computed by income_statement()) is
    not sitting in any ledger account by itself -- it is dynamically
    injected as a synthetic "Retained Earnings" line in Equity, which is
    exactly what closes the accounting equation:

        Assets = Liabilities + Equity   (Equity includes Net Income)
    """
    snapshot = filter_date_range(gl, end_date=as_of_date)
    bs_gl = snapshot[snapshot["Account_Type"].isin(["Asset", "Liability", "Equity"])]

    by_account = (
        bs_gl.groupby(["Account_Type", "Account_Code", "Account_Name"], as_index=False)[["Debit", "Credit"]]
        .sum()
    )
    by_account["Amount"] = by_account.apply(
        lambda r: _signed_amount(r["Account_Type"], r["Debit"], r["Credit"]), axis=1
    )
    by_account = by_account[["Account_Type", "Account_Code", "Account_Name", "Amount"]]

    # Dynamically inject Net Income as Retained Earnings under Equity. If
    # the ledger already booked activity to the Retained Earnings account
    # (Account_Code RETAINED_EARNINGS_CODE) directly, add to it instead of
    # creating a duplicate row.
    retained_mask = by_account["Account_Code"] == RETAINED_EARNINGS_CODE
    if retained_mask.any():
        by_account.loc[retained_mask, "Amount"] += net_income
    else:
        by_account = pd.concat(
            [
                by_account,
                pd.DataFrame(
                    [{
                        "Account_Type": "Equity",
                        "Account_Code": RETAINED_EARNINGS_CODE,
                        "Account_Name": "Retained Earnings (Current Period Net Income)",
                        "Amount": net_income,
                    }]
                ),
            ],
            ignore_index=True,
        )

    by_account = by_account.sort_values(["Account_Type", "Account_Code"])

    total_assets = by_account.loc[by_account["Account_Type"] == "Asset", "Amount"].sum()
    total_liabilities = by_account.loc[by_account["Account_Type"] == "Liability", "Amount"].sum()
    total_equity = by_account.loc[by_account["Account_Type"] == "Equity", "Amount"].sum()

    summary_rows = pd.DataFrame(
        [
            {"Account_Type": "Asset", "Account_Code": "", "Account_Name": "TOTAL ASSETS", "Amount": total_assets},
            {"Account_Type": "Liability", "Account_Code": "", "Account_Name": "TOTAL LIABILITIES", "Amount": total_liabilities},
            {"Account_Type": "Equity", "Account_Code": "", "Account_Name": "TOTAL EQUITY", "Amount": total_equity},
            {"Account_Type": "", "Account_Code": "", "Account_Name": "TOTAL LIABILITIES + EQUITY", "Amount": total_liabilities + total_equity},
        ]
    )
    statement = pd.concat([by_account, summary_rows], ignore_index=True)

    check = abs(total_assets - (total_liabilities + total_equity))
    if check > 0.01:
        print(f"[WARNING] Balance Sheet does not balance! Assets={total_assets}, L+E={total_liabilities + total_equity}")
    else:
        print(f"[OK] Balance Sheet balances: Assets ({total_assets:,.2f}) = Liabilities + Equity ({total_liabilities + total_equity:,.2f})")

    return statement


# ---------------------------------------------------------------------------
# 4. CASH FLOW STATEMENT (direct method, derived from the GL)
# ---------------------------------------------------------------------------


def cash_flow_statement(gl: pd.DataFrame, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """Explains the change in Cash by classifying every transaction that
    touched the Cash account into Operating / Investing / Financing,
    based on what account was on the OTHER side of the entry.

    Why this approach: the Cash account's own Account_Type ('Asset') tells
    us nothing about WHY money moved. But because every ledger line is
    part of a balanced Transaction_ID, we can look up the other leg(s) of
    the same transaction and classify by THAT account instead -- via
    config.CF_CATEGORY_MAP. This is the "direct method" cash flow
    statement, built the same way a GL-driven accounting engine derives it.
    """
    window = filter_date_range(gl, start_date, end_date)

    cash_lines = window[window["Account_Code"] == CASH_CODE].copy()
    cash_lines["Net_Cash_Impact"] = cash_lines["Debit"] - cash_lines["Credit"]

    # For each transaction touching Cash, find its non-cash counterpart
    # line(s) so we know which "kind" of activity moved the cash.
    other_legs = window[window["Account_Code"] != CASH_CODE][["Transaction_ID", "Account_Code"]]
    other_legs = other_legs.drop_duplicates(subset=["Transaction_ID"])  # one classifying leg per txn is enough here

    merged = cash_lines.merge(other_legs, on="Transaction_ID", how="left", suffixes=("", "_other"))
    merged["Category"] = merged["Account_Code_other"].map(config.CF_CATEGORY_MAP).fillna("Uncategorized")

    by_category = (
        merged.groupby("Category", as_index=False)["Net_Cash_Impact"]
        .sum()
        .rename(columns={"Net_Cash_Impact": "Amount"})
    )
    # Guarantee all three standard categories always appear, even at $0,
    # so the report always has a consistent shape for the Excel output.
    for cat in ["Operating", "Investing", "Financing"]:
        if cat not in by_category["Category"].values:
            by_category = pd.concat([by_category, pd.DataFrame([{"Category": cat, "Amount": 0.0}])], ignore_index=True)
    order = {"Operating": 0, "Investing": 1, "Financing": 2, "Uncategorized": 3}
    by_category = by_category.sort_values("Category", key=lambda s: s.map(order)).reset_index(drop=True)

    net_change = by_category["Amount"].sum()
    beginning_cash = 0.0  # demo assumes the business started at $0; a real system would carry this from a prior period close
    ending_cash = beginning_cash + net_change

    summary_rows = pd.DataFrame(
        [
            {"Category": "NET CHANGE IN CASH", "Amount": net_change},
            {"Category": "BEGINNING CASH BALANCE", "Amount": beginning_cash},
            {"Category": "ENDING CASH BALANCE", "Amount": ending_cash},
        ]
    )
    statement = pd.concat(
        [by_category.rename(columns={"Category": "Category"}), summary_rows], ignore_index=True
    )
    return statement
