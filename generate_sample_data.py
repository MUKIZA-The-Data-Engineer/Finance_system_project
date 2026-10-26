"""
generate_sample_data.py
========================
Creates a handful of deliberately MESSY, deliberately DIFFERENT-SHAPED
sample Excel workbooks in data/gl_raw/, so run_pipeline.py has something
realistic to ingest. This script is NOT part of the pipeline itself --
it's a one-time (or re-runnable) fixture generator, standing in for "the
five different departments/systems that hand you Excel files" a real
bookkeeper deals with.

The five files exercise every extraction pattern adapters.py supports:

  1. bank_ledger_export.xlsx        - already double-entry (pass-through).
                                       Includes ONE deliberately unbalanced
                                       transaction to prove the Phase 2
                                       validation step actually catches
                                       something real.
  2. expenses_july.xlsx             - single-sided row-based (needs the
                                       double-entry engine to invent the
                                       Cash/Accounts Payable offset leg).
  3. sales_invoices_july.xlsx       - single-sided row-based revenue.
  4. financing_activities.xlsx     - single-sided row-based financing
                                       (loans, owner capital).
  5. monthly_expense_budget_wide.xlsx - column-based/wide (months as
                                       columns) -- needs pd.melt().
"""

from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).parent / "data" / "gl_raw"


def make_bank_ledger_export() -> pd.DataFrame:
    """Already GL-shaped (one row per Debit or Credit leg, grouped by
    Reference). Transaction TXN-BANK-003 is INTENTIONALLY corrupted --
    the electricity bill was recorded as a $180 debit but only a $165
    credit (e.g. a bank fee the bookkeeper forgot to book separately) --
    so the Phase 2 validation step has a real unbalanced entry to catch."""
    rows = [
        # Reference,      Txn Date,     Acct,             AcctDesc,               DR,     CR,   Memo
        ("TXN-BANK-001", "2026-01-05", "Cash",            "Owner deposit",         5000.00, 0.00, "Owner initial deposit"),
        ("TXN-BANK-001", "2026-01-05", "Common Stock",    "Owner deposit",         0.00,    5000.00, "Owner initial deposit"),
        ("TXN-BANK-002", "2026-01-10", "Equipment",       "Laptop purchase",       1200.00, 0.00, "Bought laptop for design team"),
        ("TXN-BANK-002", "2026-01-10", "Cash",            "Laptop purchase",       0.00,    1200.00, "Bought laptop for design team"),
        ("TXN-BANK-003", "2026-01-15", "Utilities",       "Electricity bill",      180.00,  0.00, "Electricity bill - bank fee glitch"),
        ("TXN-BANK-003", "2026-01-15", "Cash",            "Electricity bill",      0.00,    165.00, "Electricity bill - bank fee glitch"),
    ]
    return pd.DataFrame(rows, columns=["Reference", "Txn Date", "Acct", "AcctDesc", "DR", "CR", "Memo"])


def make_expenses_july() -> pd.DataFrame:
    """Single-sided expense/payment log -- one row per real-world payment,
    with messy category text and a mix of Cash vs Accounts Payable."""
    rows = [
        ("2026-07-02", "City Power Co",       "Utilities",           220.50, "Cash"),
        ("2026-07-05", "WeWork HQ",           "Office Rent",         1500.00, "Cash"),
        ("2026-07-08", "ADP Payroll",         "Salaries",            4200.00, "Cash"),
        ("2026-07-12", "Facebook Ads",        "Marketing Expense",   350.00, "Cash"),
        ("2026-07-18", "Office Depot",        "Capital Expenditure", 899.00, "Cash"),
        ("2026-07-22", "Print Supplies Inc",  "COGS",                610.75, "Accounts Payable"),
    ]
    return pd.DataFrame(rows, columns=["Txn Date", "VendorDesc", "Category", "Amount Paid", "Paid Via"])


def make_sales_invoices_july() -> pd.DataFrame:
    """Single-sided revenue log. 'Paid' invoices debit Cash; 'Unpaid'
    invoices debit Accounts Receivable -- classic accrual-basis accounting."""
    rows = [
        ("2026-07-03", "Acme Corp",    "Product Sales",       3200.00, "Paid"),
        ("2026-07-09", "Globex LLC",   "Consulting Revenue",  1800.00, "Unpaid"),
        ("2026-07-14", "Initech",      "Sales",               2650.00, "Paid"),
        ("2026-07-20", "Umbrella Co",  "Service",              975.00, "Unpaid"),
    ]
    return pd.DataFrame(rows, columns=["Invoice Date", "Customer", "Revenue Type", "Invoice Amount", "Status"])


def make_financing_activities() -> pd.DataFrame:
    """Loans and owner capital contributions -- populates Liability/Equity
    accounts and Financing-activity cash flows."""
    rows = [
        ("2026-07-01", "Bank loan draw for expansion", "Loan Received",   10000.00),
        ("2026-07-01", "Founder capital injection",    "Owner Investment", 5000.00),
        ("2026-07-31", "Monthly loan repayment",       "Loan Repayment",    500.00),
    ]
    return pd.DataFrame(rows, columns=["Date", "Description", "Type", "Amount"])


def make_monthly_expense_budget_wide() -> pd.DataFrame:
    """Column-based/wide export: one row per expense category, one column
    per month. Note the blank Jun-2026 Marketing cell -- a real spreadsheet
    would leave a month with no spend blank rather than writing 0, and the
    adapter's dropna() after melt() has to handle that gracefully."""
    rows = [
        ("Rent",      "Cash", 1500, 1500, 1500),
        ("Utilities", "Cash", 200,  210,  195),
        ("Marketing", "Cash", 300,  None, 450),
    ]
    return pd.DataFrame(rows, columns=["Category", "Paid Via", "Apr-2026", "May-2026", "Jun-2026"])


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    files = {
        "bank_ledger_export.xlsx": make_bank_ledger_export(),
        "expenses_july.xlsx": make_expenses_july(),
        "sales_invoices_july.xlsx": make_sales_invoices_july(),
        "financing_activities.xlsx": make_financing_activities(),
        "monthly_expense_budget_wide.xlsx": make_monthly_expense_budget_wide(),
    }

    for filename, df in files.items():
        path = RAW_DIR / filename
        df.to_excel(path, index=False, engine="openpyxl")
        print(f"[GENERATED] {path}  ({len(df)} rows)")


if __name__ == "__main__":
    main()
