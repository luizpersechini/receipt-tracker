"""Password-protected statement PDFs (Itaú locks them to a CNPJ prefix).

With STATEMENT_PDF_PASSWORD configured the parser must unlock and read
them transparently — including when the bank quotes the password formatted
("12.345.678") but the PDF expects bare digits. Without it, the upload
must fail with a message that names the missing setting, not a cryptic
fitz error.
"""

import fitz
import pytest

from src.config import settings
from src.services.statement_pdf_parser import parse_statement_pdf, zero_parse_warning

# Deliberately fake — never put a real bank password in the repo.
PW_DIGITS = "12345678"
PW_FORMATTED = "12.345.678"


def _locked_pdf(path, user_pw=PW_DIGITS):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Documento protegido com texto suficiente para nao parecer scan, "
        "mas sem layout de fatura Itau.",
    )
    doc.save(
        str(path),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw=user_pw,
        owner_pw=user_pw,
    )
    doc.close()


def test_unlocks_with_formatted_password(tmp_path, monkeypatch):
    """Configured "12.345.678" must open a PDF whose password is 12345678."""
    p = tmp_path / "locked.pdf"
    _locked_pdf(p)
    monkeypatch.setattr(settings, "statement_pdf_password", PW_FORMATTED)
    assert parse_statement_pdf(str(p)) == []  # unlocked; no Itaú rows in it


def test_unlocks_with_exact_password(tmp_path, monkeypatch):
    p = tmp_path / "locked.pdf"
    _locked_pdf(p)
    monkeypatch.setattr(settings, "statement_pdf_password", PW_DIGITS)
    assert parse_statement_pdf(str(p)) == []


def test_raises_clear_error_without_password(tmp_path, monkeypatch):
    p = tmp_path / "locked.pdf"
    _locked_pdf(p)
    monkeypatch.setattr(settings, "statement_pdf_password", "")
    with pytest.raises(ValueError, match="STATEMENT_PDF_PASSWORD"):
        parse_statement_pdf(str(p))


def test_raises_clear_error_with_wrong_password(tmp_path, monkeypatch):
    p = tmp_path / "locked.pdf"
    _locked_pdf(p)
    monkeypatch.setattr(settings, "statement_pdf_password", "99.999.999")
    with pytest.raises(ValueError, match="password-protected"):
        parse_statement_pdf(str(p))


def test_zero_parse_warning_handles_locked_doc(tmp_path, monkeypatch):
    """Standalone safety: the warning helper must not crash on a locked PDF."""
    p = tmp_path / "locked.pdf"
    _locked_pdf(p)
    monkeypatch.setattr(settings, "statement_pdf_password", "")
    assert "password-protected" in zero_parse_warning(str(p))


def test_upload_locked_statement_via_endpoint(tmp_path, monkeypatch):
    """Full pipeline: with the password configured, an encrypted upload is
    decrypted, parsed (0 Itaú rows here), and answered with the normal
    zero-transaction warning instead of an error."""
    from fastapi.testclient import TestClient

    statements = tmp_path / "statements"
    statements.mkdir()
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "statements_dir", statements)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "statement_pdf_password", PW_FORMATTED)

    from src.db import database

    database.init_db()

    from src.main import app

    client = TestClient(app)
    p = tmp_path / "fatura_protegida.pdf"
    _locked_pdf(p)
    with open(p, "rb") as f:
        res = client.post(
            "/api/upload/statement",
            files={"file": (p.name, f, "application/pdf")},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["transaction_count"] == 0
    assert "layout" in body["warning"]  # decrypted fine; just not Itaú content
