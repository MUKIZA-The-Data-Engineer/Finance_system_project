"""ReconPipe toolkit app: upload a PDF, Excel or CSV file and download the results.

    streamlit run app/toolkit.py

Three tabs, each the same engine as the command line:
  Process a file     upload -> clean Excel + Odoo import    (reconpipe process)
  Reconcile          bank + books uploads -> full workflow  (reconpipe workflow)
  AI assistant       upload -> prompt filled in; Claude answers if a key is set (reconpipe ai)

Uploads are written to a temporary folder for the engine to read; results are
also saved under output/app/ so they are on disk after the page is closed.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from reconpipe.tools import ai
from reconpipe.tools.odoo import TARGETS, OdooConfigError, load_config
from reconpipe.tools.reader import SUPPORTED_SUFFIXES, UnreadableFileError
from reconpipe.tools.reconcile import Tolerances
from reconpipe.tools.standardizer import ISSUE_COLUMNS, MappingError
from reconpipe.tools.workflow import process_file, run_workflow

OUT = Path(os.environ.get("RECONPIPE_APP_OUTPUT", "output/app"))
TYPES = sorted(s.lstrip(".") for s in SUPPORTED_SUFFIXES)
PROBLEMS = (UnreadableFileError, MappingError, OdooConfigError, ai.AIError)


def save_uploads(uploads: list[tuple[str, bytes]], folder: Path) -> list[Path]:
    """Write (file name, content) pairs to folder; returns the paths."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in uploads:
        path = folder / Path(name).name  # never a path from the browser
        path.write_bytes(data)
        paths.append(path)
    return paths


def zip_files(files: list[Path], root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(root))
    return buffer.getvalue()


def issues_frame(issues) -> pd.DataFrame:
    return pd.DataFrame([i.as_output() for i in issues], columns=ISSUE_COLUMNS)


def mapper_for(use_ai: bool, headers_only: bool):
    return ai.make_mapper(use_ai, headers_only=headers_only)


def downloads(files: list[Path], root: Path, key: str) -> None:
    for f in files:
        st.download_button(f"Download {f.name}", f.read_bytes(), f.name, key=f"{key}-{f}")
    if len(files) > 1:
        st.download_button(
            "Download everything (.zip)", zip_files(files, root), "reconpipe.zip", key=f"{key}-zip"
        )


def sidebar() -> dict:
    st.sidebar.header("Settings")
    target = st.sidebar.selectbox("Odoo import", TARGETS, index=0)
    journal = st.sidebar.text_input("Odoo journal code (blank: config/odoo.toml)")
    date_order = st.sidebar.selectbox("Dates like 05/01/2026", ["auto", "day-first", "month-first"])
    use_ai = st.sidebar.checkbox("Ask Claude when columns are not recognised")
    headers_only = st.sidebar.checkbox("Share column names only with Claude", value=True)
    return {
        "target": target,
        "journal": journal or None,
        "date_order": date_order,
        "use_ai": use_ai,
        "headers_only": headers_only,
    }


def process_tab(settings: dict) -> None:
    st.write("Upload any bank statement or ledger. You get a clean Excel file and an Odoo import.")
    uploads = st.file_uploader("Files", type=TYPES, accept_multiple_files=True, key="process")
    if not uploads or not st.button("Process", type="primary"):
        return
    cfg = load_config(target=settings["target"], journal=settings["journal"])
    work = Path(tempfile.mkdtemp(prefix="reconpipe_"))
    for path in save_uploads([(u.name, u.getvalue()) for u in uploads], work):
        try:
            result = process_file(
                path,
                OUT / path.stem,
                cfg,
                date_order=settings["date_order"],
                mapper=mapper_for(settings["use_ai"], settings["headers_only"]),
            )
        except PROBLEMS as exc:
            st.error(f"{path.name}: {exc}")
            continue
        st.subheader(path.name)
        st.write(result.describe())
        if result.standardized:
            s = result.standardized.summary
            cols = st.columns(3)
            cols[0].metric("Clean rows", s["clean_rows"])
            cols[1].metric("Excluded", s["rows_excluded"])
            cols[2].metric("Ready for Odoo", result.export.exported_rows if result.export else 0)
            if result.export and result.export.issues:
                st.dataframe(issues_frame(result.export.issues), hide_index=True)
        downloads(result.files, OUT, key=path.name)
    if cfg.examples_in_use():
        st.warning("config/odoo.toml still has EXAMPLE codes: set your real Odoo codes first.")


def reconcile_tab(settings: dict) -> None:
    st.write("Upload the bank statement(s) and the books. Everything is cleaned, then matched.")
    bank = st.file_uploader("Bank statement(s)", type=TYPES, accept_multiple_files=True)
    books = st.file_uploader("Books / ledger", type=TYPES, accept_multiple_files=True)
    c1, c2 = st.columns(2)
    amount = c1.number_input("Amounts may differ by", min_value=0.0, value=0.0, step=1.0)
    days = c2.number_input("Dates may differ by (days)", min_value=0, value=2, step=1)
    if not (bank and books) or not st.button("Reconcile", type="primary"):
        return
    work = Path(tempfile.mkdtemp(prefix="reconpipe_"))
    bank_paths = save_uploads([(u.name, u.getvalue()) for u in bank], work / "bank")
    books_paths = save_uploads([(u.name, u.getvalue()) for u in books], work / "books")
    from decimal import Decimal

    folder = OUT / f"workflow_{bank_paths[0].stem}_vs_{books_paths[0].stem}"
    try:
        result = run_workflow(
            bank_paths,
            books_paths,
            folder,
            load_config(target=settings["target"], journal=settings["journal"]),
            Tolerances(amount=Decimal(str(amount)), days=int(days)),
            date_order=settings["date_order"],
            mapper=mapper_for(settings["use_ai"], settings["headers_only"]),
        )
    except PROBLEMS as exc:
        st.error(str(exc))
        return
    s = result.reconciliation.summary
    by_status = s["items_by_status"]
    cols = st.columns(4)
    cols[0].metric("Bank lines", s["a_lines"])
    cols[1].metric("Matched", f"{s['match_rate']:.1%}")
    cols[2].metric("Need review", sum(v for k, v in by_status.items() if k != "matched"))
    cols[3].metric("Value in exception", f"{s['value_in_exception']:,.2f}")
    exceptions = [
        {
            "status": i.status,
            "bank amount": i.a_amount,
            "books amount": i.b_amount,
            "detail": i.detail,
        }
        for i in result.reconciliation.items
        if i.status != "matched"
    ]
    st.dataframe(pd.DataFrame(exceptions), hide_index=True)
    st.text("\n".join(result.summary_lines()))
    downloads(result.files, folder, key="workflow")


def ai_tab(settings: dict) -> None:
    library = ai.prompts()
    st.write("Pick a prompt; it is filled in from your file. Nothing is sent until you ask.")
    name = st.selectbox("Prompt", list(library), format_func=lambda n: f"{library[n].title} ({n})")
    upload = st.file_uploader("File", type=TYPES, key="ai")
    if not upload:
        return
    (path,) = save_uploads(
        [(upload.name, upload.getvalue())], Path(tempfile.mkdtemp(prefix="reconpipe_"))
    )
    try:
        prompt = ai.render(name, path, settings["headers_only"])
    except PROBLEMS as exc:
        st.error(str(exc))
        return
    st.text_area("Prompt (copy it, or ask Claude below)", prompt, height=300)
    if st.button("Ask Claude"):
        try:
            with st.spinner("Claude is answering..."):
                answer = ai.ask_claude(prompt)
        except ai.AIError as exc:
            st.error(str(exc))
            return
        st.markdown(answer)
        st.download_button("Download the answer", answer, f"{path.stem}_{name}.md")


def main() -> None:
    st.set_page_config(page_title="ReconPipe toolkit", layout="wide")
    st.title("ReconPipe toolkit")
    st.caption("Messy bank files in; clean data, Odoo imports and reconciliations out.")
    settings = sidebar()
    process, reconcile, assistant = st.tabs(["Process a file", "Reconcile", "AI assistant"])
    try:
        with process:
            process_tab(settings)
        with reconcile:
            reconcile_tab(settings)
        with assistant:
            ai_tab(settings)
    except OdooConfigError as exc:
        st.error(str(exc))


if __name__ == "__main__":
    main()
