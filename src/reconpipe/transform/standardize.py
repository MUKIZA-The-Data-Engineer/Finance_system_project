"""Standardisation: RawTransaction text -> typed, comparable CleanTransaction.

After this step a row from any bank has the same shape, whatever its layout:

dates
    One ISO date. Each file's date format is inferred once: the format that
    reads the most of its rows wins, and rows it cannot read fail on their own.
    If two formats tie (a file where every day is 12 or under, so 05/01/2026
    could be 5 Jan or 1 May), the file is rejected as ambiguous, not guessed.
amounts
    Decimal, never float. Commas, currency codes, "CR"/"DR" suffixes and
    parenthesised negatives are understood; anything else is an error.
    Debit/credit columns collapse into one signed amount (money in positive),
    and the original columns are kept next to it.
descriptions
    description_raw keeps the printed text for fuzzy matching later.
    description_clean is casefolded with punctuation, the row's own reference
    and card/account numbers removed, and whitespace collapsed.
duplicates
    A SHA-256 of account + date + amount + clean description + balance. The
    balance is what tells a reprinted line (same balance) from two genuine
    identical purchases on one day (different balances). The first copy is
    kept; later copies are reported with the row they duplicate.

Rows that cannot be standardised are returned as failures with a reason, not
dropped, so the validate layer can quarantine them.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from reconpipe.schemas import CleanTransaction, RawTransaction

# Every date style a supported bank prints. %m/%d/%Y is listed so that a file
# which reads both ways is caught as ambiguous instead of silently taken as DD/MM.
DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y")

MONEY_PATTERN = re.compile(
    r"""^
    (?P<open>\()?                          # (1,234.00) means negative
    (?P<lead>[-+])?\s*                     # -RWF 300
    (?:(?P<ccy>RWF|FRW|USD|EUR)\s*)?
    (?P<sign>[-+])?\s*                     # RWF -300
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)
    (?:\.(?P<dec>\d{1,2}))?
    (?(open)\))
    (?:\s*(?P<side>CR|DR))?
    $""",
    re.VERBOSE | re.IGNORECASE,
)
CENT = Decimal("0.01")

NOISE_NUMBER = re.compile(r"\b(pos|acc|card)\s+\d+\b")
PUNCTUATION = re.compile(r"[^\w\s]")


class StandardizeError(ValueError):
    """A value could not be read unambiguously."""


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def infer_date_format(texts: list[str]) -> str:
    """Return the format that reads the most date texts in a file.

    Rows the winning format cannot read fail on their own later, so one typo
    does not sink the whole file. A tie for most rows read means the file
    genuinely reads two ways and is rejected rather than guessed.
    """
    reads = {}
    for fmt in DATE_FORMATS:
        count = 0
        for text in texts:
            try:
                datetime.strptime(text, fmt)
                count += 1
            except ValueError:
                pass
        reads[fmt] = count
    best = max(reads.values(), default=0)
    if best == 0:
        sample = texts[0] if texts else ""
        raise StandardizeError(f"no known date format reads these dates (e.g. {sample!r})")
    winners = [fmt for fmt, count in reads.items() if count == best]
    if len(winners) > 1:
        raise StandardizeError(f"ambiguous dates: rows read as both {' and '.join(winners)}")
    return winners[0]


def parse_money(text: str) -> Decimal:
    """'RWF -1,250.00' -> Decimal('-1250.00'); '6,045.10 DR' -> Decimal('-6045.10')."""
    match = MONEY_PATTERN.match(" ".join(text.split()))
    if match is None:
        raise StandardizeError(f"not an amount: {text!r}")
    try:
        value = Decimal(match["int"].replace(",", "") + "." + (match["dec"] or "0"))
    except InvalidOperation as exc:  # pragma: no cover - the regex admits only digits
        raise StandardizeError(f"not an amount: {text!r}") from exc
    if match["lead"] and match["sign"]:
        raise StandardizeError(f"two signs in {text!r}")
    negatives = [
        match["open"],
        "-" in (match["lead"], match["sign"]),
        (match["side"] or "").upper() == "DR",
    ]
    if sum(bool(n) for n in negatives) > 1:
        raise StandardizeError(f"contradictory signs in {text!r}")
    return (-value if any(negatives) else value).quantize(CENT)


def clean_description(text: str, ref_id: str | None) -> str:
    """'POS 2743 SIMBA SUPERMARKET KIGALI RW' -> 'pos simba supermarket kigali rw'."""
    out = text.casefold()
    if ref_id:
        out = out.replace(ref_id.casefold(), " ")
    out = PUNCTUATION.sub(" ", out)
    out = NOISE_NUMBER.sub(r"\1", out)
    return " ".join(out.split())


def transaction_hash(
    account_id: str, day: date, amount: Decimal, description_clean: str, balance: Decimal | None
) -> str:
    parts = [account_id, day.isoformat(), f"{amount:.2f}", description_clean]
    parts.append("" if balance is None else f"{balance:.2f}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def _signed_amount(raw: RawTransaction) -> tuple[Decimal | None, Decimal | None, Decimal]:
    """Return (debit, credit, signed amount); debit/credit stay None if not printed."""
    debit = abs(parse_money(raw.debit)) if raw.debit else None
    credit = abs(parse_money(raw.credit)) if raw.credit else None
    if raw.amount:
        amount = parse_money(raw.amount)
        if (debit or credit) and amount != (credit or 0) - (debit or 0):
            raise StandardizeError("signed amount disagrees with the debit/credit columns")
        return debit, credit, amount
    if debit is None and credit is None:
        raise StandardizeError("no amount: debit, credit and amount are all empty")
    return debit, credit, ((credit or Decimal("0")) - (debit or Decimal("0"))).quantize(CENT)


def standardize_row(raw: RawTransaction, date_format: str) -> CleanTransaction:
    try:
        day = datetime.strptime(raw.date, date_format).date()
    except ValueError as exc:
        raise StandardizeError(f"date {raw.date!r} does not match {date_format}") from exc
    debit, credit, amount = _signed_amount(raw)
    balance = parse_money(raw.balance) if raw.balance else None
    description_clean = clean_description(raw.description, raw.ref_id)
    if not description_clean:
        raise StandardizeError(f"description {raw.description!r} is empty after cleaning")
    try:
        return CleanTransaction(
            date=day,
            description_raw=raw.description,
            description_clean=description_clean,
            ref_id=raw.ref_id,
            debit=debit,
            credit=credit,
            amount=amount,
            balance=balance,
            account_id=raw.account_id,
            source_file=raw.source_file,
            row_number=raw.row_number,
            txn_hash=transaction_hash(raw.account_id, day, amount, description_clean, balance),
        )
    except ValidationError as exc:
        raise StandardizeError(exc.errors()[0]["msg"]) from exc


@dataclass(frozen=True)
class FailedRow:
    raw: RawTransaction
    reason: str


@dataclass(frozen=True)
class DuplicateRow:
    row: CleanTransaction
    duplicate_of: CleanTransaction


@dataclass
class StandardizeResult:
    clean: list[CleanTransaction] = field(default_factory=list)
    duplicates: list[DuplicateRow] = field(default_factory=list)
    failed: list[FailedRow] = field(default_factory=list)


def standardize(rows: list[RawTransaction]) -> StandardizeResult:
    """Standardise every row, then drop exact duplicates (first copy wins)."""
    result = StandardizeResult()

    by_file: dict[str, list[RawTransaction]] = defaultdict(list)
    for raw in rows:
        by_file[raw.source_file].append(raw)

    candidates: list[CleanTransaction] = []
    for file_rows in by_file.values():
        try:
            date_format = infer_date_format([r.date for r in file_rows])
        except StandardizeError as exc:
            result.failed.extend(FailedRow(r, str(exc)) for r in file_rows)
            continue
        for raw in file_rows:
            try:
                candidates.append(standardize_row(raw, date_format))
            except StandardizeError as exc:
                result.failed.append(FailedRow(raw, str(exc)))

    first_seen: dict[str, CleanTransaction] = {}
    for row in candidates:
        if row.txn_hash in first_seen:
            result.duplicates.append(DuplicateRow(row, first_seen[row.txn_hash]))
        else:
            first_seen[row.txn_hash] = row
            result.clean.append(row)
    return result
