"""
adapters.py
===========
Phase 1: EXTRACTION & ADAPTER LAYER.

Real-world finance teams hand you Excel files in whatever shape their own
tool spat out. Some are already "ledger-shaped" (one row per debit/credit
leg). Some are "single-sided" (one row per real-world event, e.g. "paid
$500 rent", with no explicit offsetting account). Some are "wide" /
column-based (an account per row, a MONTH per column -- a classic export
from a budgeting tool or a pivoted spreadsheet).

This module's job is ONLY extraction + shape-normalization. It does not
decide accounting policy beyond "which account(s) does this row touch and
how much money moved" -- the actual double-entry bookkeeping (turning that
into balanced Debit/Credit ledger rows) happens in etl_pipeline.py. That
separation mirrors a real ETL: adapters translate SOURCE shape -> a common
staging shape; the engine turns staging rows into GL rows.

Every ingestion function returns one of two staging shapes:

  A) "GL-ready" rows (already double-entry, e.g. a legacy ledger export):
     Transaction_ID, Date, Account_Label, Debit, Credit, Description, Source_File_Ref

  B) "Single-sided" staging rows (need the engine to invent the offset leg):
     Date, Description, Debit_Account_Label, Credit_Account_Label, Amount, Source_File_Ref
     (Transaction_ID is added later by the engine -- one staging row = one
     real-world transaction = exactly one Debit leg + one Credit leg.)
"""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase / strip / underscore-ify column headers so that messy
    variants like "Txn Date", "TXN_DATE", " txn date " all collapse to the
    same lookup key ("txn_date"). This is what lets rename_columns() below
    match against a fixed dict instead of needing exact header spelling."""
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"[\s/]+", "_", regex=True)
        .str.replace(r"[^a-z0-9_]", "", regex=True)
    )
    return df


def rename_columns(df: pd.DataFrame, header_map: dict) -> pd.DataFrame:
    """Rename whatever messy headers a raw file happens to use into the
    standardized intermediate names each ingest_* function expects.
    header_map keys must already be in the cleaned (lower_snake_case) form
    produced by _clean_headers()."""
    df = _clean_headers(df)
    missing = [c for c in header_map if c not in df.columns]
    if missing:
        raise KeyError(
            f"Expected column(s) {missing} not found. Found columns: {list(df.columns)}"
        )
    return df.rename(columns=header_map)


def standardize_dates(series: pd.Series) -> pd.Series:
    """Force any date-like input (Excel serial dates, 'YYYY-MM-DD' strings,
    'MM/DD/YYYY' strings, real datetime64 objects...) into the project's one
    true date format: 'DD-MM-YYYY' strings.

    pandas.to_excel()/read_excel() usually hands back native Timestamp
    objects for genuine Excel date cells, but messy source files sometimes
    store dates as free-text strings -- pd.to_datetime with errors='coerce'
    absorbs both cases into one datetime64 column, and .dt.strftime() then
    renders every row identically regardless of how it arrived."""
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.isna().any():
        bad_rows = series[parsed.isna()].tolist()
        raise ValueError(f"Unparseable date value(s) encountered: {bad_rows}")
    return parsed.dt.strftime("%d-%m-%Y")


def is_wide_format(df: pd.DataFrame, min_date_like_columns: int = 2) -> bool:
    """Heuristic structure detector: if a file has several columns whose
    HEADER is itself a date/month (e.g. 'Jan-2026', 'Feb-2026', ...), it is
    a column-based / wide file where each column is really a time bucket
    that pd.melt() needs to collapse into rows. A normal row-based ledger
    export has descriptive headers ('Date', 'Amount', ...) that don't parse
    as dates, so this count stays at 0."""
    date_like = pd.to_datetime(pd.Series(df.columns.astype(str)), errors="coerce")
    return date_like.notna().sum() >= min_date_like_columns


# ---------------------------------------------------------------------------
# Adapter 1: Row-based, ALREADY double-entry ("pass-through")
# ---------------------------------------------------------------------------
# Spec: "Row-Based Files: use pd.read_excel(), rename columns to match the
# target schema, and pass them through." This is the literal case: a legacy
# system already exported ledger-shaped rows (Reference/Account/Debit/Credit).
# No business-rule offset generation is needed -- just rename + standardize.
GL_PASSTHROUGH_HEADER_MAP = {
    "reference": "Transaction_ID",
    "txn_date": "Date",
    "acct": "Account_Label",
    "acctdesc": "Account_Label_Desc",  # kept only for reference; not used for lookups
    "dr": "Debit",
    "cr": "Credit",
    "memo": "Description",
}


def ingest_gl_passthrough(filepath: str | Path, source_ref: str) -> pd.DataFrame:
    """Row-based ingestion for a file that is ALREADY shaped like a general
    ledger (one row per Debit or Credit leg, grouped by a Reference/
    Transaction_ID). We only rename + standardize dates; no double-entry
    logic is invented because the source already supplies both legs."""
    raw = pd.read_excel(filepath)
    df = rename_columns(raw, GL_PASSTHROUGH_HEADER_MAP)

    df["Date"] = standardize_dates(df["Date"])
    df["Debit"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0.0)
    df["Credit"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0.0)
    df["Source_File_Ref"] = source_ref

    return df[
        [
            "Transaction_ID",
            "Date",
            "Account_Label",
            "Debit",
            "Credit",
            "Description",
            "Source_File_Ref",
        ]
    ]


# ---------------------------------------------------------------------------
# Adapter 2: Row-based, SINGLE-SIDED expense entries
# ---------------------------------------------------------------------------
# A typical "expenses_july.xlsx" export: one row per payment, with no
# explicit offsetting account -- a human paid $X for category Y via Z.
# The engine (etl_pipeline.py) will invent the second leg from
# Debit_Account_Label / Credit_Account_Label.
EXPENSE_HEADER_MAP = {
    "txn_date": "Date",
    "vendordesc": "Description",
    "category": "Debit_Account_Label",  # the expense (or capex) account being increased
    "amount_paid": "Amount",
    "paid_via": "Credit_Account_Label",  # Cash or Accounts Payable
}


def ingest_expense_file(filepath: str | Path, source_ref: str) -> pd.DataFrame:
    """Single-sided row-based ingestion for expense/payment logs. Each row
    debits the spending category (an Expense or Asset/capex account) and
    credits however the payment left the business (Cash or Accounts
    Payable), per the 'Paid Via' column."""
    raw = pd.read_excel(filepath)
    df = rename_columns(raw, EXPENSE_HEADER_MAP)

    df["Date"] = standardize_dates(df["Date"])
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    df["Source_File_Ref"] = source_ref

    return df[
        [
            "Date",
            "Description",
            "Debit_Account_Label",
            "Credit_Account_Label",
            "Amount",
            "Source_File_Ref",
        ]
    ]


# ---------------------------------------------------------------------------
# Adapter 3: Row-based, SINGLE-SIDED revenue entries
# ---------------------------------------------------------------------------
REVENUE_HEADER_MAP = {
    "invoice_date": "Date",
    "customer": "Description",
    "revenue_type": "Credit_Account_Label",  # Sales or Service Revenue
    "invoice_amount": "Amount",
    "status": "_status",  # Paid -> debit Cash; Unpaid -> debit Accounts Receivable
}


def ingest_revenue_file(filepath: str | Path, source_ref: str) -> pd.DataFrame:
    """Single-sided row-based ingestion for sales invoices. Revenue is
    always credited; the debit side depends on whether the invoice has
    already been collected (Cash) or is still outstanding (Accounts
    Receivable) -- a direct application of accrual-basis accounting."""
    raw = pd.read_excel(filepath)
    df = rename_columns(raw, REVENUE_HEADER_MAP)

    df["Date"] = standardize_dates(df["Date"])
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    df["Source_File_Ref"] = source_ref

    df["Debit_Account_Label"] = df["_status"].str.strip().str.lower().map(
        {"paid": "cash", "unpaid": "accounts receivable"}
    )
    if df["Debit_Account_Label"].isna().any():
        bad = df.loc[df["Debit_Account_Label"].isna(), "_status"].tolist()
        raise ValueError(f"Unrecognized invoice status value(s): {bad}")

    return df[
        [
            "Date",
            "Description",
            "Debit_Account_Label",
            "Credit_Account_Label",
            "Amount",
            "Source_File_Ref",
        ]
    ]


# ---------------------------------------------------------------------------
# Adapter 4: Row-based, financing activities (loans / owner capital)
# ---------------------------------------------------------------------------
FINANCING_DEBIT_CREDIT_BY_TYPE = {
    # type -> (Debit_Account_Label, Credit_Account_Label)
    "loan received": ("cash", "loans payable"),
    "loan repayment": ("loans payable", "cash"),
    "owner investment": ("cash", "common stock"),
}

FINANCING_HEADER_MAP = {
    "date": "Date",
    "description": "Description",
    "type": "_type",
    "amount": "Amount",
}


def ingest_financing_file(filepath: str | Path, source_ref: str) -> pd.DataFrame:
    """Single-sided row-based ingestion for financing events (loans drawn
    or repaid, owner capital contributions). Unlike expenses/revenue, BOTH
    legs vary by transaction type, so we look each type up in a small
    policy table rather than hard-coding one fixed offset account."""
    raw = pd.read_excel(filepath)
    df = rename_columns(raw, FINANCING_HEADER_MAP)

    df["Date"] = standardize_dates(df["Date"])
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    df["Source_File_Ref"] = source_ref

    type_key = df["_type"].str.strip().str.lower()
    unknown = ~type_key.isin(FINANCING_DEBIT_CREDIT_BY_TYPE)
    if unknown.any():
        raise ValueError(f"Unrecognized financing type(s): {df.loc[unknown, '_type'].tolist()}")

    legs = type_key.map(FINANCING_DEBIT_CREDIT_BY_TYPE)
    df["Debit_Account_Label"] = legs.map(lambda t: t[0])
    df["Credit_Account_Label"] = legs.map(lambda t: t[1])

    return df[
        [
            "Date",
            "Description",
            "Debit_Account_Label",
            "Credit_Account_Label",
            "Amount",
            "Source_File_Ref",
        ]
    ]


# ---------------------------------------------------------------------------
# Adapter 5: Column-based / WIDE file -> pd.melt()
# ---------------------------------------------------------------------------


def ingest_wide_expense_budget(filepath: str | Path, source_ref: str) -> pd.DataFrame:
    """Column-based ingestion for a 'wide' export where each row is an
    expense category and each MONTH is its own column (e.g. 'Jan-2026',
    'Feb-2026', 'Mar-2026' holding that month's actual cash spend).

    pd.melt() is the mirror image of a pivot: it takes N 'value' columns
    and stacks them into two long-form columns (a 'variable' column holding
    the original header, and a 'value' column holding the cell). Here that
    turns "one row x 3 month-columns" into "3 rows x (Month, Amount)" --
    exactly the Date/Value long form the rest of the pipeline expects.
    """
    raw = pd.read_excel(filepath)

    # NOTE: we deliberately do NOT run the full _clean_headers() cleaner
    # here. That helper strips punctuation (including hyphens) to build
    # lookup keys, which would turn a month header like 'Apr-2026' into
    # 'apr2026' -- destroying the exact '%b-%Y' text the parse step below
    # needs. Instead we case-insensitively identify just the two id
    # columns and leave every month/date header column byte-for-byte as
    # the source file wrote it.
    lower_to_original = {str(c).strip().lower(): c for c in raw.columns}
    try:
        category_col = lower_to_original["category"]
        paid_via_col = lower_to_original["paid via"]
    except KeyError as exc:
        raise KeyError(
            f"Wide budget file '{filepath}' must have 'Category' and 'Paid Via' columns; "
            f"found {list(raw.columns)}"
        ) from exc

    df = raw.rename(columns={category_col: "Debit_Account_Label", paid_via_col: "Credit_Account_Label"})
    month_columns = [c for c in df.columns if c not in ("Debit_Account_Label", "Credit_Account_Label")]

    long_df = df.melt(
        id_vars=["Debit_Account_Label", "Credit_Account_Label"],
        value_vars=month_columns,
        var_name="Month",          # the old column header (e.g. 'jan2026') becomes data
        value_name="Amount",       # the old cell value becomes data too
    )

    # The melted 'Month' header (e.g. 'jan-2026') is itself a date bucket --
    # parse it explicitly with a month/year format, then standardize like
    # any other date so downstream code never has to know this file was
    # ever wide in the first place.
    long_df["Date_parsed"] = pd.to_datetime(long_df["Month"], format="%b-%Y", errors="coerce")
    if long_df["Date_parsed"].isna().any():
        bad = long_df.loc[long_df["Date_parsed"].isna(), "Month"].tolist()
        raise ValueError(f"Unparseable month header(s) after melt: {bad}")
    long_df["Date"] = long_df["Date_parsed"].dt.strftime("%d-%m-%Y")

    long_df["Amount"] = pd.to_numeric(long_df["Amount"], errors="coerce")
    long_df = long_df.dropna(subset=["Amount"])  # blank month cells => no spend that month

    long_df["Description"] = (
        long_df["Debit_Account_Label"].str.title() + " - " + long_df["Month"]
    )
    long_df["Source_File_Ref"] = source_ref

    return long_df[
        [
            "Date",
            "Description",
            "Debit_Account_Label",
            "Credit_Account_Label",
            "Amount",
            "Source_File_Ref",
        ]
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
# Maps a raw filename to the adapter that knows how to read it. In a larger
# system this would inspect file content (via is_wide_format / column
# signatures) rather than filename; both techniques are shown here so the
# choice is explicit rather than hidden.
FILE_ADAPTER_REGISTRY = {
    "bank_ledger_export.xlsx": ("gl_passthrough", ingest_gl_passthrough),
    "expenses_july.xlsx": ("single_sided", ingest_expense_file),
    "sales_invoices_july.xlsx": ("single_sided", ingest_revenue_file),
    "financing_activities.xlsx": ("single_sided", ingest_financing_file),
    "monthly_expense_budget_wide.xlsx": ("wide_melt", ingest_wide_expense_budget),
}


def detect_and_ingest(filepath: str | Path) -> tuple[str, pd.DataFrame]:
    """Given a raw file path, decide which adapter applies and run it.
    Returns (staging_kind, staging_df) where staging_kind is either
    'gl_passthrough' (already double-entry) or 'single_sided' / 'wide_melt'
    (needs the double-entry engine to add the offsetting leg)."""
    filepath = Path(filepath)
    source_ref = filepath.name

    if filepath.name in FILE_ADAPTER_REGISTRY:
        kind, fn = FILE_ADAPTER_REGISTRY[filepath.name]
        return kind, fn(filepath, source_ref)

    # Fallback for genuinely unknown files: sniff the shape instead of the name.
    probe = pd.read_excel(filepath, nrows=5)
    if is_wide_format(probe):
        return "wide_melt", ingest_wide_expense_budget(filepath, source_ref)
    raise ValueError(
        f"No adapter registered for '{filepath.name}' and it does not look wide-format. "
        "Add an entry to FILE_ADAPTER_REGISTRY."
    )
