"""Tests for the shared money parser (src/services/money.py).

Regression suite for the separator bugs that zeroed or 1000x-shrank amounts
across statement_parser, gmail_ingest, uber_csv and llm_processor.
"""

import pytest

from src.services.money import parse_money
from src.services import gmail_ingest, statement_parser, uber_csv
from src.services import llm_processor as llm


# ─── Both separators present: rightmost wins ────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("12.345.678,90", 12345678.90),
        ("12,345,678.90", 12345678.90),
        ("R$ 1.234,56", 1234.56),
        ("$1,234.56", 1234.56),
    ],
)
def test_dual_separator(raw, expected):
    assert parse_money(raw) == pytest.approx(expected)


# ─── Single separator ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("41,78", 41.78),
        ("41.78", 41.78),
        ("5,5", 5.5),
        ("0,56", 0.56),
        # 3-digit tail with no hint → grouping (money rarely has 3 decimals)
        ("1,234", 1234.0),
        ("1.234", 1234.0),
        ("1.234.567", 1234567.0),
        ("1 234,56", 1234.56),
        ("1234", 1234.0),
    ],
)
def test_single_separator_inferred(raw, expected):
    assert parse_money(raw) == pytest.approx(expected)


def test_locale_hint_settles_three_digit_tail():
    # BR: '.' groups, ',' is decimal (3 decimals stays decimal e.g. fuel price)
    assert parse_money("1.234", comma_decimal=True) == 1234.0
    assert parse_money("1,234", comma_decimal=True) == pytest.approx(1.234)
    # US: ',' groups, '.' is decimal
    assert parse_money("1,234", comma_decimal=False) == 1234.0
    assert parse_money("1.234", comma_decimal=False) == pytest.approx(1.234)


# ─── Currency prefixes, signs, junk ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("R$ 50,00", 50.0),
        ("CAD$ 38.10", 38.10),
        ("US$ 5.99", 5.99),
        ("€ 12,00", 12.0),
        ("-12.50", -12.5),
        ("12.50-", -12.5),
        ("(12.50)", -12.5),
    ],
)
def test_prefixes_and_signs(raw, expected):
    assert parse_money(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "   ", "nan", "abc", "12-34", True, False])
def test_unparseable_returns_none(raw):
    assert parse_money(raw) is None


def test_nan_float_returns_none():
    assert parse_money(float("nan")) is None
    assert parse_money(float("inf")) is None


def test_numeric_passthrough():
    assert parse_money(42) == 42.0
    assert parse_money(41.78) == pytest.approx(41.78)


# ─── The wrappers keep their contracts ───────────────────────────────────────


def test_statement_parser_thousands_no_longer_zeroed():
    assert statement_parser._parse_amount("1.234,56") == pytest.approx(1234.56)
    assert statement_parser._parse_amount("1,234.56") == pytest.approx(1234.56)
    assert statement_parser._parse_amount("garbage") == 0.0  # legacy contract


def test_gmail_amount_thousands():
    assert gmail_ingest._parse_amount_str("1.234,56") == pytest.approx(1234.56)
    assert gmail_ingest._parse_amount_str("32.29") == pytest.approx(32.29)


def test_uber_csv_brl_prefix_and_thousands():
    assert uber_csv._parse_amount("R$ 50,00") == pytest.approx(50.0)
    assert uber_csv._parse_amount("1.234,56") == pytest.approx(1234.56)
    assert uber_csv._parse_amount("0") is None  # non-positive filtered


def test_llm_parse_amount_locale_and_letter_prefix():
    assert llm._parse_amount("1.234", comma_decimal=True) == 1234.0
    assert llm._parse_amount("1,234", comma_decimal=False) == 1234.0
    assert llm._parse_amount("CAD$ 38.10", comma_decimal=False) == pytest.approx(38.10)
    assert llm._parse_amount(None, comma_decimal=False) is None


def test_decimal_hint_known_vs_unknown_locale():
    # Known comma-decimal → True, known dot-decimal → False
    assert llm._decimal_hint("portuguese", "BRL") is True
    assert llm._decimal_hint("english", "USD") is False
    # Unknown locale (e.g. Indonesian / IDR) → None so parse_money infers
    assert llm._decimal_hint("indonesian", "IDR") is None
    assert llm._decimal_hint(None, None) is None


def test_unknown_locale_reads_three_digit_group_as_thousands():
    """Regression (surfaced by the CORD QA corpus): an unrecognised-locale
    receipt's "38.000" must parse as 38000, not 38 — while a known dot-decimal
    receipt keeps "1.234" as the 1.234 fuel-price reading."""
    assert llm._parse_amount("38.000", llm._decimal_hint("indonesian", "IDR")) == 38000.0
    assert llm._parse_amount("6.300.000", llm._decimal_hint(None, "IDR")) == 6300000.0
    # known dot-decimal locale is unchanged
    assert llm._parse_amount("1.234", llm._decimal_hint("english", "USD")) == pytest.approx(1.234)
