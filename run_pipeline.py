"""
run_pipeline.py
================
The sample script deliverable: runs the ENTIRE ETL pipeline end-to-end and
writes one clean Excel workbook with a separate sheet per financial
statement. This is the file a non-technical user would actually double-
click (via `python run_pipeline.py`) to regenerate the books.

Pipeline flow (mirrors the three phases across adapters.py / etl_pipeline.py
/ reports.py):

    data/gl_raw/*.xlsx  --[adapters.py: Phase 1 extraction]-->
    staging rows     --[etl_pipeline.py: Phase 2 double-entry engine]-->
    Universal GL     --[etl_pipeline.py: validate_ledger()]-->
    clean GL         --[reports.py: Phase 3 groupby/pivot reporting]-->
    output/financial_statements.xlsx
"""

from pathlib import Path

import pandas as pd

import etl_pipeline
import generate_sample_data
import reports

RAW_DIR = Path("data/gl_raw")
OUTPUT_PATH = Path("output/financial_statements.xlsx")


def demo_optional_coa_enrichment(clean_ledger: pd.DataFrame) -> None:
    """Show BOTH branches of the optional external Chart-of-Accounts
    enrichment feature (etl_pipeline.enrich_with_external_coa), without
    re-running extraction:

      - coa_filepath=None            -> bypassed, ledger unchanged
      - coa_filepath='odoo_coa_sample.csv' -> Account_Code/Account_Type
        overridden via a pd.merge() left join wherever the external file
        recognizes the account name (Odoo-style 6-digit codes here).
    """
    print("\n--- Optional External CoA Enrichment Demo ---")

    bypassed = etl_pipeline.enrich_with_external_coa(clean_ledger, coa_filepath=None)
    assert bypassed["Account_Code"].equals(clean_ledger["Account_Code"])
    print("[bypass] coa_filepath=None -> Account_Code left exactly as internally resolved.")

    enriched = etl_pipeline.enrich_with_external_coa(clean_ledger, coa_filepath="data/odoo_coa_sample.csv")
    before_after = (
        clean_ledger[["Account_Name", "Account_Code"]]
        .drop_duplicates()
        .merge(
            enriched[["Account_Name", "Account_Code"]].drop_duplicates(),
            on="Account_Name",
            suffixes=("_internal", "_odoo"),
        )
        .sort_values("Account_Name")
    )
    print("[enrich] coa_filepath='data/odoo_coa_sample.csv' -> codes overridden per account:")
    print(before_after.to_string(index=False))


def main() -> None:
    # Regenerate the messy sample workbooks every run so this script is a
    # fully self-contained, reproducible demo (safe to re-run any time).
    generate_sample_data.main()

    # Phase 1 + Phase 2: extract every raw file, run the double-entry
    # engine, validate, and get back only the transactions that balance.
    # coa_filepath=None -> we rely on the account codes/types resolved
    # from the raw files via the internal, CSV-driven Chart of Accounts
    # (data/chart_of_accounts.csv). Pass a path here instead to enrich/
    # override against an external CoA export (see demo below).
    clean_ledger = etl_pipeline.run_extraction_and_validation(RAW_DIR, coa_filepath=None)

    demo_optional_coa_enrichment(clean_ledger)

    # Phase 3: every statement is just a groupby/pivot over clean_ledger.
    tb = reports.trial_balance(clean_ledger)
    income_stmt, net_income = reports.income_statement(clean_ledger)
    bs = reports.balance_sheet(clean_ledger, net_income=net_income)
    cash_flow = reports.cash_flow_statement(clean_ledger)

    print(f"\n[REPORT] Net Income (all periods): {net_income:,.2f}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        clean_ledger.to_excel(writer, sheet_name="General Ledger", index=False)
        tb.to_excel(writer, sheet_name="Trial Balance", index=False)
        income_stmt.to_excel(writer, sheet_name="Income Statement", index=False)
        bs.to_excel(writer, sheet_name="Balance Sheet", index=False)
        cash_flow.to_excel(writer, sheet_name="Cash Flow Statement", index=False)

    print(f"\n[DONE] Financial statements written to {OUTPUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
