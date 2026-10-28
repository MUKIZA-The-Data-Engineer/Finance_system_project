"""The whole workflow in one step: messy files in, clean + Odoo-ready + reconciled out.

    reconpipe process statement.pdf               one file (or a folder, e.g. inbox/)
    reconpipe workflow --bank stmt.pdf --books ledger.xlsx

process
    Any PDF, Excel or CSV -> output/<name>/: <name>_clean.xlsx (for a PDF,
    <name>_extracted.xlsx, which also keeps the raw extracted rows) and
    <name>_odoo.xlsx, the Odoo import.
workflow
    Statement file(s) and book file(s) -> output/workflow_<bank>_vs_<books>/:
    every statement processed as above, the books cleaned, then the
    reconciliation workbook and a short summary.txt.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from reconpipe.tools.odoo import OdooConfig, OdooExport, build_export, write_export
from reconpipe.tools.reader import SUPPORTED_SUFFIXES, UnreadableFileError
from reconpipe.tools.reconcile import (
    DEFAULT_TOLERANCES,
    ReconcileResult,
    Tolerances,
    reconcile_rows,
    write_report,
)
from reconpipe.tools.standardizer import (
    MappingError,
    StandardizeOutput,
    standardize_file,
    write_standardized,
)

PDF = ".pdf"
# in a folder, .txt files are more likely notes than tab-separated exports
FOLDER_SUFFIXES = SUPPORTED_SUFFIXES - {".txt"}


@dataclass
class ProcessResult:
    source: Path
    standardized: StandardizeOutput | None
    export: OdooExport | None
    files: list[Path] = field(default_factory=list)
    problem: str | None = None

    def describe(self) -> str:
        if self.standardized is None:
            return f"{self.source.name}: raw rows only ({self.problem})"
        s = self.standardized.summary
        text = (
            f"{self.source.name}: {s['clean_rows']} clean rows, {s['rows_excluded']} excluded, "
            f"{s['warnings']} warnings"
        )
        if self.export is not None:
            text += f"; {self.export.exported_rows} line(s) ready for Odoo"
        return text


def find_files(paths: list[Path]) -> list[Path]:
    """Files as given, and every supported file inside any folder given."""
    found: list[Path] = []
    for path in map(Path, paths):
        if path.is_dir():
            found += sorted(
                p
                for p in path.iterdir()
                if p.suffix.lower() in FOLDER_SUFFIXES and not p.name.startswith(("~$", "."))
            )
        else:
            found.append(path)
    if not found:
        raise UnreadableFileError(
            f"no PDF, Excel or CSV files in {', '.join(str(p) for p in paths)}"
        )
    return found


def process_file(
    path: Path,
    out_dir: Path,
    cfg: OdooConfig | None,
    overrides: dict[str, str] | None = None,
    date_order: str = "auto",
    mapper=None,
    csv_too: bool = False,
    today: dt.date | None = None,
) -> ProcessResult:
    """Clean one file and, when cfg is given, shape it for Odoo."""
    from reconpipe.tools.pdf_to_excel import pdf_to_excel

    path, out_dir = Path(path), Path(out_dir)
    if path.suffix.lower() == PDF:
        extracted = pdf_to_excel(
            path, out_dir / f"{path.stem}_extracted.xlsx", overrides, date_order, today, mapper
        )
        result = ProcessResult(
            path, extracted.standardized, None, [extracted.output], extracted.mapping_problem
        )
    else:
        standardized = standardize_file(path, overrides, date_order, today, mapper)
        clean = write_standardized(standardized, out_dir / f"{path.stem}_clean.xlsx")
        result = ProcessResult(path, standardized, None, [clean])
    if cfg is not None and result.standardized is not None:
        export = build_export(result.standardized.rows, cfg)
        export.issues = result.standardized.issues + export.issues
        export.source = result.standardized
        result.export = export
        result.files += write_export(export, out_dir / f"{path.stem}_odoo.xlsx", csv_too)
    return result


@dataclass
class WorkflowResult:
    folder: Path
    bank: list[ProcessResult]
    books: list[ProcessResult]
    reconciliation: ReconcileResult
    report: Path
    summary_file: Path

    @property
    def files(self) -> list[Path]:
        made = [f for r in self.bank + self.books for f in r.files]
        return [*made, self.report, self.summary_file]

    def summary_lines(self) -> list[str]:
        r = self.reconciliation
        s, by_status = r.summary, r.summary["items_by_status"]
        return [
            *(p.describe() for p in self.bank + self.books),
            "",
            f"{s['matched_a_lines']} of {s['a_lines']} {r.a_label} lines matched "
            f"({s['match_rate']:.1%})",
            f"{by_status['amount_mismatch']} amount mismatches, "
            f"{by_status['missing_in_b']} missing in {r.b_label}, "
            f"{by_status['missing_in_a']} missing in {r.a_label}, "
            f"{by_status['duplicate_suspected']} suspected duplicates",
            f"value matched {s['value_matched']:,.2f}; "
            f"value in exception {s['value_in_exception']:,.2f}",
        ]


def default_folder(bank_files: list[Path], books_files: list[Path]) -> Path:
    return Path("output") / f"workflow_{Path(bank_files[0]).stem}_vs_{Path(books_files[0]).stem}"


def run_workflow(
    bank_files: list[Path],
    books_files: list[Path],
    out_dir: Path | None = None,
    cfg: OdooConfig | None = None,
    tolerances: Tolerances = DEFAULT_TOLERANCES,
    bank_label: str = "bank",
    books_label: str = "books",
    date_order: str = "auto",
    mapper=None,
    today: dt.date | None = None,
) -> WorkflowResult:
    """Clean every file, prepare the statements for Odoo, reconcile, report."""
    folder = Path(out_dir or default_folder(bank_files, books_files))
    bank = [
        process_file(f, folder / "bank", cfg, None, date_order, mapper, today=today)
        for f in bank_files
    ]
    books = [
        process_file(f, folder / "books", None, None, date_order, mapper, today=today)
        for f in books_files
    ]
    unreadable = [p for p in bank + books if p.standardized is None]
    if unreadable:
        raise MappingError("; ".join(f"{p.source.name}: {p.problem}" for p in unreadable))
    result = reconcile_rows(
        [r for p in bank for r in p.standardized.rows],
        [r for p in books for r in p.standardized.rows],
        tolerances,
        bank_label,
        books_label,
    )
    result.a_inputs = [p.standardized for p in bank]
    result.b_inputs = [p.standardized for p in books]
    report = write_report(result, folder / "reconciliation.xlsx")
    summary_file = folder / "summary.txt"
    outcome = WorkflowResult(folder, bank, books, result, report, summary_file)
    summary_file.write_text("\n".join(outcome.summary_lines()) + "\n", encoding="utf-8")
    return outcome
