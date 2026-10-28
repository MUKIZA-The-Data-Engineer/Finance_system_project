"""Tool 1: a PDF bank statement -> an Excel workbook.

A known bank layout is read by its own parser; any other PDF goes through the
generic reader (tables, then text lines). The workbook holds the clean,
standardised transactions, the problems found, how the columns were mapped,
the rows exactly as extracted, and an 'about' sheet saying how it was read.
If the columns cannot be mapped, the raw extraction is still written.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from reconpipe.tools.excel_out import Sheet, default_output, write_workbook
from reconpipe.tools.reader import PDF_SUFFIXES, ReadResult, UnreadableFileError, read_tables
from reconpipe.tools.standardizer import (
    MappingError,
    StandardizeOutput,
    output_sheets,
    standardize_tables,
)


@dataclass
class PdfToExcelResult:
    output: Path
    read: ReadResult
    rows: int
    standardized: StandardizeOutput | None
    mapping_problem: str | None = None


def about_sheet(read: ReadResult, extra: list[list[object]] = ()) -> Sheet:
    rows = [["source file", read.source_file], ["file type", read.kind]]
    for t in read.tables:
        rows.append([f"table '{t.sheet}'", f"{len(t.rows)} rows, read by {t.method}"])
        if t.account_id:
            rows.append(["account", t.account_id])
        rows.extend(["note", n] for n in t.notes)
    rows.extend(["note", n] for n in read.notes)
    rows.extend(extra)
    return Sheet("about", ["item", "value"], rows)


def raw_sheets(read: ReadResult) -> list[Sheet]:
    return [
        Sheet(
            f"extracted {t.sheet}"[:31],
            ["source_row", *t.header],
            [[n, *r] for n, r in zip(t.row_numbers, t.rows, strict=True)],
        )
        for t in read.tables
    ]


def pdf_to_excel(
    source: Path,
    output: Path | None = None,
    overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    today: dt.date | None = None,
) -> PdfToExcelResult:
    source = Path(source)
    if source.suffix.lower() not in PDF_SUFFIXES:
        raise UnreadableFileError(f"{source.name}: pdf-to-excel needs a .pdf file")
    read = read_tables(source, hints=list(overrides or {}))
    try:
        clean = standardize_tables(read, overrides, date_order, today)
    except MappingError as exc:
        clean, problem = None, str(exc)
    else:
        problem = None
    sheets = output_sheets(clean) if clean else []
    note = [["note", f"columns not mapped, raw rows only: {problem}"]] if problem else []
    out = write_workbook(
        output or default_output(source, "extracted"),
        [*sheets, *raw_sheets(read), about_sheet(read, note)],
    )
    rows = len(clean.rows) if clean else sum(len(t.rows) for t in read.tables)
    return PdfToExcelResult(out, read, rows, clean, problem)
