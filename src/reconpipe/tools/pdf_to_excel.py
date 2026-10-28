"""Tool 1: a PDF bank statement -> an Excel workbook.

A known bank layout is read by its own parser; any other PDF goes through the
generic reader (tables, then text lines). Each table becomes a sheet, and an
'about' sheet records how the file was read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reconpipe.tools.excel_out import Sheet, default_output, write_workbook
from reconpipe.tools.reader import PDF_SUFFIXES, ReadResult, UnreadableFileError, read_tables


@dataclass
class PdfToExcelResult:
    output: Path
    read: ReadResult
    rows: int


def about_sheet(read: ReadResult) -> Sheet:
    rows = [["source file", read.source_file], ["file type", read.kind]]
    for t in read.tables:
        rows.append([f"table '{t.sheet}'", f"{len(t.rows)} rows, read by {t.method}"])
        if t.account_id:
            rows.append(["account", t.account_id])
        rows.extend(["note", n] for n in t.notes)
    rows.extend(["note", n] for n in read.notes)
    return Sheet("about", ["item", "value"], rows)


def pdf_to_excel(source: Path, output: Path | None = None) -> PdfToExcelResult:
    source = Path(source)
    if source.suffix.lower() not in PDF_SUFFIXES:
        raise UnreadableFileError(f"{source.name}: pdf-to-excel needs a .pdf file")
    read = read_tables(source)
    sheets = [
        Sheet(
            t.sheet,
            ["source_row", *t.header],
            [[n, *r] for n, r in zip(t.row_numbers, t.rows, strict=True)],
        )
        for t in read.tables
    ]
    out = write_workbook(
        output or default_output(source, "extracted"), [*sheets, about_sheet(read)]
    )
    return PdfToExcelResult(out, read, sum(len(t.rows) for t in read.tables))
