"""Bank A (Kivu Commercial Bank) PDF parser: a ruled table, debit and credit columns.

What the statement looks like, page after page:

    logo band, "STATEMENT OF ACCOUNT", "Account number: 0040-1187342-01"
    ruled table  Date | Description | Reference | Debit | Credit | Balance
                 (header repeated at the top of every page)
    footer       disclaimer and "Page n of N"; "Closing balance" after the last table

Every printed line is its own ruled table row, so a description too long for
its cell spills onto a following row whose only filled cell is Description:

    02/01/2026 | TRF TO AKAGERA OFFICE SUPPLIES LTD INV | FT26002VE2T2 | 891,750.00 | ...
               | 1041                                   |              |            |

That continuation row belongs to the transaction above it, even when a page
break falls between them, so the parser holds the last transaction open until
the next dated row arrives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException
from pydantic import ValidationError

from reconpipe.extract.base import BaseStatementParser, ParseError
from reconpipe.schemas import RawTransaction

HEADER = ("date", "description", "reference", "debit", "credit", "balance")
FIELD_FOR = {
    "date": "date",
    "description": "description",
    "reference": "ref_id",
    "debit": "debit",
    "credit": "credit",
    "balance": "balance",
}
ACCOUNT_PATTERN = re.compile(r"Account number:\s*(\S+)", re.IGNORECASE)
# What pdfplumber/pdfminer raise for a missing, corrupt or non-PDF file.
UNREADABLE = (OSError, PdfminerException, PDFSyntaxError, ValueError, KeyError, TypeError)


def _cell(value: str | None) -> str:
    """pdfplumber gives None for empty cells and '\\n' inside multi-line cells."""
    return " ".join((value or "").split())


def _is_header(row: list[str | None]) -> bool:
    return tuple(_cell(c).casefold() for c in row) == HEADER


def _statement_tables(page: pdfplumber.page.Page) -> list[list[list[str | None]]]:
    return [t for t in page.extract_tables() if t and _is_header(t[0])]


class BankAPdfParser(BaseStatementParser):
    name = "bank_a_pdf_ruled"

    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        try:
            with pdfplumber.open(path) as pdf:
                return bool(pdf.pages) and bool(_statement_tables(pdf.pages[0]))
        except UNREADABLE:
            return False

    def parse(self, path: Path) -> list[RawTransaction]:
        try:
            with pdfplumber.open(path) as pdf:
                if not pdf.pages:
                    raise ParseError(path, "PDF has no pages")
                first_text = pdf.pages[0].extract_text() or ""
                lines = [
                    [_cell(c) for c in row]
                    for page in pdf.pages
                    for table in _statement_tables(page)
                    for row in table[1:]  # skip the header repeated on every page
                ]
        except UNREADABLE as exc:
            raise ParseError(path, f"cannot open PDF: {exc}") from exc

        match = ACCOUNT_PATTERN.search(first_text)
        if match is None:
            raise ParseError(path, "no 'Account number:' on the first page")
        account_id = match.group(1)

        records: list[dict[str, str]] = []
        for line in lines:
            values = dict(zip(HEADER, line, strict=True))
            if not any(values.values()):
                continue
            if values["date"]:
                records.append(values)
            elif values["description"] and not any(
                v for k, v in values.items() if k != "description"
            ):
                if not records:
                    raise ParseError(path, "description continuation before any transaction")
                records[-1]["description"] += " " + values["description"]
            else:
                raise ParseError(path, f"undated table row with amounts: {line}")

        if not records:
            raise ParseError(path, "statement table has no transactions")

        transactions = []
        for seq, values in enumerate(records, start=1):
            try:
                transactions.append(
                    RawTransaction(
                        **{FIELD_FOR[k]: (v or None) for k, v in values.items()},
                        account_id=account_id,
                        source_file=path.name,
                        row_number=seq,  # order in the statement; a PDF has no row numbers
                    )
                )
            except ValidationError as exc:
                raise ParseError(path, f"transaction {seq}: {exc.errors()[0]['msg']}") from exc
        return transactions
