"""Tests for the combined expense-report PDF builder.

Pure helpers (bucketing, pt-BR formatting, IOF regex) are unit-tested
directly. The end-to-end build is exercised against a throwaway DB with a
synthesised statement + receipt + match, asserting a valid multi-page PDF
comes out — no pixel assertions.
"""

from __future__ import annotations

import json

import pytest

from src.services import expense_report_pdf as erp


# ─── Bucketing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "category,label,expected",
    [
        ("meals", "Restaurant X", "alimentacao"),
        ("transportation", "UBER* TRIP", "taxi"),
        ("transportation", "PARKCHAMP PARKING", "combustivel"),
        ("office_supplies", "Staples", "material"),
        ("travel", "VISTO LONDRES", "outros"),
        ("lodging", "Sutton Place Hotel", "outros"),
        ("other", "Whatever", "outros"),
        (None, "Posto Shell combustível", "combustivel"),
        (None, "", "outros"),
    ],
)
def test_bucket(category, label, expected):
    assert erp._bucket(category, label) == expected


# ─── pt-BR formatting ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "v,expected",
    [
        (47.98, "47,98"),
        (1234.5, "1.234,50"),
        (3934.44, "3.934,44"),
        (0, "0,00"),
        (None, ""),
    ],
)
def test_fmt_brl(v, expected):
    assert erp._fmt_brl(v) == expected


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-03-21", "21/03/2026"),
        ("2026-03-21T12:00", "21/03/2026"),
        (None, ""),
        ("garbage", "garbage"),
    ],
)
def test_fmt_date_br(iso, expected):
    assert erp._fmt_date_br(iso) == expected


# ─── IOF extraction regex ───────────────────────────────────────────────────


def test_iof_regex_matches_itau_line():
    text = "Total transações inter. em R$\n12.860,18\nRepasse de IOF em R$\n450,25\n"
    m = erp._IOF_RE.search(text)
    assert m
    val = float(m.group(1).replace(".", "").replace(",", "."))
    assert val == 450.25


def test_extract_iof_none_for_non_pdf():
    assert erp._extract_iof({"filepath": "foo.csv"}) is None
    assert erp._extract_iof({}) is None


# ─── End-to-end build ───────────────────────────────────────────────────────

pytest.importorskip("weasyprint", reason="WeasyPrint not installed")


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    from src.config import settings

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    from src.db import database

    database.init_db()
    return database


def test_build_report_endtoend(report_env, tmp_path):
    db = report_env
    # One receipt (no image file → synthetic card path).
    rid = db.save_receipt(
        filename="uber_synth_1.jpg",
        filepath="",  # no on-disk file → renders the "sem imagem" card
        metadata={
            "vendor_name": "Uber",
            "total_amount": 47.98,
            "currency": "BRL",
            "date": "2026-04-05",
            "category": "transportation",
        },
        description="test",
    )
    # A statement with two transactions; one will be matched, one open.
    txns = [
        {
            "date": "2026-04-05",
            "description": "UBER* TRIP",
            "amount": 47.98,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        },
        {
            "date": "2026-04-06",
            "description": "RESTAURANTE",
            "amount": 56.0,
            "category": "meals",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("statement_test.csv", "statement_test.csv", txns)

    # Match the receipt to txn index 0; leave index 1 open.
    db.save_match(rid, sid, txns[0], 0.95, match_kind="manual", transaction_index=0)

    pdf = erp.build_statement_report(sid)
    assert isinstance(pdf, bytes)
    assert pdf[:4] == b"%PDF"

    # Verify it has at least the front page + a receipt page.
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert len(doc) >= 1
        front = doc[0].get_text()
        # Open item should carry the justification note.
        assert "sem comprovante" in front.lower()
        # Matched vendor label appears.
        assert "UBER" in front.upper()
    finally:
        doc.close()


def test_build_report_missing_statement_raises(report_env):
    with pytest.raises(ValueError):
        erp.build_statement_report(99999)
