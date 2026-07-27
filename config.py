"""
config.py
=========
The single source of truth for accounting metadata: the Chart of Accounts
(CoA) and the Cash Flow category map.

IMPORTANT -- Chart of Accounts is DATA, not code.
---------------------------------------------------
The CoA lives in `data/chart_of_accounts.csv`, NOT hard-coded in this file.
That is deliberate: adding a new account (say, "Software Subscriptions" as
a new Expense account, or a second bank account) is something a bookkeeper
should be able to do by editing a spreadsheet -- it should never require a
Python code change or a redeploy. This module just loads that CSV into the
in-memory structures the rest of the pipeline uses.

CSV columns:
    Account_Code   - unique CoA code, e.g. "5500"
    Account_Name   - human-readable name, e.g. "Software Subscriptions"
    Account_Type   - one of Asset / Liability / Equity / Revenue / Expense
    Aliases        - pipe-separated ("|") list of messy free-text labels
                      that should resolve to this account, e.g.
                      "software|saas|subscriptions|software expense"

To add a new account WITHOUT touching this file, either:
    1. Open data/chart_of_accounts.csv in Excel and add a row, or
    2. Call config.add_account(code, name, account_type, aliases=[...])
       from a Python script -- it appends to the CSV and refreshes the
       in-memory lookups used by adapters.py / etl_pipeline.py.
"""

from pathlib import Path

import pandas as pd

COA_CSV_PATH = Path(__file__).parent / "data" / "chart_of_accounts.csv"

# ---------------------------------------------------------------------------
# Well-known codes referenced by name in business logic (offsets, CF mapping).
# These are structural (every install of this system needs SOME cash
# account, SOME AR account, etc.) even though the CoA itself is editable.
# ---------------------------------------------------------------------------
CASH_CODE = "1000"
AR_CODE = "1100"
AP_CODE = "2000"
RETAINED_EARNINGS_CODE = "3900"

# Every account TYPE's "normal balance" side. Assets & Expenses grow with
# Debits; Liabilities, Equity & Revenue grow with Credits. This is fixed by
# accounting convention (not user-editable), so it stays a plain dict.
NORMAL_BALANCE = {
    "Asset": "Debit",
    "Expense": "Debit",
    "Liability": "Credit",
    "Equity": "Credit",
    "Revenue": "Credit",
}

# Maps a NEW account's code to a Cash Flow Statement bucket. Kept editable
# alongside the CoA CSV would be ideal in a bigger system; for this
# project's scope we key off Account_Type as a sensible default and let
# specific codes override it (see CF_CATEGORY_OVERRIDES).
CF_TYPE_DEFAULT = {
    "Revenue": "Operating",
    "Expense": "Operating",
    "Asset": "Investing",     # non-cash assets (equipment, etc.) default to Investing
    "Liability": "Financing",
    "Equity": "Financing",
}
# Exceptions to the type-based default above (AR/AP are Assets/Liabilities
# but represent OPERATING activity, not investing/financing).
CF_CATEGORY_OVERRIDES = {
    AR_CODE: "Operating",
    AP_CODE: "Operating",
}


def _load_chart_of_accounts(path: Path = COA_CSV_PATH) -> pd.DataFrame:
    """Read the editable CoA CSV into a DataFrame. Aliases column is split
    on '|' into a Python list so lookups can iterate over it directly."""
    coa = pd.read_csv(path, dtype={"Account_Code": str})
    coa["Aliases"] = coa["Aliases"].fillna("").apply(
        lambda s: [a.strip().lower() for a in s.split("|") if a.strip()]
    )
    return coa


def _build_alias_map(coa: pd.DataFrame) -> dict:
    """Flatten the CoA's Aliases lists into a single {alias: code} dict for
    O(1) exact-match lookups in etl_pipeline._resolve_label(). Also
    registers the account's own name (lowercased) as an implicit alias, so
    a source file that already uses the "clean" account name still
    resolves without needing an explicit entry in the Aliases column."""
    alias_map = {}
    for _, row in coa.iterrows():
        alias_map[row["Account_Name"].strip().lower()] = row["Account_Code"]
        for alias in row["Aliases"]:
            alias_map[alias] = row["Account_Code"]
    return alias_map


def _build_cf_category_map(coa: pd.DataFrame) -> dict:
    """Derive the Cash Flow category for every non-cash account: explicit
    per-code overrides win, otherwise fall back to the account TYPE's
    default bucket. Rebuilt whenever the CoA changes so a newly added
    account is automatically classified without editing this file."""
    cf_map = {}
    for _, row in coa.iterrows():
        code = row["Account_Code"]
        if code == CASH_CODE:
            continue  # Cash is the subject of the cash flow statement, not a category
        cf_map[code] = CF_CATEGORY_OVERRIDES.get(code, CF_TYPE_DEFAULT.get(row["Account_Type"], "Operating"))
    return cf_map


def reload() -> None:
    """Re-read the CoA CSV and refresh every derived structure. Call this
    after editing the CSV on disk mid-session (e.g. from a notebook) or
    after add_account() below."""
    global CHART_OF_ACCOUNTS, COA_BY_CODE, ACCOUNT_ALIASES, CF_CATEGORY_MAP
    CHART_OF_ACCOUNTS = _load_chart_of_accounts()
    COA_BY_CODE = CHART_OF_ACCOUNTS.set_index("Account_Code")
    ACCOUNT_ALIASES = _build_alias_map(CHART_OF_ACCOUNTS)
    CF_CATEGORY_MAP = _build_cf_category_map(CHART_OF_ACCOUNTS)


def add_account(code: str, name: str, account_type: str, aliases: list[str] | None = None) -> None:
    """Programmatically add a new account to the Chart of Accounts --
    appends a row to data/chart_of_accounts.csv and refreshes every
    in-memory lookup so it's usable immediately, e.g.:

        import config
        config.add_account("5500", "Software Subscriptions", "Expense",
                            aliases=["software", "saas", "subscriptions"])

    This is the "add an account without touching pipeline code" escape
    hatch requested by the user -- equally, an accountant can just open
    the CSV in Excel and add the row by hand; both paths are supported.
    """
    if account_type not in NORMAL_BALANCE:
        raise ValueError(f"account_type must be one of {list(NORMAL_BALANCE)}, got {account_type!r}")
    if code in CHART_OF_ACCOUNTS["Account_Code"].values:
        raise ValueError(f"Account_Code {code!r} already exists in the Chart of Accounts")

    new_row = pd.DataFrame(
        [{
            "Account_Code": code,
            "Account_Name": name,
            "Account_Type": account_type,
            "Aliases": "|".join(aliases or []),
        }]
    )
    existing = pd.read_csv(COA_CSV_PATH, dtype={"Account_Code": str})
    updated = pd.concat([existing, new_row], ignore_index=True)
    updated.to_csv(COA_CSV_PATH, index=False)
    reload()
    print(f"[CoA] Added account {code} - {name} ({account_type}). Chart of Accounts now has {len(CHART_OF_ACCOUNTS)} accounts.")


# Load once at import time so `from config import CHART_OF_ACCOUNTS, ...`
# works exactly as before for every other module in the project.
reload()
