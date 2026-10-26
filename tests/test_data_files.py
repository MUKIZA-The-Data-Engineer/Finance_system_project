"""The committed input files must be byte-sound, not just readable by a forgiving parser.

pdfplumber silently repairs a PDF whose cross-reference table is broken (for
example after a text-mode line-ending conversion), so the parser tests alone
cannot catch a corrupted file. These checks read the files strictly.
"""

import io
import zipfile
from pathlib import Path

import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


@pytest.mark.parametrize("path", sorted(RAW.glob("*.pdf")), ids=lambda p: p.name)
def test_pdf_structure_is_intact(path):
    data = path.read_bytes()
    PDFDocument(PDFParser(io.BytesIO(data)), fallback=False)  # raises if the xref is broken
    assert b"\r\n" not in data, "CRLF line endings: the file went through a text conversion"


@pytest.mark.parametrize("path", sorted(RAW.glob("*.xlsx")), ids=lambda p: p.name)
def test_workbook_zip_is_intact(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
