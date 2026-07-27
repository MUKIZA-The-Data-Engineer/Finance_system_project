"""
ingestion.py
============
Stage 1 of the Confidence-Driven Pipeline: MULTI-SHEET INGESTION & DISCOVERY.

Real-world "here's our export" workbooks are rarely one clean table. They
usually contain a mix of:
  - A cover/title sheet, an instructions sheet, a dashboard/summary sheet
    (no transactional data at all)
  - One or more genuine transaction sheets, each often preceded by a few
    rows of logo/title/period text before the real header row appears

This module reads EVERY sheet, figures out where each sheet's true header
row actually is (if it has one), scores that row against a financial
vocabulary, and returns only the sheets that look like real ledger/
statement data -- ready for schema fingerprinting (memory.py) and mapping
(ai_mapper.py / pipeline.py).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Keywords that show up in real financial statement/ledger header rows.
# Matching is substring-based and case-insensitive, so "Transaction Date"
# matches both "transaction" and "date"; "Debit Amount" matches both
# "debit" and "amount".
FINANCIAL_KEYWORDS = [
    "date", "amount", "debit", "credit", "balance", "description", "payee",
    "memo", "narrative", "particulars", "reference", "transaction",
    "withdrawal", "deposit", "paid", "received", "payment", "invoice",
    "account", "vendor", "customer",
]

MAX_HEADER_SCAN_ROWS = 15   # per spec: only scan the first 15 rows for a header
MIN_KEYWORD_SCORE = 2       # a real header row should hit at least 2 distinct keywords
MIN_STRING_RATIO = 0.5      # at least half the row's non-null cells should be text, not numbers


def read_workbook(file_path: str | Path) -> dict[str, pd.DataFrame]:
    """Read every sheet of the workbook as RAW, header-less data.

    header=None is deliberate: we don't yet know which row (if any) is the
    real header, so we pull everything in as positional columns (0, 1, 2,
    ...) and scan it ourselves in detect_header_row(). Reading with the
    default header=0 would silently (and often wrongly) treat row 0 --
    frequently a company name or logo placeholder -- as the header.
    """
    return pd.read_excel(file_path, sheet_name=None, header=None)


def _clean_cell(value: object) -> str:
    """Normalize one raw cell to lowercase, stripped text for keyword
    matching. NaN/None becomes an empty string rather than the literal
    'nan', so it never accidentally matches anything."""
    if pd.isna(value):
        return ""
    return str(value).strip().lower()


def score_row(row: pd.Series) -> tuple[int, float]:
    """Score a single raw row as a header-row CANDIDATE.

    Returns (keyword_score, string_ratio):
      - keyword_score: how many DISTINCT financial keywords appear anywhere
        in this row's cells (a real header like
        ['Trans Date', 'Debit Amount', 'Credit Amount', 'Balance'] hits
        'date', 'debit', 'amount', 'credit', 'balance' = 5).
      - string_ratio: fraction of non-empty cells that are text. A genuine
        header row is almost entirely short text labels; a DATA row that
        happens to contain a 'Date' value and a numeric 'Amount' is mostly
        numbers/timestamps, not text, so this ratio tells them apart.
    """
    cleaned_cells = [_clean_cell(v) for v in row]
    non_empty = [c for c in cleaned_cells if c]
    if not non_empty:
        return 0, 0.0

    matched_keywords = {kw for kw in FINANCIAL_KEYWORDS if any(kw in cell for cell in non_empty)}

    text_like = 0
    for original, cleaned in zip(row, cleaned_cells):
        if not cleaned:
            continue
        # A cell counts as "text-like" if the ORIGINAL value is a string
        # (not a number/timestamp that merely stringifies to something
        # containing a keyword-like substring).
        if isinstance(original, str):
            text_like += 1

    string_ratio = text_like / len(non_empty)
    return len(matched_keywords), string_ratio


def detect_header_row(raw_df: pd.DataFrame, max_scan_rows: int = MAX_HEADER_SCAN_ROWS) -> int | None:
    """Scan the first `max_scan_rows` rows of a raw (header=None) sheet and
    return the index of the row that best looks like a real header, or
    None if nothing in that window clears the bar.

    We pick the highest-scoring row (by keyword_score, tie-broken by
    string_ratio) among candidates that meet both MIN_KEYWORD_SCORE and
    MIN_STRING_RATIO -- this rejects title-block rows (low keyword score)
    and data rows that coincidentally mention a keyword (low string ratio).
    """
    best_row_idx: int | None = None
    best_score = (-1, -1.0)

    scan_limit = min(max_scan_rows, len(raw_df))
    for row_idx in range(scan_limit):
        keyword_score, string_ratio = score_row(raw_df.iloc[row_idx])
        if keyword_score < MIN_KEYWORD_SCORE or string_ratio < MIN_STRING_RATIO:
            continue
        candidate_score = (keyword_score, string_ratio)
        if candidate_score > best_score:
            best_score = candidate_score
            best_row_idx = row_idx

    return best_row_idx


def _header_text(value: object, fallback_index: int) -> str:
    """Turn one header cell into a column name, preserving its ORIGINAL
    casing/spacing (unlike _clean_cell, which lowercases for keyword
    matching). The AI mapper and the human CLI confirmation both display
    these names verbatim, so a bookkeeper should see 'Trans Date' exactly
    as it appears in their spreadsheet, not a lowercased 'trans date'."""
    if pd.isna(value):
        return f"Unnamed_{fallback_index}"
    text = str(value).strip()
    return text or f"Unnamed_{fallback_index}"


def build_sheet_dataframe(raw_df: pd.DataFrame, header_row_idx: int) -> pd.DataFrame:
    """Slice a raw (header=None) sheet into a properly-headered DataFrame:
    the detected row becomes the column names, everything below becomes
    the data. Fully-empty rows/columns (common in exports with decorative
    spacing) are dropped."""
    headers = [
        _header_text(v, i)
        for i, v in enumerate(raw_df.iloc[header_row_idx])
    ]
    body = raw_df.iloc[header_row_idx + 1 :].copy()
    body.columns = headers
    body = body.dropna(axis="index", how="all").dropna(axis="columns", how="all")
    body = body.reset_index(drop=True)
    return body


def is_financial_sheet(header_row_idx: int | None, body: pd.DataFrame) -> bool:
    """A sheet only qualifies for the pipeline if it (a) actually had a
    detectable header row and (b) has at least one row of real data below
    it. This is what filters out 'Cover', 'Instructions', 'Dashboard'
    style sheets, which either have no keyword-dense row at all or have a
    header-like row with nothing underneath it."""
    return header_row_idx is not None and len(body) > 0


def discover_financial_sheets(
    file_path: str | Path,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Orchestrates the full Stage 1 flow for one workbook.

    Returns (financial_sheets, skipped_sheets):
      - financial_sheets: {sheet_name: cleaned DataFrame with real headers}
        for every sheet that passed the financial-sheet filter.
      - skipped_sheets: {sheet_name: human-readable reason} for every sheet
        that was excluded, so the CLI can tell the user WHY (e.g. "no
        header row with >= 2 financial keywords found in the first 15
        rows").
    """
    raw_sheets = read_workbook(file_path)

    financial_sheets: dict[str, pd.DataFrame] = {}
    skipped_sheets: dict[str, str] = {}

    for sheet_name, raw_df in raw_sheets.items():
        header_row_idx = detect_header_row(raw_df)

        if header_row_idx is None:
            skipped_sheets[sheet_name] = (
                f"No header row with >= {MIN_KEYWORD_SCORE} financial keywords "
                f"found in the first {MAX_HEADER_SCAN_ROWS} rows"
            )
            continue

        body = build_sheet_dataframe(raw_df, header_row_idx)

        if not is_financial_sheet(header_row_idx, body):
            skipped_sheets[sheet_name] = "Header row detected but no data rows underneath it"
            continue

        financial_sheets[sheet_name] = body

    return financial_sheets, skipped_sheets
