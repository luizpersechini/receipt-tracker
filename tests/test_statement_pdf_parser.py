"""Tests for the Itaú PDF statement parser.

We synthesise small PDFs in-memory rather than committing real bank
statements as fixtures (they contain PII). The synthesised pages reproduce
the exact text layout PyMuPDF extracts from a real Itaú statement:
section headers + 3-line domestic txns + 8-line international txns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")

from src.services.statement_pdf_parser import (
    parse_statement_pdf,
    _parse_brl,
)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _write_pdf(path: Path, lines: list[str]) -> None:
    """Render a single-page PDF with each line stacked vertically.

    PyMuPDF's text extraction will return these in the same order, which
    is exactly the shape the parser walks over.
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=12 + 14 * len(lines))
    y = 20
    for line in lines:
        page.insert_text((30, y), line, fontsize=10)
        y += 14
    doc.save(str(path))
    doc.close()


# ─── _parse_brl ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("24,20", 24.20),
        ("4.662,00", 4662.00),
        ("- 6.789,36", -6789.36),  # sign preserved — credits/estornos are rows now
        ("5,62", 5.62),
        ("", None),
        ("not a number", None),
    ],
)
def test_parse_brl_handles_pt_br_decimals(raw, expected):
    assert _parse_brl(raw) == expected


# ─── Domestic-only statement ───────────────────────────────────────────────


def test_parses_domestic_transactions(tmp_path):
    pdf = tmp_path / "domestic.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Lançamentos: compras e saques",
            "DATA",
            "VALOR EM R$",
            "21/03",
            "DL          *UberRides",
            "24,20",
            "VEÍCULOS .Sao Paulo",
            "22/03",
            "UBER* TRIP",
            "17,81",
            "VEÍCULOS .WWW.UBER.COM.",
            "26/03",
            "CHARLIE CHAPLIN DRINK",
            "4.662,00",
            "ALIMENTAÇÃO .ITAGIBA",
        ],
    )

    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 3

    # Year inferred from Postagem header
    assert txns[0]["date"] == "2026-03-21"
    assert txns[0]["description"] == "DL          *UberRides"
    assert txns[0]["amount"] == 24.20
    assert txns[0]["raw_data"]["section"] == "domestic"
    assert txns[0]["raw_data"]["original_currency"] == "BRL"

    # Thousands separator parses correctly
    assert txns[2]["amount"] == 4662.00
    assert txns[2]["category"] == "meals"  # "drink" → "meals" via keyword


def test_skips_payments_section_entirely(tmp_path):
    """The '- 6.789,36' payment row in the Pagamentos efetuados section
    must NOT show up as a positive R$ 6789 transaction."""
    pdf = tmp_path / "with_payments.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Pagamentos efetuados",
            "19/03",
            "PAGAMENTO EFETUADO 9487",
            "- 6.789,36",
            "Total dos pagamentos",
            "- 6.789,36",
            "Lançamentos: compras e saques",
            "DATA",
            "VALOR EM R$",
            "22/03",
            "UBER* TRIP",
            "17,81",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 1
    assert txns[0]["description"] == "UBER* TRIP"
    assert txns[0]["amount"] == 17.81
    # No 6789 phantom — positive OR negative (the payment is not an estorno)
    assert all(abs(t["amount"]) != 6789.36 for t in txns)


# ─── International ─────────────────────────────────────────────────────────


def test_parses_international_with_fx(tmp_path):
    pdf = tmp_path / "intl.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Lançamentos internacionais",
            "LUIZ PERSECHINI (final 2928)",
            "DATA",
            "ESTABELECIMENTO",
            "VALOR EM US$",
            "06/03",
            "UBER CANADA/UBERTRIP",
            "90,31",
            "TORONTO",
            "21,88",
            "CAD",
            "16,07",
            "Dólar de Conversão R$ 5,62",
            "07/03",
            "SQ *TURF KIT-CT NO (KIT",
            "129,37",
            "Vancouver",
            "31,21",
            "CAD",
            "23,02",
            "Dólar de Conversão R$ 5,62",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 2

    a, b = txns
    assert a["date"] == "2026-03-06"
    assert a["amount"] == 90.31  # BRL on the bill
    assert a["raw_data"]["section"] == "international"
    assert a["raw_data"]["original_currency"] == "CAD"
    assert a["raw_data"]["original_amount"] == 21.88
    assert a["raw_data"]["fx_to_brl"] == 5.62
    assert a["raw_data"]["city"] == "TORONTO"
    assert a["category"] == "transportation"  # "uber" keyword

    assert b["raw_data"]["original_amount"] == 31.21
    assert b["raw_data"]["original_currency"] == "CAD"


def test_falls_back_to_domestic_when_intl_fx_marker_missing(tmp_path):
    """If a 'transaction-looking' triplet lacks the 'Dólar de Conversão'
    marker, we treat it as domestic — never invent a fake int'l row."""
    pdf = tmp_path / "mixed.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Lançamentos: compras e saques",
            "DATA",
            "VALOR EM R$",
            "08/03",
            "STARBUCKS 71-CT",
            "64,52",
            "TORONTO",  # looks like a city, but the FX line never follows
            "15,56",
            "CAD",
            "11,42",
            # No "Dólar de Conversão" line — int'l matcher must reject and
            # fall back to plain domestic 3-line parse.
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 1
    assert txns[0]["amount"] == 64.52
    assert txns[0]["raw_data"]["section"] == "domestic"


# ─── Year inference fallback ───────────────────────────────────────────────


def test_year_falls_back_to_current_when_postagem_missing(tmp_path):
    from datetime import datetime

    pdf = tmp_path / "noyear.pdf"
    _write_pdf(
        pdf,
        [
            "Lançamentos: compras e saques",
            "22/03",
            "UBER* TRIP",
            "17,81",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 1
    assert txns[0]["date"].startswith(f"{datetime.now().year}-")


# ─── Dispatcher ────────────────────────────────────────────────────────────


def test_parse_statement_dispatcher_routes_pdf(tmp_path):
    """The top-level dispatcher in statement_parser.parse_statement must
    pick the PDF parser for .pdf and the CSV parser for everything else."""
    pdf = tmp_path / "demo.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Lançamentos: compras e saques",
            "22/03",
            "UBER* TRIP",
            "17,81",
        ],
    )
    from src.services.statement_parser import parse_statement

    txns = parse_statement(str(pdf))
    assert len(txns) == 1
    assert txns[0]["amount"] == 17.81


def test_iso_date_rolls_back_year_across_statement_boundary():
    """Regression: a January statement (Postagem 05/01/2026) lists December
    purchases as '15/12' — they belong to 2025, not 11 months in the future."""
    from src.services.statement_pdf_parser import _DATE_RE, _iso_date

    dec = _DATE_RE.match("15/12")
    jan = _DATE_RE.match("03/01")
    assert _iso_date(dec, 2026, (1, 5)) == "2025-12-15"
    assert _iso_date(jan, 2026, (1, 5)) == "2026-01-03"
    # No Postagem found → behave as before (no rollback guess).
    assert _iso_date(dec, 2026, None) == "2026-12-15"


# ─── Credits / estornos ─────────────────────────────────────────────────────


def test_captures_estorno_as_negative_credit_row(tmp_path):
    """A negative amount inside the purchases section is a refund — it must
    come through as a negative-amount row flagged credit, not be dropped."""
    pdf = tmp_path / "estorno.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Lançamentos: compras e saques",
            "22/03",
            "UBER* TRIP",
            "17,81",
            "VEÍCULOS .WWW.UBER.COM.",
            "25/03",
            "ESTORNO LOJA EXEMPLO",
            "- 123,45",
            "ALIMENTAÇÃO .SAO PAULO",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 2
    credit = next(t for t in txns if t["amount"] < 0)
    assert credit["amount"] == -123.45
    assert credit["description"] == "ESTORNO LOJA EXEMPLO"
    assert credit["raw_data"].get("credit") is True
    # The normal purchase is untouched
    purchase = next(t for t in txns if t["amount"] > 0)
    assert "credit" not in purchase["raw_data"]


def test_second_payments_block_is_also_skipped(tmp_path):
    """Multi-cardholder statements repeat 'Pagamentos efetuados'. Every block
    must be skipped — now that negatives parse, an unskipped second block
    would emit the bill payment as a phantom credit row."""
    pdf = tmp_path / "two_payments.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/04/2026",
            "Pagamentos efetuados",
            "19/03",
            "PAGAMENTO EFETUADO 9487",
            "- 6.789,36",
            "Lançamentos: compras e saques",
            "22/03",
            "UBER* TRIP",
            "17,81",
            "VEÍCULOS .WWW.UBER.COM.",
            "Pagamentos efetuados",
            "20/03",
            "PAGAMENTO EFETUADO 1111",
            "- 1.000,00",
            "Lançamentos: compras e saques",
            "23/03",
            "PADARIA SOL",
            "30,00",
            "ALIMENTAÇÃO .RIO",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    amounts = sorted(t["amount"] for t in txns)
    assert amounts == [17.81, 30.00]


def test_intl_city_dropped_when_merchant_echo(tmp_path):
    """Newer templates print a shortened merchant echo in the city slot for
    online purchases ("METALPRICEAPI.COM" -> "METALPRICEAPI"); a real city
    (TORONTO) must survive."""
    pdf = tmp_path / "echo.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/08/2026",
            "Lançamentos internacionais",
            "DATA",
            "ESTABELECIMENTO",
            "VALOR EM US$",
            "17/07",
            "METALPRICEAPI.COM",
            "64,99",
            "METALPRICEAPI",
            "11,99",
            "USD",
            "11,99",
            "Dólar de Conversão R$ 5,42",
            "08/07",
            "ANTHROPIC*CLAUDESUB",
            "587,26",
            "ANTHROPIC.COM",
            "107,36",
            "USD",
            "107,36",
            "Dólar de Conversão R$ 5,47",
            "06/03",
            "UBER CANADA/UBERTRIP",
            "90,31",
            "TORONTO",
            "21,88",
            "CAD",
            "16,07",
            "Dólar de Conversão R$ 5,62",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 3
    assert txns[0]["raw_data"]["city"] is None  # exact-prefix echo
    assert txns[1]["raw_data"]["city"] is None  # shared >=8-char prefix echo
    assert txns[2]["raw_data"]["city"] == "TORONTO"  # real city untouched


def test_tags_transactions_by_cardholder(tmp_path):
    """Corporate statements group each card under a "NAME (final NNNN)"
    header; every transaction must be tagged with the active cardholder,
    and the card's own subtotal line must not be mistaken for a header."""
    pdf = tmp_path / "corp.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/09/2026",
            "Lançamentos: compras e saques",
            "TONY LIMA (final 9218)",
            "DATA",
            "ESTABELECIMENTO",
            "VALOR EM R$",
            "26/08",
            "CURB PHL TAXI",
            "223,67",
            "Lançamentos no cartão (final 9218)",  # subtotal — NOT a header
            "223,67",
            "LUIZ PERSECHINI (final 2928)",
            "DATA",
            "ESTABELECIMENTO",
            "VALOR EM R$",
            "13/08",
            "RIO DESIGN HOTEL",
            "1.664,80",
            "31/08",
            "SAMSUNG DIAMOND MALL",
            "549,00",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    by_holder = {}
    for t in txns:
        by_holder.setdefault(t.get("cardholder"), []).append(t)

    assert set(by_holder) == {"TONY LIMA", "LUIZ PERSECHINI"}
    assert len(by_holder["TONY LIMA"]) == 1
    assert by_holder["TONY LIMA"][0]["raw_data"]["card_last4"] == "9218"
    assert len(by_holder["LUIZ PERSECHINI"]) == 2
    assert by_holder["LUIZ PERSECHINI"][0]["raw_data"]["card_last4"] == "2928"


def test_single_cardholder_statement_leaves_tag_absent(tmp_path):
    """A statement with no cardholder header must not invent a cardholder —
    keeps single-card uploads a single statement (no split)."""
    pdf = tmp_path / "solo.pdf"
    _write_pdf(
        pdf,
        [
            "Postagem: 05/09/2026",
            "Lançamentos: compras e saques",
            "13/08",
            "RIO DESIGN HOTEL",
            "1.664,80",
        ],
    )
    txns = parse_statement_pdf(str(pdf))
    assert len(txns) == 1
    assert txns[0].get("cardholder") is None


def test_ifood_statement_descriptors_are_meals():
    """Itaú prints iFood as "IFD*<restaurant>"; the prefix must map to meals
    (both hard-truth misses in the category comparison were this gap)."""
    from src.services.statement_pdf_parser import _guess_category

    assert _guess_category("IFD*iFood") == "meals"
    assert _guess_category("IFD*VARANDA1389DELIV") == "meals"
    assert _guess_category("IFD*FRUTAFAST BAR E LA") == "meals"
