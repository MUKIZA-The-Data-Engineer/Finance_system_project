"""Tool 2: any messy PDF, Excel or CSV -> one clean, standard table plus a list of problems.

    result = standardize_file(Path("export.xlsx"))
    result.rows      # StandardRow: date, description, reference, debit, credit,
                     # amount (signed), balance, account_id, counterparty, ...
    result.issues    # every problem found, with its file, sheet, row and column

What it does, in the pipeline's own terms:

- columns are mapped to standard fields (reconpipe.tools.columns);
- dates get one format per column, inferred from all its values; when a
  column reads equally well day-first and month-first it is rejected, not
  guessed (pass date_order to decide);
- amounts become Decimal: commas, spaces, currency symbols, CR/DR, brackets
  and European "1 250,50" are understood; debit/credit collapse into one
  signed amount (money in positive);
- wrapped description lines are joined to the line above; total and
  opening/closing balance lines are dropped;
- an exact reprint of a line is removed; two lines that are identical except
  for their own ID (a possible double posting) are both kept and flagged;
- the pipeline's validation rules run unchanged (no zero amounts, no future
  dates, the running balance must add up): a failing row is excluded and
  listed with the rule it broke.

Nothing is silently dropped: every excluded, skipped or merged row is an issue.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from reconpipe.tools.columns import FIELDS, IGNORE, Mapping, map_columns
from reconpipe.tools.reader import ReadResult, Table, cell_to_text, normalise, read_tables
from reconpipe.transform.standardize import (
    StandardizeError,
    clean_description,
    infer_date_format,
    parse_money,
    transaction_hash,
)
from reconpipe.validate import validate

DAY_FIRST = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y")
MONTH_FIRST = ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y")
UNAMBIGUOUS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d %b %Y", "%d-%b-%Y",
    "%d %B %Y", "%d-%b-%y", "%b %d, %Y", "%B %d, %Y", "%d %b %y",
)  # fmt: skip
DATE_ORDERS = {
    "auto": DAY_FIRST + MONTH_FIRST + UNAMBIGUOUS,
    "day-first": DAY_FIRST + UNAMBIGUOUS,
    "month-first": MONTH_FIRST + UNAMBIGUOUS,
}

SUMMARY_LINE = re.compile(
    r"^(total|totals|sub ?total|grand total|sous total|opening balance|closing balance|"
    r"balance (brought|carried) forward|balance b f|balance c f|brought forward|carried forward|"
    r"solde (initial|final|d ouverture|de cloture|precedent|reporte|au)|report|a reporter|"
    r"end of statement)\b"
)
CURRENCY_TOKEN = re.compile(r"(?<![A-Za-z])(?:USD|EUR|GBP|KES|UGX|TZS|\$|€|£)(?![A-Za-z])")
EUROPEAN = re.compile(
    r"^(\(?[-+]?)\s*(\d{1,3}(?:[ .  ]\d{3})*|\d+),(\d{1,2})(\)?)(\s*(?:CR|DR))?$", re.I
)
CENT = Decimal("0.01")

OUTPUT_COLUMNS = [
    "Date", "Description", "Reference", "Debit", "Credit", "Amount", "Balance", "Account",
    "Counterparty", "Currency", "Record ID", "Source file", "Source sheet", "Source row",
]  # fmt: skip


@dataclass
class StandardRow:
    """One clean transaction. Field names match CleanTransaction where they overlap,
    so the pipeline's validation rules run on these rows unchanged."""

    date: dt.date
    description: str
    amount: Decimal
    reference: str | None = None
    debit: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal | None = None
    account_id: str | None = None
    counterparty: str | None = None
    currency: str | None = None
    record_id: str | None = None
    source_file: str = ""
    source_sheet: str = ""
    row_number: int = 0

    @property
    def description_clean(self) -> str:
        return clean_description(self.description, self.reference) or self.description.casefold()

    def as_output(self) -> list[object]:
        return [
            self.date,
            self.description,
            self.reference,
            self.debit,
            self.credit,
            self.amount,
            self.balance,
            self.account_id,
            self.counterparty,
            self.currency,
            self.record_id,
            self.source_file,
            self.source_sheet,
            self.row_number,
        ]


@dataclass
class Issue:
    source_file: str
    sheet: str
    row: int | None
    column: str
    value: str
    problem: str
    severity: str  # error (row excluded) | warning (row kept) | info
    action: str  # excluded | kept | merged | skipped | removed

    def as_output(self) -> list[object]:
        return [
            self.severity,
            self.problem,
            self.action,
            self.source_file,
            self.sheet,
            self.row,
            self.column,
            self.value,
        ]


ISSUE_COLUMNS = ["Severity", "Problem", "Action", "Source file", "Sheet", "Row", "Column", "Value"]


@dataclass
class StandardizeOutput:
    source_file: str
    read: ReadResult
    rows: list[StandardRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    mappings: dict[str, Mapping] = field(default_factory=dict)  # sheet -> mapping

    @property
    def summary(self) -> dict:
        severities = Counter(i.severity for i in self.issues)
        actions = Counter(i.action for i in self.issues)
        return {
            "source_file": self.source_file,
            "file_type": self.read.kind,
            "tables": len(self.read.tables),
            "rows_read": sum(len(t.rows) for t in self.read.tables),
            "clean_rows": len(self.rows),
            "rows_excluded": actions["excluded"],
            "rows_merged_into_previous": actions["merged"],
            "summary_lines_skipped": actions["skipped"],
            "duplicates_removed": actions["removed"],
            "errors": severities["error"],
            "warnings": severities["warning"],
        }


class MappingError(ValueError):
    """The date or amount columns could not be found."""


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def parse_amount(value: object) -> Decimal | None:
    """A cell -> Decimal (2 places), None for blank. Raises StandardizeError if unreadable."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise StandardizeError(f"not an amount: {value!r}")
    if isinstance(value, int | Decimal):
        return Decimal(value).quantize(CENT)
    if isinstance(value, float):
        return Decimal(str(value)).quantize(CENT)
    text = " ".join(str(value).replace(" ", " ").replace(" ", " ").split())
    if not text or text in ("-", "--"):
        return None
    text = CURRENCY_TOKEN.sub("", text).strip()
    try:
        return parse_money(text)
    except StandardizeError:
        pass
    match = EUROPEAN.match(text.replace(" ,", ","))
    if match:
        lead, whole, cents, close, side = match.groups()
        digits = re.sub(r"[ .  ]", "", whole)
        return parse_money(f"{lead}{digits}.{cents}{close}{side or ''}")
    raise StandardizeError(f"not an amount: {value!r}")


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: object) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return cell_to_text(value).strip()


def _value_kind(value: object) -> str:
    if _is_blank(value):
        return "blank"
    if isinstance(value, dt.date | dt.datetime):
        return "date"
    if isinstance(value, int | float | Decimal):
        return "number"
    try:
        parse_amount(value)
        return "number"
    except (StandardizeError, InvalidOperation):
        return "text"


# --------------------------------------------------------------------------- #
# One table
# --------------------------------------------------------------------------- #


def _date_reader(values: list[object], date_order: str):
    """Return (parse(value) -> date | raises, file-level problem or None)."""
    texts = sorted({str(v).strip() for v in values if isinstance(v, str) and v.strip()})
    fmt, problem = None, None
    if texts:
        try:
            fmt = infer_date_format(texts, DATE_ORDERS[date_order])
        except StandardizeError as exc:
            problem = str(exc)
            if "ambiguous" in problem:
                problem += " (choose day-first or month-first)"

    def parse(value: object) -> dt.date:
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        if problem:
            raise StandardizeError(problem)
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except (TypeError, ValueError) as exc:
            raise StandardizeError(f"date {value!r} does not match {fmt}") from exc

    return parse, problem


def _standardize_table(
    table: Table, mapping: Mapping, date_order: str, out: StandardizeOutput
) -> list[StandardRow]:
    cols = {f: table.header.index(src) for f, src in mapping.by_field.items()}

    def get(values: list[object], name: str) -> object:
        i = cols.get(name)
        return values[i] if i is not None and i < len(values) else None

    def issue(row: int | None, column: str, value: object, problem: str, sev: str, act: str):
        out.issues.append(
            Issue(
                table.source_file, table.sheet, row, column, _text(value) or "", problem, sev, act
            )
        )

    # Column-level: mixed data types in a mapped column (beyond blanks).
    for name, i in cols.items():
        kinds = Counter(_value_kind(r[i]) for r in table.rows if i < len(r)) - Counter(["blank"])
        kinds.pop("blank", None)
        if name in ("debit", "credit", "amount", "balance") and kinds.get("text"):
            issue(
                None,
                table.header[i],
                "",
                f"column has text where amounts are expected "
                f"({kinds['text']} of {sum(kinds.values())} values)",
                "warning",
                "kept",
            )
        if len(kinds) > 1 and name not in ("description", "reference", "counterparty"):
            mix = ", ".join(f"{n} {k}" for k, n in kinds.most_common())
            issue(None, table.header[i], "", f"inconsistent data types ({mix})", "warning", "kept")

    parse_date, date_problem = _date_reader([get(r, "date") for r in table.rows], date_order)
    if date_problem:
        issue(None, mapping.by_field.get("date", ""), "", date_problem, "error", "excluded")

    rows: list[StandardRow] = []
    for values, number in zip(table.rows, table.row_numbers, strict=True):
        raw_date, desc = get(values, "date"), _text(get(values, "description"))
        money = {f: get(values, f) for f in ("debit", "credit", "amount")}
        has_money = any(not _is_blank(v) for v in money.values())
        first_text = normalise(next((v for v in values if isinstance(v, str) and v.strip()), ""))

        date_cell = normalise(raw_date) if isinstance(raw_date, str) else ""
        if (_is_blank(raw_date) or SUMMARY_LINE.match(date_cell)) and SUMMARY_LINE.match(
            first_text
        ):
            issue(
                number, "", first_text, "total / balance line, not a transaction", "info", "skipped"
            )
            continue
        if _is_blank(raw_date) and not has_money:
            if desc and rows:
                rows[-1].description = f"{rows[-1].description} {desc}"
                issue(
                    number,
                    mapping.by_field.get("description", ""),
                    desc,
                    "wrapped description joined to the line above",
                    "info",
                    "merged",
                )
            continue
        if desc and SUMMARY_LINE.match(normalise(desc)) and not has_money:
            issue(number, "", desc, "balance line without an amount", "info", "skipped")
            continue

        try:
            if _is_blank(raw_date):
                raise StandardizeError("missing date")
            day = parse_date(raw_date)
            debit = parse_amount(money["debit"])
            credit = parse_amount(money["credit"])
            amount = parse_amount(money["amount"])
        except StandardizeError as exc:
            issue(number, "", raw_date, str(exc), "error", "excluded")
            continue
        debit = abs(debit) if debit is not None else None
        credit = abs(credit) if credit is not None else None
        if debit is not None or credit is not None:
            from_columns = (credit or Decimal("0")) - (debit or Decimal("0"))
            if amount is not None and amount != from_columns:
                issue(
                    number,
                    mapping.by_field.get("amount", ""),
                    amount,
                    f"amount disagrees with debit/credit ({from_columns}); debit/credit used",
                    "warning",
                    "kept",
                )
            amount = from_columns.quantize(CENT)
        if amount is None:
            issue(number, "", "", "missing amount", "error", "excluded")
            continue
        try:
            balance = parse_amount(get(values, "balance"))
        except StandardizeError as exc:
            issue(
                number,
                mapping.by_field.get("balance", ""),
                get(values, "balance"),
                f"{exc}; balance left empty",
                "warning",
                "kept",
            )
            balance = None
        if not desc:
            issue(
                number,
                mapping.by_field.get("description", ""),
                "",
                "blank description",
                "warning",
                "kept",
            )

        rows.append(
            StandardRow(
                date=day,
                description=desc or "(no description)",
                amount=amount,
                reference=_text(get(values, "reference")),
                debit=debit,
                credit=credit,
                balance=balance,
                account_id=_text(get(values, "account")) or table.account_id,
                counterparty=_text(get(values, "counterparty")),
                currency=_text(get(values, "currency")),
                record_id=_text(get(values, "record_id")),
                source_file=table.source_file,
                source_sheet=table.sheet,
                row_number=number,
            )
        )
    return rows


def _deduplicate(rows: list[StandardRow], out: StandardizeOutput) -> list[StandardRow]:
    """Remove exact reprints; flag (keep) lines identical except for their own ID."""
    seen: dict[str, list[StandardRow]] = defaultdict(list)
    kept = []
    for row in rows:
        key = transaction_hash(
            row.account_id or "", row.date, row.amount, row.description_clean, row.balance
        )
        earlier = seen[key]
        same = next((e for e in earlier if e.record_id == row.record_id), None)
        if same is not None:
            problem, severity, action = (
                f"duplicate of row {same.row_number} (reprinted line)",
                "info",
                "removed",
            )
        elif earlier:
            first = earlier[0]
            problem, severity, action = (
                f"possible double posting: same as {first.record_id} (row {first.row_number})",
                "warning",
                "kept",
            )
        else:
            problem = None
        if problem:
            out.issues.append(
                Issue(
                    row.source_file,
                    row.source_sheet,
                    row.row_number,
                    "",
                    row.record_id or "",
                    problem,
                    severity,
                    action,
                )  # fmt: skip
            )
        if problem is None or action == "kept":
            earlier.append(row)
            kept.append(row)
    return kept


def standardize_tables(
    read: ReadResult,
    overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    today: dt.date | None = None,
    mapper=None,
) -> StandardizeOutput:
    """Standardise every table of an already-read file.

    mapper(table, mapping) -> Mapping may improve a mapping that is missing
    required columns (the AI assistant plugs in here).
    """
    out = StandardizeOutput(read.source_file, read)
    rows: list[StandardRow] = []
    failures: list[str] = []
    for table in read.tables:
        mapping = map_columns(table.header, overrides)
        if mapping.missing and mapper is not None:
            mapping = mapper(table, mapping)
        out.mappings[table.sheet] = mapping
        if mapping.missing:
            failures.append(
                f"{read.source_file} [{table.sheet}]: could not find {', '.join(mapping.missing)} "
                f"among columns {table.header}. Tell it which column is which, e.g. "
                f'--map "{table.header[0]}=date"'
            )
            continue
        rows.extend(_standardize_table(table, mapping, date_order, out))
    if len(failures) == len(read.tables):
        raise MappingError(failures[0])
    for failure in failures:  # other tables in the file were usable: note and carry on
        out.issues.append(
            Issue(
                read.source_file,
                "",
                None,
                "",
                "",
                f"table skipped: {failure}",
                "warning",
                "skipped",
            )
        )

    rows = _deduplicate(rows, out)
    result = validate(rows, today or dt.date.today())
    for q in result.quarantined:
        for v in q.violations:
            out.issues.append(
                Issue(
                    q.row.source_file,
                    q.row.source_sheet,
                    q.row.row_number,
                    "",
                    str(q.row.amount),
                    f"{v.rule}: {v.message}",
                    "error",
                    "excluded",
                )
            )
    out.rows = result.passed
    return out


def standardize_file(
    path: Path,
    overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    today: dt.date | None = None,
    mapper=None,
) -> StandardizeOutput:
    read = read_tables(Path(path), hints=list(overrides or {}))
    return standardize_tables(read, overrides, date_order, today, mapper)


# --------------------------------------------------------------------------- #
# Excel output
# --------------------------------------------------------------------------- #


def output_sheets(result: StandardizeOutput):
    from reconpipe.tools.excel_out import Sheet

    mapping_rows = [
        [sheet, c.source, c.field if c.field != IGNORE else "(not used)", c.how]
        for sheet, m in result.mappings.items()
        for c in m.choices
    ]
    by_problem = defaultdict(int)
    for i in result.issues:
        by_problem[(i.severity, i.action, i.problem.split(":")[0][:60])] += 1
    summary = [[k.replace("_", " "), v] for k, v in result.summary.items()]
    summary += [[f"{s} / {a}: {p}", n] for (s, a, p), n in sorted(by_problem.items())]
    return [
        Sheet(
            "clean",
            OUTPUT_COLUMNS,
            [r.as_output() for r in result.rows],
            ("Debit", "Credit", "Amount", "Balance"),
        ),
        Sheet("issues", ISSUE_COLUMNS, [i.as_output() for i in result.issues]),
        Sheet("column mapping", ["Sheet", "Source column", "Standard field", "How"], mapping_rows),
        Sheet("summary", ["Item", "Value"], summary),
    ]


def write_standardized(result: StandardizeOutput, output: Path) -> Path:
    from reconpipe.tools.excel_out import write_workbook

    return write_workbook(output, output_sheets(result))


__all__ = [
    "FIELDS",
    "OUTPUT_COLUMNS",
    "Issue",
    "MappingError",
    "StandardRow",
    "StandardizeOutput",
    "parse_amount",
    "standardize_file",
    "standardize_tables",
    "write_standardized",
]
