"""
exporter.py
===========
Stage 6: ODOO EXPORT LAYER.

Formats the consolidated master Universal General Ledger into a CSV/Excel
file shaped for Odoo's `account.move` (Journal Entries) import.

Design note -- why two lines per source row
--------------------------------------------
This pipeline's UGL schema (Date, Description, Payee, Debit, Credit,
Balance) deliberately has NO Chart-of-Accounts classification step (unlike
a full bookkeeping engine) -- each source row is normally already a single
bank/ledger transaction line, not a matched pair of debit/credit legs.
But Odoo's `account.move` model is strict double-entry: every Journal
Entry's lines must balance (SUM(Debit) == SUM(Credit)) on their own.

So each UGL row is exported as ONE Odoo Journal Entry made of two lines,
sharing one external ID (grouping them into a single balanced move):
  1. A line against DEFAULT_BANK_ACCOUNT_CODE, carrying the row's actual
     Debit/Credit exactly as extracted from the source file.
  2. An offsetting line against DEFAULT_SUSPENSE_ACCOUNT_CODE (Debit/Credit
     swapped), standing in for "the other side of this transaction, to be
     reclassified to its real expense/revenue/asset account once imported
     into Odoo."

This mirrors how real bank-feed-to-ledger workflows operate: transactions
land against a suspense/clearing account first, then get reconciled to
their true account. EDIT THE CONSTANTS BELOW to match your actual Odoo
Chart of Accounts and Journal before importing.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# --- EDIT THESE to match your real Odoo setup before importing -------------
DEFAULT_JOURNAL_CODE = "BANK1"                # the Odoo Journal these entries post to
DEFAULT_BANK_ACCOUNT_CODE = "1000"            # the real account this statement/ledger represents
DEFAULT_SUSPENSE_ACCOUNT_CODE = "499999"      # temporary holding account, reclassify in Odoo post-import
# -----------------------------------------------------------------------------

ODOO_COLUMNS = [
    "id",            # external ID grouping the two lines of one Journal Entry
    "date",
    "journal_id",
    "account_id",
    "partner",
    "label",
    "debit",
    "credit",
]


def build_odoo_journal_entries(master_df: pd.DataFrame) -> pd.DataFrame:
    """Convert the master UGL DataFrame into Odoo `account.move`-ready
    lines: two rows (one balanced Journal Entry) per source UGL row."""
    lines: list[dict] = []

    for i, row in master_df.reset_index(drop=True).iterrows():
        external_id = f"ugl_import_{i}"
        label = row["Description"] or row["Payee"] or "Imported transaction"

        lines.append(
            {
                "id": external_id,
                "date": row["Date"],
                "journal_id": DEFAULT_JOURNAL_CODE,
                "account_id": DEFAULT_BANK_ACCOUNT_CODE,
                "partner": row["Payee"],
                "label": label,
                "debit": row["Debit"],
                "credit": row["Credit"],
            }
        )
        lines.append(
            {
                "id": external_id,  # SAME external id -> Odoo groups these two lines into one move
                "date": row["Date"],
                "journal_id": DEFAULT_JOURNAL_CODE,
                "account_id": DEFAULT_SUSPENSE_ACCOUNT_CODE,
                "partner": row["Payee"],
                "label": label,
                "debit": row["Credit"],   # swapped: this is the offsetting leg
                "credit": row["Debit"],
            }
        )

    return pd.DataFrame(lines, columns=ODOO_COLUMNS)


def export_to_csv(odoo_df: pd.DataFrame, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    odoo_df.to_csv(output_path, index=False)
    return output_path


def export_to_excel(odoo_df: pd.DataFrame, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    odoo_df.to_excel(output_path, index=False, engine="openpyxl")
    return output_path


def generate_odoo_export(master_df: pd.DataFrame, output_dir: str | Path) -> dict[str, Path]:
    """Build the Odoo-ready journal entry lines and write BOTH a CSV and
    an Excel copy to `output_dir`. Returns the paths written."""
    odoo_df = build_odoo_journal_entries(master_df)
    output_dir = Path(output_dir)

    csv_path = export_to_csv(odoo_df, output_dir / "odoo_journal_entries.csv")
    xlsx_path = export_to_excel(odoo_df, output_dir / "odoo_journal_entries.xlsx")

    return {"csv": csv_path, "xlsx": xlsx_path}
