"""Bank B (Akagera Trust Bank) PDF parser: unruled columns, one signed amount.

Unlike Bank A there are no ruled lines, so pdfplumber finds no table. The
layout is recovered from word positions instead:

    Value Date   Ref No.     Transaction Details                    Amount            Balance
                             BALANCE BROUGHT FORWARD                              7,325,500.00 CR
    05 Jan 2026  AKB347231   Outward transfer Kigali Fresh ...   RWF -702,650.00  6,622,850.00 CR
                             1100                                (wrapped details)
                             BALANCE CARRIED FORWARD                              ...          CR

- Date, reference and details are split by the x position where each header
  word starts.
- Amount and balance are read from the end of the line: "RWF <signed money>"
  then "<money> CR|DR". Details never contain that pattern, so the split is
  unambiguous even when long details run close to the amount column.
- A line with only details text continues the transaction above it.
- Each page's table ends at "BALANCE CARRIED FORWARD" or "CLOSING BALANCE";
  the brought-forward line and the page footer are not transactions.

Every printed transaction line is emitted, including the line this bank
repeats at the top of a page after a page break. Removing duplicates is the
transform layer's job, not the parser's.
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

HEADER_TEXT = "Value Date Ref No. Transaction Details Amount Balance"
ACCOUNT_PATTERN = re.compile(r"A/C No\.\s*(\S+)")
AMOUNT_AND_BALANCE = re.compile(
    r"^(?P<details>.*?)\s*(?P<amount>RWF\s+-?[\d,]+\.\d{2})\s+(?P<balance>[\d,]+\.\d{2}\s+(?:CR|DR))$"
)
MARKER_LINES = ("BALANCE BROUGHT FORWARD", "BALANCE CARRIED FORWARD", "CLOSING BALANCE")
TABLE_END = ("BALANCE CARRIED FORWARD", "CLOSING BALANCE")
LINE_TOLERANCE = 2.0  # words whose tops differ by less than this are on one line
# What pdfplumber/pdfminer raise for a missing, corrupt or non-PDF file.
UNREADABLE = (OSError, PdfminerException, PDFSyntaxError, ValueError, KeyError, TypeError)


def _lines(page: pdfplumber.page.Page) -> list[list[dict]]:
    """Group a page's words into visual lines, each sorted left to right."""
    lines: list[list[dict]] = []
    for word in sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) < LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def _header_index(lines: list[list[dict]]) -> int | None:
    return next((i for i, line in enumerate(lines) if _text(line) == HEADER_TEXT), None)


class BankBPdfParser(BaseStatementParser):
    name = "bank_b_pdf_unruled"

    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        try:
            with pdfplumber.open(path) as pdf:
                return bool(pdf.pages) and _header_index(_lines(pdf.pages[0])) is not None
        except UNREADABLE:
            return False

    def parse(self, path: Path) -> list[RawTransaction]:
        try:
            with pdfplumber.open(path) as pdf:
                pages = [_lines(page) for page in pdf.pages]
        except UNREADABLE as exc:
            raise ParseError(path, f"cannot open PDF: {exc}") from exc
        if not pages:
            raise ParseError(path, "PDF has no pages")

        match = ACCOUNT_PATTERN.search("\n".join(_text(line) for line in pages[0]))
        if match is None:
            raise ParseError(path, "no 'A/C No.' on the first page")
        account_id = match.group(1)

        records: list[dict[str, str]] = []
        for page_no, lines in enumerate(pages, start=1):
            header = _header_index(lines)
            if header is None:
                raise ParseError(path, f"page {page_no} has no column header")
            records.extend(self._page_records(path, page_no, lines, header, records))

        if not records:
            raise ParseError(path, "statement has no transactions")

        transactions = []
        for seq, values in enumerate(records, start=1):
            try:
                transactions.append(
                    RawTransaction(
                        date=values["date"],
                        ref_id=values["ref_id"] or None,
                        description=values["description"],
                        amount=values["amount"],
                        balance=values["balance"],
                        account_id=account_id,
                        source_file=path.name,
                        row_number=seq,  # printed order in the statement
                    )
                )
            except ValidationError as exc:
                raise ParseError(path, f"transaction {seq}: {exc.errors()[0]['msg']}") from exc
        return transactions

    def _page_records(
        self,
        path: Path,
        page_no: int,
        lines: list[list[dict]],
        header: int,
        earlier: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        x = {w["text"]: w["x0"] for w in lines[header]}
        ref_x, details_x = x["Ref"], x["Transaction"]

        records: list[dict[str, str]] = []
        for line in lines[header + 1 :]:
            date = _text([w for w in line if w["x1"] <= ref_x])
            ref = _text([w for w in line if ref_x <= w["x0"] < details_x])
            rest = _text([w for w in line if w["x0"] >= details_x])
            if not date and rest.startswith(TABLE_END):
                return records
            if not date and rest.startswith(MARKER_LINES):
                continue

            match = AMOUNT_AND_BALANCE.match(rest)
            if date and match:
                records.append(
                    {
                        "date": date,
                        "ref_id": ref,
                        "description": match["details"],
                        "amount": match["amount"],
                        "balance": match["balance"],
                    }
                )
            elif not date and not ref and not match and rest:
                previous = records or earlier
                if not previous:
                    raise ParseError(path, f"page {page_no}: continuation before any transaction")
                previous[-1]["description"] += " " + rest
            else:
                raise ParseError(path, f"page {page_no}: unrecognised line {_text(line)!r}")
        raise ParseError(path, f"page {page_no}: no carried-forward or closing balance line")
