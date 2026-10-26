"""Business rules every clean row must pass before it reaches the warehouse.

The CleanTransaction Pydantic model is the structural contract (types, signs,
amount = credit - debit); a row that cannot be built at all fails in the
transform layer under the rule ``contract``. These rules add what a typed row
can still get wrong:

    date_not_in_future   a statement cannot contain tomorrow's transactions
    amount_non_zero      a zero-value line is not a transaction
    balance_continuity   previous balance + amount must equal this balance

Balance continuity is checked so that one bad line fails on its own instead of
also failing the line after it (see check_balance_continuity).
A row breaking several rules is quarantined once, with every rule listed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from reconpipe.schemas import CleanTransaction, RawTransaction

CONTRACT = "contract"
DATE_NOT_IN_FUTURE = "date_not_in_future"
AMOUNT_NON_ZERO = "amount_non_zero"
BALANCE_CONTINUITY = "balance_continuity"
RULES = (CONTRACT, DATE_NOT_IN_FUTURE, AMOUNT_NON_ZERO, BALANCE_CONTINUITY)


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str


@dataclass(frozen=True)
class QuarantinedRow:
    """A row that failed, with everything needed to find it in the original file."""

    row: CleanTransaction | RawTransaction
    violations: tuple[Violation, ...]

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(v.rule for v in self.violations)


@dataclass
class ValidationResult:
    passed: list[CleanTransaction] = field(default_factory=list)
    quarantined: list[QuarantinedRow] = field(default_factory=list)


def check_date_not_in_future(row: CleanTransaction, today: date) -> Violation | None:
    if row.date > today:
        return Violation(DATE_NOT_IN_FUTURE, f"{row.date} is after the run date {today}")
    return None


def check_amount_non_zero(row: CleanTransaction) -> Violation | None:
    if row.amount == 0:
        return Violation(AMOUNT_NON_ZERO, "amount is zero")
    return None


def check_balance_continuity(rows: list[CleanTransaction]) -> dict[int, Violation]:
    """Return violations keyed by id(row), checking each account's rows in statement order.

    One bad line must fail on its own. If line k's balance is misprinted, line
    k+1 still follows from what k's balance *should* have been; if line k's
    amount was misread, line k+1 still follows from k's *printed* balance. So
    a line passes when it follows from either.
    """
    statements: dict[tuple[str, str], list[CleanTransaction]] = defaultdict(list)
    for row in rows:
        statements[(row.source_file, row.account_id)].append(row)

    violations = {}
    for statement in statements.values():
        statement.sort(key=lambda r: r.row_number)
        anchors: set[Decimal] = set()  # balances the next line may follow from
        for row in statement:
            if row.balance is None:
                anchors = set()
                continue
            expected = {anchor + row.amount for anchor in anchors}
            if anchors and row.balance not in expected:
                shown = " or ".join(str(e) for e in sorted(expected))
                violations[id(row)] = Violation(
                    BALANCE_CONTINUITY,
                    f"balance {row.balance} does not follow from the previous line "
                    f"(expected {shown})",
                )
                anchors = {row.balance, *expected}
            else:
                anchors = {row.balance}
    return violations


def validate(rows: list[CleanTransaction], today: date) -> ValidationResult:
    """Split rows into passed and quarantined. Nothing is dropped."""
    balance_violations = check_balance_continuity(rows)
    result = ValidationResult()
    for row in rows:
        found = [
            v
            for v in (
                check_date_not_in_future(row, today),
                check_amount_non_zero(row),
                balance_violations.get(id(row)),
            )
            if v is not None
        ]
        if found:
            result.quarantined.append(QuarantinedRow(row, tuple(found)))
        else:
            result.passed.append(row)
    return result
