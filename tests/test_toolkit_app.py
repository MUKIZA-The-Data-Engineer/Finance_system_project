"""The upload app: files in, results out (Streamlit AppTest with simulated uploads)."""

from pathlib import Path

import pytest

import tool_fixtures as fx

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402
from streamlit.testing.v1.element_tree import FileUploader  # noqa: E402

pytestmark = pytest.mark.skipif(
    not hasattr(FileUploader, "set_value"), reason="needs a Streamlit with upload testing"
)
APP = Path(__file__).resolve().parent.parent / "app" / "toolkit.py"
CSV = "text/csv"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONPIPE_APP_OUTPUT", str(tmp_path / "app_out"))
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception
    return at


def upload(path: Path):
    return (path.name, path.read_bytes(), CSV)


def test_opens_with_three_tabs(app):
    assert [t.label for t in app.tabs] == ["Process a file", "Reconcile", "AI assistant"]
    assert [u.label for u in app.file_uploader] == [
        "Files",
        "Bank statement(s)",
        "Books / ledger",
        "File",
    ]


def test_upload_and_process(app, tmp_path):
    app.file_uploader[0].set_value([upload(fx.french_csv(tmp_path))]).run()
    app.button[0].click().run()
    assert not app.exception and not app.error
    metrics = {m.label: m.value for m in app.metric}
    assert metrics == {"Clean rows": "4", "Excluded": "0", "Ready for Odoo": "4"}
    assert any("_odoo.xlsx" in str(d.label) for d in app.get("download_button"))
    assert list((tmp_path / "app_out").rglob("*_odoo.xlsx"))


def test_upload_and_reconcile(app, tmp_path):
    app.file_uploader[1].set_value([upload(fx.french_csv(tmp_path))])
    app.file_uploader[2].set_value([upload(fx.english_csv(tmp_path))]).run()
    app.button[0].click().run()  # only the Reconcile button shows: no file in tab 1
    assert not app.exception and not app.error
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Bank lines"] == "4" and metrics["Matched"] == "100.0%"


def test_ai_tab_fills_the_prompt_without_sending(app, tmp_path):
    app.file_uploader[3].set_value(upload(fx.french_csv(tmp_path))).run()
    assert not app.exception
    (prompt,) = app.text_area
    assert "Date" in prompt.value and "{" not in prompt.value


def test_unreadable_upload_shows_an_error(app, tmp_path):
    bad = tmp_path / "empty.csv"
    bad.write_text("")
    app.file_uploader[0].set_value([upload(bad)]).run()
    app.button[0].click().run()
    assert not app.exception and app.error
