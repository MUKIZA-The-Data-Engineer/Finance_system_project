# How to Run the Finance ETL Pipeline

This guide covers everything needed to run this project from a clean machine:
installing Python dependencies, running the pipeline, where the output
lands, and how to point it at your own Excel files instead of the sample
data.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10 or newer | Uses modern type-hint syntax (`str \| None`, `list[dict]`). Tested on 3.13. |
| pip | any recent | Ships with Python |

Check what you have:

```powershell
python --version
```

If `python` isn't recognized, use `py --version` instead (the Windows Python
launcher) — then substitute `py` for `python` in every command below.

---

## 2. One-time setup: virtual environment + dependencies

Open PowerShell and `cd` into the project folder:

```powershell
cd "C:\Users\jeanm\OneDrive\Desktop\project to work on\finance_system"
```

Create and activate a virtual environment (keeps this project's packages
isolated from anything else on your machine):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the activation script with an "execution policy"
> error, run this once (in an admin PowerShell) and try again:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

Install the two dependencies (both listed in `requirements.txt`):

```powershell
pip install -r requirements.txt
```

That's it — the only third-party packages this project needs are:

- **pandas** (>=2.0) — all extraction, transformation, and reporting logic
- **openpyxl** (>=3.1) — lets pandas read and write `.xlsx` files

You'll know setup worked if this prints version numbers with no errors:

```powershell
python -c "import pandas, openpyxl; print(pandas.__version__, openpyxl.__version__)"
```

Each time you come back to work on this project in a new terminal, re-activate
the environment first:

```powershell
.\.venv\Scripts\Activate.ps1
```

---

## 3. Running the pipeline

One command runs the entire thing — extraction, the double-entry engine,
validation, and all four financial statements:

```powershell`
python run_pipeline.py
```

What happens, in order:

1. **Sample data is (re)generated** into `data/gl_raw/` — five messy, differently
   -shaped `.xlsx` files (see `generate_sample_data.py`). Safe to re-run any
   time; it overwrites the same files.
2. **Extraction (Phase 1)** — `adapters.py` detects each file's shape
   (already-double-entry / single-sided / wide-column) and ingests it.
3. **Double-entry engine + validation (Phase 2)** — `etl_pipeline.py` builds
   the Universal General Ledger, then checks every transaction's Debits equal
   its Credits. Anything that fails is written to
   `data/processed/error_log.csv` and excluded from reporting.
4. **Reporting (Phase 3)** — `reports.py` builds the Trial Balance, Income
   Statement, Balance Sheet, and Cash Flow Statement purely via `groupby`/
   `merge` on the clean ledger.
5. **Output** — everything lands in:
   - `data/processed/general_ledger.csv` — the clean Universal General Ledger
   - `data/processed/error_log.csv` — any rows that failed validation
   - `output/financial_statements.xlsx` — one workbook, one sheet per report
     (`General Ledger`, `Trial Balance`, `Income Statement`, `Balance Sheet`,
     `Cash Flow Statement`)

Expected console output ends with:

```
[OK] Balance Sheet balances: Assets (...) = Liabilities + Equity (...)
[REPORT] Net Income (all periods): ...
[DONE] Financial statements written to ...\output\financial_statements.xlsx
```

If you only want to (re)generate the sample raw Excel files without running
the pipeline:

```powershell
python generate_sample_data.py
```

---

## 4. Using your OWN Excel files instead of the sample data

1. Drop your `.xlsx` file(s) into `data/gl_raw/`.
2. Each file needs to match one of the three shapes `adapters.py` understands:
   - **Already double-entry** (one row per Debit/Credit leg, grouped by a
     reference number) → register it in `FILE_ADAPTER_REGISTRY` in
     `adapters.py` pointing at `ingest_gl_passthrough`.
   - **Single-sided** (one row per real-world event: an expense, an invoice,
     a loan) → write a small adapter function modeled on
     `ingest_expense_file` / `ingest_revenue_file` / `ingest_financing_file`,
     mapping your columns to `Date, Description, Debit_Account_Label,
     Credit_Account_Label, Amount, Source_File_Ref`.
   - **Wide / column-based** (accounts as rows, months as columns) → use
     `ingest_wide_expense_budget` as a template; it's driven by `pd.melt()`.
3. Add a line to `FILE_ADAPTER_REGISTRY` (bottom of `adapters.py`) mapping
   your filename to the right ingestion function and "kind"
   (`"gl_passthrough"` or `"single_sided"`/`"wide_melt"`).
4. Re-run `python run_pipeline.py`. Unknown files that don't match any
   registered name are auto-sniffed: if their headers look wide/date-like
   they're routed through the wide-melt adapter, otherwise you'll get a clear
   error asking you to register an adapter.

---

## 5. Adding a new account to the Chart of Accounts

The Chart of Accounts is **data, not code** — it lives in
`data/chart_of_accounts.csv`. Two ways to add an account:

**Option A — edit the CSV directly** (e.g. in Excel):

```
Account_Code,Account_Name,Account_Type,Aliases
5500,Software Subscriptions,Expense,software|saas|subscriptions
```

**Option B — do it from Python**, which also refreshes everything in memory:

```python
import config
config.add_account("5500", "Software Subscriptions", "Expense",
                    aliases=["software", "saas", "subscriptions"])
```

`Account_Type` must be one of `Asset`, `Liability`, `Equity`, `Revenue`,
`Expense`. The `Aliases` column is what lets messy free-text labels in raw
files (e.g. "SaaS Tools", "software expense") resolve to this account.

---

## 6. (Optional) Reconciling against an external Chart of Accounts (e.g. Odoo)

If you have an authoritative CoA export from a real accounting system (Odoo,
QuickBooks, etc.), you can enrich/override the ledger's account codes
against it instead of relying only on the internal CSV:

```python
import etl_pipeline

clean_ledger = etl_pipeline.run_extraction_and_validation(
    "data/gl_raw",
    coa_filepath="data/odoo_coa_sample.csv",   # or None to skip this step
)
```

- `coa_filepath=None` (the default) — bypassed entirely; codes/types come
  straight from the internal Chart of Accounts.
- `coa_filepath="path/to/file.csv"` — a `pd.merge` **left join** on
  `Account_Name` overrides `Account_Code`/`Account_Type` wherever a match is
  found; unmatched accounts keep their internally-resolved values. The
  external CSV needs columns recognizable as code/name/type (e.g. Odoo's own
  `code,name,account_type` headers work out of the box — see
  `data/odoo_coa_sample.csv` for the expected shape).

`run_pipeline.py` already demonstrates both branches side-by-side in its
console output ("Optional External CoA Enrichment Demo").

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError: Unmapped account label: '...'` (in `error_log.csv`, `Error_Type = Unmapped Account Label`) | A raw file uses account wording not in `ACCOUNT_ALIASES` | Add an alias for it (Section 5), or rename the column value in the source file |
| `Error_Type = Unbalanced Transaction (Debit != Credit)` in `error_log.csv` | A transaction's legs don't sum to zero | This is the pipeline working as intended — investigate that `Transaction_ID`/`Source_File_Ref` in the source file and correct it, then re-run |
| `KeyError: Expected column(s) [...] not found` | A raw file is missing/misnamed a column the adapter expects | Check the column names against the adapter's `*_HEADER_MAP` in `adapters.py`, or rename the header in your Excel file |
| `ValueError: Unparseable date value(s)` | A date column has text pandas can't parse | Fix the offending cell(s) in the source Excel file |
| PowerShell won't run `Activate.ps1` | Execution policy blocks scripts | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry |
| `ModuleNotFoundError: No module named 'pandas'` | Dependencies not installed, or venv not activated | Run `.\.venv\Scripts\Activate.ps1` then `pip install -r requirements.txt` |
