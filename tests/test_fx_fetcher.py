"""Unit tests for FX helpers (network calls are not exercised here)."""

import json

from src.services.fx_fetcher import convert, get_unique_currencies


def test_convert_same_currency_returns_input():
    assert convert(100.0, "BRL", "BRL", {}) == 100.0


def test_convert_uses_rate_table():
    assert convert(10.0, "USD", "BRL", {"USD": 5.0}) == 50.0


def test_convert_none_amount_returns_none():
    assert convert(None, "USD", "BRL", {"USD": 5.0}) is None


def test_convert_missing_rate_returns_none():
    assert convert(10.0, "USD", "BRL", {}) is None


def test_unique_currencies_from_string_metadata():
    receipts = [
        {"metadata": json.dumps({"currency": "BRL"})},
        {"metadata": json.dumps({"currency": "usd"})},
        {"metadata": json.dumps({"currency": "BRL"})},
        {"metadata": json.dumps({"currency": None})},
    ]
    assert get_unique_currencies(receipts) == ["BRL", "USD"]


def test_unique_currencies_from_dict_metadata():
    receipts = [
        {"metadata": {"currency": "EUR"}},
        {"metadata": {"currency": "GBP"}},
        {"metadata": {}},
    ]
    assert get_unique_currencies(receipts) == ["EUR", "GBP"]
