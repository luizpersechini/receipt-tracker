"""Zero-transaction statement uploads must say WHY (scan vs unknown layout).

A scanned statement (image-only pages, no text layer) parses to an empty
list without any error — the regression this guards is the upload UI
reporting that as a plain success. Covers the diagnosis helper directly
and the /api/upload/statement warning field end-to-end.
"""

import io

import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import settings
from src.services.statement_pdf_parser import zero_parse_warning


def _png_bytes(size=(200, 280)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(250, 248, 244)).save(buf, format="PNG")
    return buf.getvalue()


def _scan_like_pdf(path, pages=2):
    """Image-only PDF: what a scanner/photo export produces."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=_png_bytes())
    doc.save(str(path))
    doc.close()


def _text_pdf(path):
    """PDF with a real text layer but nothing resembling Itaú's layout."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Relatório qualquer com texto suficiente para não parecer um scan, "
        "mas sem nenhuma transação no layout esperado do cartão.",
    )
    doc.save(str(path))
    doc.close()


# ─── Helper-level ───────────────────────────────────────────────────────────


def test_scan_pdf_gets_scan_warning(tmp_path):
    p = tmp_path / "scan.pdf"
    _scan_like_pdf(p)
    msg = zero_parse_warning(str(p))
    assert "scan" in msg
    assert "text layer" in msg


def test_text_pdf_gets_layout_warning(tmp_path):
    p = tmp_path / "text.pdf"
    _text_pdf(p)
    msg = zero_parse_warning(str(p))
    assert "layout" in msg
    assert "scan" not in msg


# ─── Endpoint-level ─────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired to a throwaway DB + statements dir."""
    statements = tmp_path / "statements"
    statements.mkdir()
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "statements_dir", statements)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    from src.db import database

    database.init_db()

    from src.main import app

    return TestClient(app)


def _upload(client, path):
    with open(path, "rb") as f:
        return client.post(
            "/api/upload/statement",
            files={"file": (path.name, f, "application/pdf")},
        )


def test_upload_scan_statement_returns_warning(client, tmp_path):
    p = tmp_path / "fatura_scan.pdf"
    _scan_like_pdf(p)
    res = _upload(client, p)
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["transaction_count"] == 0
    assert "scan" in body["warning"]


def test_upload_text_pdf_returns_layout_warning(client, tmp_path):
    p = tmp_path / "outro_layout.pdf"
    _text_pdf(p)
    res = _upload(client, p)
    assert res.status_code == 200
    assert "layout" in res.json()["warning"]
