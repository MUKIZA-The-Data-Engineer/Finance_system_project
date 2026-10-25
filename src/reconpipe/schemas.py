"""Canonical transaction schemas: the contract between pipeline layers.

Two models, one per side of the transform layer:

RawTransaction
    What every parser emits, whatever the bank layout. Values are kept as the
    text that was on the page (dates like "15 Mar 2026", amounts like
    "1,250.00" or "-RWF 300"), because interpreting them is the transform
    layer's job, not the parser's. One row per statement line.

CleanTransaction
    What the transform layer emits and the validate layer checks. Typed:
    ISO dates, Decimal amounts (never float), one signed amount, a cleaned
    description next to the raw one, and a hash used for de-duplication.

Both are frozen. A layer never edits a row in place; it builds a new one.
Both carry ``source_file`` and ``row_number`` so any number in a report can be
walked back to the exact line of the original file.

Sign convention (bank's point of view, for the account holder):
    credit = money in  -> positive amount
    debit  = money out -> negative amount
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Shared field types
# --------------------------------------------------------------------------- #


def _empty_to_none(value: object) -> object:
    """Treat blank cells ("", "   ") as missing rather than as text."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _reject_float(value: object) -> object:
    """Refuse floats so binary rounding error can never enter a money field.

    0.1 + 0.2 != 0.3 in float. Money must arrive as str, int or Decimal.
    """
    if isinstance(value, float):
        raise ValueError("money must be passed as str, int or Decimal, never float")
    return value


OptionalText = Annotated[str | None, BeforeValidator(_empty_to_none)]

Money = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(max_digits=18, decimal_places=2),
]
NonNegativeMoney = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0, max_digits=18, decimal_places=2),
]

SourceFile = Annotated[str, Field(min_length=1, description="File name under data/raw/.")]
RowNumber = Annotated[
    int, Field(ge=1, description="1-based line number of the row in its source file.")
]
AccountId = Annotated[str, Field(min_length=1, description="Bank account the row belongs to.")]


# --------------------------------------------------------------------------- #
# Raw layer
# --------------------------------------------------------------------------- #


class RawTransaction(BaseModel):
    """One statement line exactly as a parser read it. Still dirty."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    date: str = Field(min_length=1, description='Date text as printed, e.g. "15 Mar 2026".')
    description: str = Field(min_length=1, description="Narrative text, wrapped lines joined.")
    ref_id: OptionalText = Field(default=None, description="Bank reference / cheque number.")

    # A layout has either separate debit/credit columns or one signed column.
    debit: OptionalText = Field(default=None, description="Money-out text, if a debit column.")
    credit: OptionalText = Field(default=None, description="Money-in text, if a credit column.")
    amount: OptionalText = Field(default=None, description="Signed amount text, if one column.")
    balance: OptionalText = Field(default=None, description="Running balance text, if printed.")

    account_id: AccountId
    source_file: SourceFile
    row_number: RowNumber


# --------------------------------------------------------------------------- #
# Clean layer
# --------------------------------------------------------------------------- #


class CleanTransaction(BaseModel):
    """One statement line after standardisation. Typed and comparable across banks."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    date: dt.date
    description_raw: str = Field(min_length=1, description="Original text, kept for fuzzy match.")
    description_clean: str = Field(
        min_length=1, description="Casefolded, whitespace-collapsed, reference noise stripped."
    )
    ref_id: OptionalText = None

    # The original columns are kept next to the signed amount for traceability.
    debit: NonNegativeMoney | None = None
    credit: NonNegativeMoney | None = None
    amount: Money = Field(description="Signed: credit positive, debit negative.")
    balance: Money | None = None

    account_id: AccountId
    source_file: SourceFile
    row_number: RowNumber
    txn_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of date + amount + description_clean + account_id.",
    )

    @model_validator(mode="after")
    def _amount_matches_columns(self) -> CleanTransaction:
        """If the source had debit/credit columns, the signed amount must agree with them."""
        if self.debit is None and self.credit is None:
            return self
        expected = (self.credit or Decimal("0")) - (self.debit or Decimal("0"))
        if self.amount != expected:
            raise ValueError(f"amount {self.amount} does not equal credit - debit ({expected})")
        return self
