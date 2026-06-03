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
        # Fuel, parking and taxi/transport share the Abastecimento column.
        ("transportation", "UBER* TRIP", "abastecimento"),
        ("transportation", "PARKCHAMP PARKING", "abastecimento"),
        ("fuel", "Posto BR", "abastecimento"),
        ("office_supplies", "Staples", "outros"),
        ("travel", "VISTO LONDRES", "outros"),
        # Lodging now has its own dedicated column.
        ("lodging", "Sutton Place Hotel", "hospedagem"),
        ("other", "Whatever", "outros"),
        (None, "Posto Shell combustível", "abastecimento"),
        (None, "", "outros"),
    ],
)
def test_bucket(category, label, expected):
    assert erp._bucket(category, label) == expected


@pytest.mark.parametrize(
    "category,label,expected",
    [
        ("meals", "Restaurante Z", "ALIMENTAÇÃO"),
        ("transportation", "UBER* TRIP", "UBER"),
        ("transportation", "TAXI RIO SUL", "TAXI"),
        ("transportation", "DL *UBERRIDES", "UBER"),
        ("transportation", "99 POP", "TRANSPORTE"),
        ("lodging", "Hilton", "HOSPEDAGEM"),
        ("fuel", "Posto", "ABASTECIMENTO"),
        ("office_supplies", "Kalunga", "MATERIAL"),
        ("travel", "LATAM", "VIAGEM"),
        ("other", "Wi-Fi on board", "WI-FI ON BOARD"),
        (None, "", "—"),
    ],
)
def test_auto_detalhe(category, label, expected):
    assert erp.auto_detalhe(category, label) == expected


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


# ─── Reimbursement report ───────────────────────────────────────────────────


def _add_reimb(db, vendor, amount, currency, date, category="meals"):
    return db.save_receipt(
        filename=f"{vendor}.jpg",
        filepath="",  # no file → synthetic card
        metadata={
            "vendor_name": vendor,
            "total_amount": amount,
            "currency": currency,
            "date": date,
            "category": category,
        },
        description="reimb",
    )


def test_reimbursement_report_filters_by_currency(report_env):
    db = report_env
    r1 = _add_reimb(db, "Cafe BR", 30.0, "BRL", "2026-04-10")
    r2 = _add_reimb(db, "Coffee CA", 8.0, "CAD", "2026-04-11", "meals")
    # Mark both as reimbursement.
    db.update_receipt(r1, column_patch={"expense_type": "reimbursement"}, metadata_patch={})
    db.update_receipt(r2, column_patch={"expense_type": "reimbursement"}, metadata_patch={})

    pdf = erp.build_reimbursement_report(currency="BRL")
    assert pdf[:4] == b"%PDF"
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        front = doc[0].get_text()
        # Reembolso framing, BRL row present, CAD row excluded.
        assert "Reembolso" in front
        assert "CAFE BR" in front.upper()
        assert "COFFEE CA" not in front.upper()
        # No IOF / justification on a reimbursement report.
        assert "sem comprovante" not in front.lower()
    finally:
        doc.close()


def test_reimbursement_report_empty_raises(report_env):
    with pytest.raises(ValueError):
        erp.build_reimbursement_report(currency="BRL")


# ─── Open-item justification notes ──────────────────────────────────────────


def test_statement_note_prints_on_report(report_env):
    db = report_env
    from src.services import statement_notes

    txns = [
        {
            "date": "2026-04-05",
            "description": "UNKNOWN CHARGE",
            "amount": 99.0,
            "category": "other",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("s.csv", "s.csv", txns)
    # No match for index 0 → open item. Add a justification.
    statement_notes.set_note(sid, 0, "Cobrança não identificada — recibo perdido")

    pdf = erp.build_statement_report(sid)
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        front = " ".join(doc[0].get_text().split())  # normalise PDF line wraps
        # The typed note replaces the blank "justificativa: ___" placeholder.
        assert "recibo perdido" in front
        assert "justificativa: ____" not in front
    finally:
        doc.close()


def test_open_items_page_lists_charges_and_justifications(report_env):
    db = report_env
    from src.services import statement_notes

    txns = [
        {
            "date": "2026-04-05",
            "description": "MYSTERY CHARGE",
            "amount": 99.0,
            "category": "other",
            "raw_data": {"section": "domestic"},
        },
        {
            "date": "2026-04-06",
            "description": "LOST TAXI",
            "amount": 30.0,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("oi.csv", "oi.csv", txns)
    statement_notes.set_note(sid, 0, "Assinatura sem recibo emitido")
    # index 1 left without a note → blank placeholder.

    pdf = erp.build_statement_report(sid)
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        full = " ".join(("\n".join(p.get_text() for p in doc)).split())
        # The dedicated open-items page heading + both charges + the note.
        # The open-items page shows the raw bank descriptor (identifying),
        # not the generic auto label.
        assert "Lançamentos sem comprovante" in full
        assert "MYSTERY CHARGE" in full
        assert "LOST TAXI" in full
        assert "Assinatura sem recibo emitido" in full
        # The page is its own page after the front spreadsheet.
        page_with_heading = next(
            i for i, p in enumerate(doc) if "Lançamentos sem comprovante" in p.get_text()
        )
        assert page_with_heading >= 1
    finally:
        doc.close()


def test_category_override_changes_report_column(report_env):
    db = report_env
    from src.services import statement_categories

    txns = [
        {
            "date": "2026-04-01",
            "description": "MYSTERY",
            "amount": 50.0,
            "category": "other",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("c.csv", "c.csv", txns)
    # Parser said "other" (→ Outros). Override to meals → Alimentação column.
    statement_categories.set_category(sid, 0, "meals")
    data = erp._assemble(sid)
    assert data["rows"][0]["col"] == "alimentacao"
    # `fuel` override reaches the Abastecimento column.
    statement_categories.set_category(sid, 0, "fuel")
    assert erp._assemble(sid)["rows"][0]["col"] == "abastecimento"
    # `lodging` override reaches the Hospedagem column.
    statement_categories.set_category(sid, 0, "lodging")
    assert erp._assemble(sid)["rows"][0]["col"] == "hospedagem"


def test_matched_receipt_category_beats_override(report_env):
    db = report_env
    from src.services import statement_categories

    txns = [
        {
            "date": "2026-04-01",
            "description": "UBER",
            "amount": 50.0,
            "category": "other",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("c2.csv", "c2.csv", txns)
    statement_categories.set_category(sid, 0, "meals")  # override → would be Alimentação
    rid = db.save_receipt(
        "u.jpg",
        "",
        {
            "vendor_name": "Uber",
            "total_amount": 50.0,
            "currency": "BRL",
            "date": "2026-04-01",
            "category": "transportation",
        },
        "x",
    )
    db.save_match(rid, sid, txns[0], 0.9, match_kind="manual", transaction_index=0)
    # Receipt (transportation → Abastecimento) wins over the meals override.
    assert erp._assemble(sid)["rows"][0]["col"] == "abastecimento"


def test_no_open_items_page_when_all_matched(report_env):
    db = report_env
    rid = _add_reimb(db, "Cafe", 12.0, "BRL", "2026-04-10")
    txns = [
        {
            "date": "2026-04-10",
            "description": "CAFE",
            "amount": 12.0,
            "category": "meals",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("ok.csv", "ok.csv", txns)
    db.save_match(rid, sid, txns[0], 0.99, match_kind="manual", transaction_index=0)
    pdf = erp.build_statement_report(sid)
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        full = "\n".join(p.get_text() for p in doc)
        assert "Lançamentos sem comprovante" not in full
    finally:
        doc.close()


def test_open_item_without_note_shows_blank_placeholder(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-04-05",
            "description": "MYSTERY",
            "amount": 10.0,
            "category": "other",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("s2.csv", "s2.csv", txns)
    pdf = erp.build_statement_report(sid)
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        front = doc[0].get_text()
        assert "sem comprovante" in front.lower()
    finally:
        doc.close()


# ─── Detalhamento + Centro de Custo per-line overrides ──────────────────────


def test_detalhe_defaults_to_auto_label(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-04-05",
            "description": "UBER* TRIP",
            "amount": 20.0,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("d.csv", "d.csv", txns)
    row = erp._assemble(sid)["rows"][0]
    assert row["detalhe"] == "UBER"  # auto label from category + descriptor
    assert row["cost_center"] == ""  # no override yet


def test_detalhe_and_cost_center_overrides(report_env):
    db = report_env
    from src.services import statement_costcenters, statement_labels

    txns = [
        {
            "date": "2026-04-05",
            "description": "POSTO XYZ",
            "amount": 400.0,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("d2.csv", "d2.csv", txns)
    statement_labels.set_label(sid, 0, "MOTORISTA RAYLANDER")
    statement_costcenters.set_cost_center(sid, 0, "20101/AA7")
    row = erp._assemble(sid)["rows"][0]
    assert row["detalhe"] == "MOTORISTA RAYLANDER"  # override wins over auto label
    assert row["cost_center"] == "20101/AA7"


# ─── Cost center at the receipt level ───────────────────────────────────────


def _match(db, sid, txn, meta):
    rid = db.save_receipt("r.jpg", "", meta, "x")
    db.save_match(rid, sid, txn, 0.9, match_kind="manual", transaction_index=0)
    return rid


def test_matched_receipt_cost_centers_drive_line(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-04-01",
            "description": "UBER",
            "amount": 50.0,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        }
    ]
    sid = db.save_statement("cc.csv", "cc.csv", txns)
    _match(
        db,
        sid,
        txns[0],
        {
            "vendor_name": "Uber",
            "total_amount": 50.0,
            "currency": "BRL",
            "date": "2026-04-01",
            "category": "transportation",
            "cost_centers": "20101/AA1",
        },
    )
    assert erp._assemble(sid)["rows"][0]["cost_center"] == "20101/AA1"


def test_multiple_receipts_join_cost_centers(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-04-01",
            "description": "HOTEL",
            "amount": 300.0,
            "category": "lodging",
            "raw_data": {"section": "domestic"},
        }
    ]
    sid = db.save_statement("cc2.csv", "cc2.csv", txns)
    base = {"total_amount": 100.0, "currency": "BRL", "date": "2026-04-01", "category": "lodging"}
    _match(db, sid, txns[0], {**base, "vendor_name": "A", "cost_centers": "20101/AA1"})
    _match(db, sid, txns[0], {**base, "vendor_name": "B", "cost_centers": "20101/AA2, 20101/AA1"})
    # Unique, order-preserving join across both matched receipts.
    assert erp._assemble(sid)["rows"][0]["cost_center"] == "20101/AA1, 20101/AA2"


def test_blank_receipt_cc_falls_back_to_profile(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-04-01",
            "description": "UBER",
            "amount": 50.0,
            "category": "transportation",
            "raw_data": {"section": "domestic"},
        }
    ]
    sid = db.save_statement("cc3.csv", "cc3.csv", txns)
    _match(
        db,
        sid,
        txns[0],
        {
            "vendor_name": "Uber",
            "total_amount": 50.0,
            "currency": "BRL",
            "date": "2026-04-01",
            "category": "transportation",
        },  # no cost_centers
    )
    data = erp._assemble(sid)
    assert data["rows"][0]["cost_center"] == ""  # blank → render fills profile default
    from src.services.report_profile import DEFAULT_PROFILE

    prof = erp._with_global_accounts({**DEFAULT_PROFILE, "centro_custo": "20101/AA9"})
    assert "20101/AA9" in erp._front_page_html(data, prof)


def test_front_page_has_form_structure(report_env):
    db = report_env
    txns = [
        {
            "date": "2026-03-03",
            "description": "ALMOCO",
            "amount": 45.0,
            "category": "meals",
            "raw_data": {"section": "domestic"},
        },
    ]
    sid = db.save_statement("f.csv", "f.csv", txns)
    pdf = erp.build_statement_report(sid, vencimento="2026-03-20")
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        front = " ".join(doc[0].get_text().split())  # normalise PDF line wraps
        # New column headers from the MMB-F-REG-0101-001-00 form.
        for header in (
            "Centro de Custo+Uneg",
            "Conta Contábil",
            "Detalhamento da despesa",
            "Alimentação",
            "Abastecimento",
            "Hospedagem",
            "Outros",
        ):
            assert header in front, header
        # Header band fields.
        assert "MMB-F-REG-0101-001-00" in front
        assert "VENCIMENTO" in front.upper()
        assert "20/03/2026" in front  # vencimento formatted pt-BR
        assert "Aprovador imediato" in front
        # Old layout bits are gone.
        assert "Motivo do Gasto" not in front
    finally:
        doc.close()
