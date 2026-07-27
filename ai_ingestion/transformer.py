"""
transformer.py
==============
Stage 5: SHEET CONSOLIDATION & TRANSFORMATION.

Once every sheet has an approved raw-column -> UGL-column mapping (from
pipeline.py), this module actually applies those mappings, enforces
consistent data types, tags every row with its originating sheet for
audit traceability, and concatenates everything into one master Universal
General Ledger DataFrame -- then runs a whole-ledger accounting integrity
check across it.
"""

from __future__ import annotations

import pandas as pd

TARGET_COLUMNS = ["Date", "Description", "Payee", "Debit", "Credit", "Balance"]
NUMERIC_COLUMNS = ["Debit", "Credit", "Balance"]
TEXT_COLUMNS = ["Description", "Payee"]


def apply_mapping(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Select and rename only the raw columns that have an approved target
    in `mapping`, producing a DataFrame with the standard UGL column set.
    Any TARGET_COLUMNS not covered by this sheet's mapping (e.g. a sheet
    with no separate 'Payee' column) are added as all-NaN so every
    transformed sheet has an identical shape before concatenation."""
    raw_to_target = {raw: target for raw, target in mapping.items() if raw in df.columns}
    selected = df[list(raw_to_target.keys())].rename(columns=raw_to_target)

    for col in TARGET_COLUMNS:
        if col not in selected.columns:
            selected[col] = pd.NA

    return selected[TARGET_COLUMNS]


def standardize_types(df: pd.DataFrame) -> pd.DataFrame:
    """Enforce the Universal General Ledger's strict data types:
      - Date        -> ISO 'YYYY-MM-DD' string (unambiguous, sortable,
                       matches the format Odoo's importer expects)
      - Debit/Credit/Balance -> float, missing values filled with 0.0
                       (a blank Debit cell means "no debit", not "unknown")
      - Description/Payee   -> stripped string, missing values filled with ""
    """
    df = df.copy()

    parsed_dates = pd.to_datetime(df["Date"], errors="coerce")
    df["Date"] = parsed_dates.dt.strftime("%Y-%m-%d")

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    for col in TEXT_COLUMNS:
        df[col] = df[col].astype(str).str.strip().replace({"nan": "", "None": ""}).fillna("")

    return df


def add_source_sheet(df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
    """Tag every row with the sheet it came from, e.g. 'Jan_2026' or
    'Bank_Account_1' -- required for audit traceability once sheets from
    many different sources are merged into one master ledger."""
    df = df.copy()
    df["source_sheet"] = sheet_name
    return df


def transform_sheet(df: pd.DataFrame, mapping: dict[str, str], sheet_name: str) -> pd.DataFrame:
    """Full per-sheet transformation: apply the approved mapping, enforce
    types, and tag with source_sheet. Returns a DataFrame with exactly
    TARGET_COLUMNS + ['source_sheet']."""
    mapped = apply_mapping(df, mapping)
    typed = standardize_types(mapped)
    return add_source_sheet(typed, sheet_name)


def consolidate(financial_sheets: dict[str, pd.DataFrame], mappings: dict[str, dict]) -> pd.DataFrame:
    """Transform every sheet and concatenate them into one master Universal
    General Ledger DataFrame. `mappings` is keyed by sheet_name and holds
    the result dict from pipeline.process_sheet() (must contain a
    'mapping' key)."""
    transformed = [
        transform_sheet(df, mappings[sheet_name]["mapping"], sheet_name)
        for sheet_name, df in financial_sheets.items()
    ]
    if not transformed:
        return pd.DataFrame(columns=TARGET_COLUMNS + ["source_sheet"])

    master = pd.concat(transformed, ignore_index=True)
    return master


def check_debit_credit_integrity(master_df: pd.DataFrame, tolerance: float = 0.01) -> dict:
    """Whole-ledger accounting integrity check: total Debits should equal
    total Credits across every consolidated sheet.

    Unlike a true double-entry engine (which validates balance PER
    transaction), raw bank/ledger exports rarely carry a shared
    transaction ID linking a debit to its offsetting credit -- each row
    here is usually already a single, final transaction line. So this
    check is a whole-dataset sanity flag (are debits and credits at least
    in the same ballpark, as you'd expect from a real ledger extract?)
    rather than a per-row balancing rule. It flags, it doesn't drop rows.
    """
    total_debit = float(master_df["Debit"].sum())
    total_credit = float(master_df["Credit"].sum())
    difference = round(total_debit - total_credit, 2)
    is_balanced = abs(difference) <= tolerance

    report = {
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": difference,
        "is_balanced": is_balanced,
    }

    if is_balanced:
        print(f"[OK] Debit/Credit integrity check passed (Debit={total_debit:,.2f}, Credit={total_credit:,.2f}).")
    else:
        print(
            f"[ALERT] Debit/Credit imbalance detected: "
            f"Debit={total_debit:,.2f}, Credit={total_credit:,.2f}, Difference={difference:,.2f}. "
            "Review source sheets/mappings before importing into Odoo."
        )

    return report
