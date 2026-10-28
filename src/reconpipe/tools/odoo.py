"""Tool 4 (Odoo preparation): any file -> an Odoo-ready import workbook, validated.

    reconpipe odoo-export statement.pdf
    reconpipe odoo-export --template          # empty templates for reuse

The file is standardised first (so any PDF, Excel or CSV works), then shaped for
one of two Odoo imports, chosen in config/odoo.toml:

bank_statement_lines
    Accounting > Bank > Import: one line per transaction
    (date, payment_ref, partner_id, ref, amount, journal_id).
journal_entries
    Accounting > Journal Entries > Import: each transaction is one balanced
    entry of two lines, the bank account against a suspense account to be
    reclassified in Odoo (Odoo's one2many import layout: the entry's own
    fields on its first line only).

Every line is validated before it is written (date, label, non-zero amount,
journal, partner), and the workbook lists every problem next to the import
sheet. Lines with errors are left out of the import sheet.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from reconpipe.tools.excel_out import Sheet, write_workbook
from reconpipe.tools.standardizer import (
    ISSUE_COLUMNS,
    Issue,
    StandardizeOutput,
    StandardRow,
)

DEFAULT_CONFIG = Path("config/odoo.toml")
TARGETS = ("bank_statement_lines", "journal_entries")
STATEMENT_COLUMNS = ["date", "payment_ref", "partner_id", "ref", "amount", "journal_id"]
JOURNAL_COLUMNS = [
    "id", "date", "journal_id", "ref", "line_ids/account_id", "line_ids/name",
    "line_ids/partner_id", "line_ids/debit", "line_ids/credit",
]  # fmt: skip
EXAMPLE_MARK = "EXAMPLE"


@dataclass
class OdooConfig:
    target: str = "bank_statement_lines"
    journal: str = "EXAMPLE-BNK1"
    bank_account_code: str = "EXAMPLE-1000"
    suspense_account_code: str = "EXAMPLE-499999"
    date_format: str = "%Y-%m-%d"
    partner_required: bool = False
    journals: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)

    def journal_for(self, account_id: str | None) -> str:
        return self.journals.get(account_id or "", self.journal)

    def bank_account_for(self, account_id: str | None) -> str:
        return self.accounts.get(account_id or "", self.bank_account_code)

    def examples_in_use(self) -> list[str]:
        used = [("journal", self.journal)]
        if self.target == "journal_entries":
            used += [
                ("bank_account_code", self.bank_account_code),
                ("suspense_account_code", self.suspense_account_code),
            ]
        return [name for name, value in used if EXAMPLE_MARK in value.upper()]


class OdooConfigError(ValueError):
    """config/odoo.toml cannot be used."""


def load_config(path: Path | None = None, **overrides) -> OdooConfig:
    """Read the settings file (if present) and apply command-line overrides."""
    data: dict = {}
    path = Path(path) if path else DEFAULT_CONFIG
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise OdooConfigError(f"{path}: {exc}") from exc
    odoo = data.get("odoo", {})
    known = set(OdooConfig.__dataclass_fields__) - {"journals", "accounts"}
    unknown = set(odoo) - known
    if unknown:
        raise OdooConfigError(f"{path}: unknown setting(s) {', '.join(sorted(unknown))}")
    cfg = OdooConfig(
        **odoo,
        journals={str(k): str(v) for k, v in data.get("journals", {}).items()},
        accounts={str(k): str(v) for k, v in data.get("accounts", {}).items()},
    )
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)
    if cfg.target not in TARGETS:
        raise OdooConfigError(f"target must be one of {', '.join(TARGETS)}, not {cfg.target!r}")
    return cfg


@dataclass
class OdooExport:
    config: OdooConfig
    columns: list[str]
    lines: list[list[object]]
    issues: list[Issue]
    exported_rows: int
    source: StandardizeOutput | None = None


def _external_id(row: StandardRow) -> str:
    stem = re.sub(r"\W+", "_", Path(row.source_file).stem).strip("_").lower()
    return f"reconpipe_{stem}_{row.row_number}"


def build_export(rows: list[StandardRow], cfg: OdooConfig) -> OdooExport:
    issues: list[Issue] = []
    lines: list[list[object]] = []
    columns = STATEMENT_COLUMNS if cfg.target == "bank_statement_lines" else JOURNAL_COLUMNS
    no_partner = 0
    exported = 0

    def problem(row: StandardRow, text: str, severity: str, action: str) -> None:
        issues.append(
            Issue(
                row.source_file,
                row.source_sheet,
                row.row_number,
                "",
                row.description,
                text,
                severity,
                action,
            )
        )

    for name in cfg.examples_in_use():
        issues.append(
            Issue(
                "config/odoo.toml",
                "",
                None,
                name,
                getattr(cfg, name),
                "example value still in use: set your real Odoo code before importing",
                "warning",
                "kept",
            )
        )

    for row in rows:
        errors = []
        if not row.description or row.description == "(no description)":
            errors.append("no label (description) for Odoo")
        if row.amount == 0:
            errors.append("amount is zero")
        journal = cfg.journal_for(row.account_id)
        if not journal:
            errors.append(f"no Odoo journal for account {row.account_id}")
        if not row.counterparty:
            if cfg.partner_required:
                errors.append("no partner")
            else:
                no_partner += 1
        if errors:
            for e in errors:
                problem(row, e, "error", "excluded")
            continue
        exported += 1
        day = row.date.strftime(cfg.date_format)
        if cfg.target == "bank_statement_lines":
            lines.append(
                [day, row.description, row.counterparty, row.reference, row.amount, journal]
            )
            continue
        value = abs(row.amount)
        bank, suspense = cfg.bank_account_for(row.account_id), cfg.suspense_account_code
        money_in = row.amount > 0
        lines.append(
            [
                _external_id(row),
                day,
                journal,
                row.reference,
                bank,
                row.description,
                row.counterparty,
                value if money_in else Decimal("0"),
                Decimal("0") if money_in else value,
            ]
        )
        lines.append(
            [
                None,
                None,
                None,
                None,
                suspense,
                row.description,
                row.counterparty,
                Decimal("0") if money_in else value,
                value if money_in else Decimal("0"),
            ]
        )

    if no_partner:
        issues.append(
            Issue(
                "",
                "",
                None,
                "partner_id",
                "",
                f"{no_partner} line(s) have no partner; Odoo imports them without one",
                "warning",
                "kept",
            )
        )
    return OdooExport(cfg, columns, lines, issues, exported)


def export_file(
    path: Path,
    cfg: OdooConfig,
    overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    mapper=None,
) -> OdooExport:
    from reconpipe.tools.standardizer import standardize_file

    standardized = standardize_file(path, overrides, date_order, mapper=mapper)
    export = build_export(standardized.rows, cfg)
    export.issues = standardized.issues + export.issues
    export.source = standardized
    return export


INSTRUCTIONS = {
    "bank_statement_lines": [
        "In Odoo: Accounting > Configuration > Journals > (your bank journal) > Import, or",
        "Accounting > Bank > (journal) > Import, then choose this file's 'odoo import' sheet.",
        "Columns are Odoo field names, so Odoo maps them automatically.",
        "partner_id is matched by partner name; unknown names can be created or left empty.",
    ],
    "journal_entries": [
        "In Odoo: Accounting > Accounting > Journal Entries > Import.",
        "Each entry is two lines: the bank account and the suspense account. Only the first",
        "line carries id, date, journal_id and ref (Odoo's layout for lines of one entry).",
        "Reclassify the suspense lines to their real accounts in Odoo after importing.",
    ],
}


def write_export(export: OdooExport, output: Path, csv_too: bool = False) -> list[Path]:
    cfg = export.config
    settings = [
        ["target", cfg.target], ["default journal", cfg.journal],
        ["bank account code", cfg.bank_account_code],
        ["suspense account code", cfg.suspense_account_code], ["date format", cfg.date_format],
        ["partner required", cfg.partner_required],
        *[[f"journal for {k}", v] for k, v in cfg.journals.items()],
        *[[f"account for {k}", v] for k, v in cfg.accounts.items()],
        ["lines exported", export.exported_rows],
    ]  # fmt: skip
    money = ("amount", "line_ids/debit", "line_ids/credit")
    sheets = [
        Sheet("odoo import", export.columns, export.lines, money),
        Sheet("issues", ISSUE_COLUMNS, [i.as_output() for i in export.issues]),
        Sheet("settings used", ["Setting", "Value"], settings),
        Sheet("how to import", ["Steps"], [[s] for s in INSTRUCTIONS[cfg.target]]),
    ]
    written = [write_workbook(output, sheets)]
    if csv_too:
        import csv

        csv_path = Path(output).with_suffix(".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(export.columns)
            writer.writerows([["" if v is None else v for v in line] for line in export.lines])
        written.append(csv_path)
    return written


def write_template(target: str, output: Path) -> Path:
    """An empty import workbook for target, with the steps to import it."""
    if target not in TARGETS:
        raise OdooConfigError(f"target must be one of {', '.join(TARGETS)}")
    columns = STATEMENT_COLUMNS if target == "bank_statement_lines" else JOURNAL_COLUMNS
    return write_workbook(
        output,
        [
            Sheet("odoo import", columns, []),
            Sheet("how to import", ["Steps"], [[s] for s in INSTRUCTIONS[target]]),
        ],
    )
