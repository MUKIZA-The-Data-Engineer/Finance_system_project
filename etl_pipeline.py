"""
etl_pipeline.py
================
Phase 2: THE DOUBLE-ENTRY ENGINE + VALIDATION.

This is the heart of the "Odoo-style" philosophy: no matter which adapter a
raw file went through, everything funnels into ONE function here that
either (a) trusts already-double-entry rows, or (b) explodes single-sided
staging rows into a balanced Debit leg + Credit leg. The output is always
the same Universal General Ledger schema, so reports.py never needs to
know or care where a row originally came from.

    Transaction_ID | Date | Account_Code | Account_Name | Account_Type |
    Description | Debit | Credit | Source_File_Ref

After the ledger is assembled, we run the one rule that makes double-entry
bookkeeping meaningful: for every Transaction_ID, total Debits must equal
total Credits. Anything that fails is quarantined to error_log.csv and
excluded from the ledger used for reporting -- bad data should never
silently distort a financial statement.
"""

import itertools
from pathlib import Path

import pandas as pd

import config

GL_COLUMNS = [
    "Transaction_ID",
    "Date",
    "Account_Code",
    "Account_Name",
    "Account_Type",
    "Description",
    "Debit",
    "Credit",
    "Source_File_Ref",
]

# Monotonic counter used to mint Transaction_IDs for single-sided staging
# rows, where one staging row = one real-world transaction. Using a simple
# counter (rather than a UUID) keeps the demo output human-readable, e.g.
# 'TXN-00001', 'TXN-00002', ...
_txn_counter = itertools.count(start=1)


def _resolve_label(label: str) -> tuple[str, str, str]:
    """Translate a messy free-text account label ('Rent', 'office rent',
    'RENT-EXPENSE') into a (Account_Code, Account_Name, Account_Type)
    triple, using config.ACCOUNT_ALIASES.

    Matching strategy: exact match first (fast path for clean data), then
    a substring match against every known alias (handles messier variants
    like 'Rent Expense (HQ - Q3)' containing the known alias 'rent
    expense'). Raises if nothing matches -- callers turn that into a
    per-row entry in error_log.csv rather than crashing the whole batch.
    """
    key = str(label).strip().lower()

    # NOTE: accessed as config.ACCOUNT_ALIASES / config.COA_BY_CODE (not
    # imported by name) so that config.add_account() -- which rebinds these
    # names on the config module at runtime -- is always seen immediately,
    # even if this function was already compiled/imported beforehand.
    code = config.ACCOUNT_ALIASES.get(key)
    if code is None:
        # Substring fallback: sort candidate aliases longest-first so a
        # more specific alias ("rent expense") wins over a shorter one
        # that happens to also be a substring ("rent").
        for alias in sorted(config.ACCOUNT_ALIASES, key=len, reverse=True):
            if alias in key:
                code = config.ACCOUNT_ALIASES[alias]
                break

    if code is None:
        raise KeyError(f"Unmapped account label: '{label}'")

    coa_row = config.COA_BY_CODE.loc[code]
    return code, coa_row["Account_Name"], coa_row["Account_Type"]


def expand_single_sided(staging_df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Explode single-sided staging rows (one row per transaction, with
    Debit_Account_Label / Credit_Account_Label / Amount) into balanced GL
    row PAIRS: one Debit leg and one Credit leg sharing a fresh
    Transaction_ID. This is literally what "double-entry" means -- every
    event that increases one account simultaneously decreases (or
    increases the opposite side of) another.

    Returns (gl_rows_df, skipped_rows) where skipped_rows records any
    staging row whose account label couldn't be resolved -- these are
    reported as extraction-time errors alongside the balance-validation
    errors from validate_ledger().
    """
    gl_rows = []
    skipped = []

    for _, row in staging_df.iterrows():
        txn_id = f"TXN-{next(_txn_counter):05d}"
        try:
            debit_code, debit_name, debit_type = _resolve_label(row["Debit_Account_Label"])
            credit_code, credit_name, credit_type = _resolve_label(row["Credit_Account_Label"])
        except KeyError as exc:
            skipped.append({**row.to_dict(), "Reason": str(exc)})
            continue

        amount = row["Amount"]
        shared = {
            "Transaction_ID": txn_id,
            "Date": row["Date"],
            "Description": row["Description"],
            "Source_File_Ref": row["Source_File_Ref"],
        }
        gl_rows.append(
            {**shared, "Account_Code": debit_code, "Account_Name": debit_name,
             "Account_Type": debit_type, "Debit": amount, "Credit": 0.0}
        )
        gl_rows.append(
            {**shared, "Account_Code": credit_code, "Account_Name": credit_name,
             "Account_Type": credit_type, "Debit": 0.0, "Credit": amount}
        )

    return pd.DataFrame(gl_rows, columns=GL_COLUMNS), skipped


def normalize_passthrough(staging_df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Resolve account labels on already-double-entry staging rows (from
    ingest_gl_passthrough). Unlike expand_single_sided, no new rows are
    invented -- we only look up Account_Code/Name/Type for the label the
    source file already tagged each row with."""
    gl_rows = []
    skipped = []

    for _, row in staging_df.iterrows():
        try:
            code, name, acct_type = _resolve_label(row["Account_Label"])
        except KeyError as exc:
            skipped.append({**row.to_dict(), "Reason": str(exc)})
            continue

        gl_rows.append(
            {
                "Transaction_ID": row["Transaction_ID"],
                "Date": row["Date"],
                "Account_Code": code,
                "Account_Name": name,
                "Account_Type": acct_type,
                "Description": row["Description"],
                "Debit": row["Debit"],
                "Credit": row["Credit"],
                "Source_File_Ref": row["Source_File_Ref"],
            }
        )

    return pd.DataFrame(gl_rows, columns=GL_COLUMNS), skipped


def build_general_ledger(staged: list[tuple[str, pd.DataFrame]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run every (staging_kind, staging_df) pair produced by adapters.py
    through the appropriate engine step and concatenate the results into
    one Universal General Ledger. Returns (raw_ledger, extraction_errors);
    extraction_errors captures rows dropped for unresolvable account
    labels (kept separate from the Debit/Credit balance errors raised by
    validate_ledger(), but both ultimately land in error_log.csv)."""
    ledger_parts = []
    extraction_errors = []

    for kind, staging_df in staged:
        if staging_df.empty:
            continue
        if kind == "gl_passthrough":
            gl_part, skipped = normalize_passthrough(staging_df)
        else:  # 'single_sided' or 'wide_melt' -- both need offset-leg expansion
            gl_part, skipped = expand_single_sided(staging_df)

        ledger_parts.append(gl_part)
        extraction_errors.extend(skipped)

    raw_ledger = pd.concat(ledger_parts, ignore_index=True) if ledger_parts else pd.DataFrame(columns=GL_COLUMNS)
    errors_df = pd.DataFrame(extraction_errors)
    return raw_ledger, errors_df


def validate_ledger(raw_ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """THE crucial accounting invariant: for every Transaction_ID, total
    Debits must equal total Credits (within floating-point tolerance).

    We use groupby('Transaction_ID') to collapse the ledger down to one
    row per transaction with summed Debit/Credit columns, then compare.
    Any Transaction_ID that fails is entirely excluded from the clean
    ledger (both legs -- you can't keep half of an unbalanced entry) and
    every one of its original rows is written out for investigation.

    Returns (clean_ledger, balance_errors_df).
    """
    if raw_ledger.empty:
        return raw_ledger, pd.DataFrame()

    txn_totals = raw_ledger.groupby("Transaction_ID")[["Debit", "Credit"]].sum()
    txn_totals["Difference"] = (txn_totals["Debit"] - txn_totals["Credit"]).round(2)

    tolerance = 0.01  # absorb float rounding noise, not real accounting errors
    unbalanced_ids = txn_totals.index[txn_totals["Difference"].abs() > tolerance]

    is_unbalanced = raw_ledger["Transaction_ID"].isin(unbalanced_ids)
    clean_ledger = raw_ledger.loc[~is_unbalanced].reset_index(drop=True)

    balance_errors = raw_ledger.loc[is_unbalanced].merge(
        txn_totals.loc[unbalanced_ids, "Difference"],
        left_on="Transaction_ID",
        right_index=True,
    )

    return clean_ledger, balance_errors


EXTERNAL_COA_HEADER_MAP = {
    # Accepts a few common spellings so an Odoo export ('code'/'name'/
    # 'user_type') or a generic export ('account_code'/'account_name'/
    # 'account_type') both work without editing this module.
    "code": "Account_Code", "account_code": "Account_Code",
    "name": "Account_Name", "account_name": "Account_Name",
    "account_type": "Account_Type", "user_type": "Account_Type", "type": "Account_Type",
}


def load_external_coa(coa_filepath: str | Path) -> pd.DataFrame:
    """Read an external, authoritative Chart of Accounts export (e.g. a
    CSV pulled from a live Odoo instance: 'odoo_coa.csv') and normalize its
    headers to Account_Code / Account_Name / Account_Type so it can be
    merged against the internally-resolved ledger."""
    raw = pd.read_csv(coa_filepath, dtype=str)
    raw.columns = raw.columns.str.strip().str.lower().str.replace(" ", "_")
    unknown = [c for c in raw.columns if c not in EXTERNAL_COA_HEADER_MAP]
    if unknown:
        raise KeyError(
            f"External CoA file '{coa_filepath}' has unrecognized column(s) {unknown}. "
            f"Expected some combination of {sorted(set(EXTERNAL_COA_HEADER_MAP))}."
        )
    return raw.rename(columns=EXTERNAL_COA_HEADER_MAP)


def enrich_with_external_coa(gl: pd.DataFrame, coa_filepath: str | Path | None = None) -> pd.DataFrame:
    """OPTIONAL Phase 1 enrichment step, applied right after ingestion.

    - coa_filepath is None (default): bypass this step entirely and return
      the ledger unchanged, i.e. rely on the Account_Code/Account_Type the
      pipeline already resolved from the raw input files via the internal
      Chart of Accounts (config.ACCOUNT_ALIASES).

    - coa_filepath is given: treat that file as an authoritative EXTERNAL
      Chart of Accounts (e.g. a real Odoo export) and pd.merge() it against
      the ledger as a LEFT JOIN on Account_Name. Wherever the external CoA
      has a matching account name, its Account_Code/Account_Type OVERRIDE
      the internally-resolved values (e.g. remapping our internal '1000'
      to Odoo's real '101000' cash account code). Accounts with no match
      in the external file keep their internally-resolved values -- a
      left join never drops ledger rows, it only enriches them.
    """
    if coa_filepath is None:
        return gl

    external_coa = load_external_coa(coa_filepath)

    gl = gl.copy()
    gl["_join_key"] = gl["Account_Name"].str.strip().str.lower()
    external_coa["_join_key"] = external_coa["Account_Name"].str.strip().str.lower()

    merged = gl.merge(
        external_coa[["_join_key", "Account_Code", "Account_Type"]],
        on="_join_key",
        how="left",              # LEFT JOIN: keep every ledger row, matched or not
        suffixes=("", "_external"),
    )

    matched = merged["Account_Code_external"].notna()
    print(f"[CoA ENRICH] {matched.sum()}/{len(merged)} row(s) matched '{coa_filepath}'; "
          f"{(~matched).sum()} kept their internally-resolved account codes.")

    # combine_first: prefer the external (authoritative) value; fall back
    # to the internally-resolved value for names the external file doesn't know.
    merged["Account_Code"] = merged["Account_Code_external"].combine_first(merged["Account_Code"])
    merged["Account_Type"] = merged["Account_Type_external"].combine_first(merged["Account_Type"])

    return merged.drop(columns=["_join_key", "Account_Code_external", "Account_Type_external"])[GL_COLUMNS]


def write_error_log(extraction_errors: pd.DataFrame, balance_errors: pd.DataFrame, path: str | Path) -> None:
    """Combine both error sources (unmapped account labels at extraction
    time, unbalanced transactions at validation time) into one
    error_log.csv so an accountant has a single place to look."""
    parts = []
    if not extraction_errors.empty:
        tagged = extraction_errors.copy()
        tagged["Error_Type"] = "Unmapped Account Label"
        parts.append(tagged)
    if not balance_errors.empty:
        tagged = balance_errors.copy()
        tagged["Error_Type"] = "Unbalanced Transaction (Debit != Credit)"
        parts.append(tagged)

    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    combined.to_csv(path, index=False)

    if not combined.empty:
        print(f"[ALERT] {len(combined)} row(s) failed validation. See {path}")
    else:
        print("[OK] No validation errors. All transactions balance.")


def run_extraction_and_validation(raw_dir: str | Path, coa_filepath: str | Path | None = None) -> pd.DataFrame:
    """End-to-end Phase 1 + Phase 2 orchestration: discover raw files,
    run each through its adapter, explode/normalize into the Universal GL,
    OPTIONALLY enrich/override Account_Code + Account_Type against an
    external Chart of Accounts, validate, and return only the CLEAN ledger
    (ready for Phase 3 reporting). Also writes data/processed/error_log.csv
    as a side effect, matching the spec's requirement to "drop the
    corrupted batch into an error log ... and exclude it from the main
    ledger."

    coa_filepath: path to an external CoA export (e.g. 'odoo_coa.csv').
        Pass None (the default) to skip enrichment entirely and rely
        solely on the account codes/types resolved from the raw input
        files via the internal Chart of Accounts.
    """
    import adapters  # local import avoids a circular import at module load time

    raw_dir = Path(raw_dir)
    staged = []
    for filepath in sorted(raw_dir.glob("*.xlsx")):
        kind, staging_df = adapters.detect_and_ingest(filepath)
        staged.append((kind, staging_df))
        print(f"[EXTRACT] {filepath.name}: {len(staging_df)} row(s) via '{kind}' adapter")

    raw_ledger, extraction_errors = build_general_ledger(staged)
    raw_ledger = enrich_with_external_coa(raw_ledger, coa_filepath)
    clean_ledger, balance_errors = validate_ledger(raw_ledger)

    error_log_path = Path("data/processed/error_log.csv")
    error_log_path.parent.mkdir(parents=True, exist_ok=True)
    write_error_log(extraction_errors, balance_errors, error_log_path)

    ledger_path = Path("data/processed/general_ledger.csv")
    clean_ledger.to_csv(ledger_path, index=False)
    print(f"[LOAD] {len(clean_ledger)} clean GL row(s) written to {ledger_path}")

    return clean_ledger
