"""
generate_sample_workbook.py
============================
Creates one deliberately messy, multi-sheet .xlsx fixture for exercising
the ai_ingestion pipeline end-to-end:

  - "Cover"          - a title/logo placeholder sheet, no data (should be SKIPPED)
  - "Instructions"   - prose text, no tabular data (should be SKIPPED)
  - "Jan_2026"       - a bank statement with a 3-row title block ABOVE its
                       real header row (tests dynamic header detection)
  - "Bank_Account_1" - a differently-worded export with the header on row 0
                       (tests that a DIFFERENT column vocabulary gets its
                       own schema fingerprint / mapping)
  - "Feb_2026"       - SAME column layout as Jan_2026, to prove the schema
                       Fast Lane (memory.py) is hit on the second occurrence
"""

from pathlib import Path

import pandas as pd

OUTPUT_PATH = Path(__file__).parent / "messy_multi_sheet_export.xlsx"


def build_cover_sheet() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["ACME HOLDINGS INC."],
            ["Annual Export Package"],
            ["Generated for internal use only"],
        ]
    )


def build_instructions_sheet() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["How to use this workbook"],
            ["Please review each tab before distributing."],
            ["Contact finance@acme.example with questions."],
        ]
    )


def build_jan_2026_sheet() -> pd.DataFrame:
    """Title block in rows 0-2, real header on row 3 (0-indexed)."""
    title_block = [
        ["ACME HOLDINGS INC. - CHECKING ACCOUNT"],
        ["Statement Period: 01-Jan-2026 to 31-Jan-2026"],
        [],
    ]
    header = ["Trans Date", "Details", "Debit Amount", "Credit Amount", "Running Balance"]
    rows = [
        ["2026-01-03", "ACME Payroll Services", 0, 8000.00, 8000.00],
        ["2026-01-06", "City Power & Water", 220.15, 0, 7779.85],
        ["2026-01-12", "Office Supplies Co", 145.60, 0, 7634.25],
        ["2026-01-20", "Client Invoice #4021 - Globex", 0, 3200.00, 10834.25],
        ["2026-01-28", "Payroll Run - Staff", 4500.00, 0, 6334.25],
    ]
    all_rows = title_block + [header] + rows
    return pd.DataFrame(all_rows)


def build_feb_2026_sheet() -> pd.DataFrame:
    """Same schema as Jan_2026 (same title-block-then-header shape) --
    should hit the schema memory Fast Lane on the second occurrence."""
    title_block = [
        ["ACME HOLDINGS INC. - CHECKING ACCOUNT"],
        ["Statement Period: 01-Feb-2026 to 28-Feb-2026"],
        [],
    ]
    header = ["Trans Date", "Details", "Debit Amount", "Credit Amount", "Running Balance"]
    rows = [
        ["2026-02-02", "ACME Payroll Services", 0, 8000.00, 14334.25],
        ["2026-02-09", "Umbrella Corp Invoice #55", 0, 1800.00, 16134.25],
        ["2026-02-15", "City Power & Water", 235.40, 0, 15898.85],
        ["2026-02-27", "Payroll Run - Staff", 4500.00, 0, 11398.85],
    ]
    all_rows = title_block + [header] + rows
    return pd.DataFrame(all_rows)


def build_bank_account_1_sheet() -> pd.DataFrame:
    """Header directly on row 0, DIFFERENT column vocabulary than the
    Jan/Feb sheets above -- must get its own schema fingerprint."""
    return pd.DataFrame(
        [
            ["Date", "Narrative", "Counterparty", "Paid Out", "Paid In", "Balance"],
            ["2026-01-04", "Wire transfer received", "Initech LLC", 0, 12500.00, 12500.00],
            ["2026-01-11", "Equipment lease payment", "LeaseCo", 950.00, 0, 11550.00],
            ["2026-01-19", "Marketing services", "AdWorks Media", 600.00, 0, 10950.00],
            ["2026-01-25", "Customer refund issued", "Jane Doe", 120.00, 0, 10830.00],
        ]
    )


def main() -> None:
    sheets = {
        "Cover": build_cover_sheet(),
        "Instructions": build_instructions_sheet(),
        "Jan_2026": build_jan_2026_sheet(),
        "Bank_Account_1": build_bank_account_1_sheet(),
        "Feb_2026": build_feb_2026_sheet(),
    }

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)

    print(f"[GENERATED] {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
