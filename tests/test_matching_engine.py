"""Unit tests for the receipt-to-statement matcher."""

import json

from src.services.matching_engine import match_receipts_to_statements


def _receipt(rid, *, date, vendor, amount, expense_type="corp_card"):
    return {
        "id": rid,
        "filename": f"receipt_{rid}.jpg",
        "expense_type": expense_type,
        "metadata": json.dumps({
            "date": date,
            "vendor_name": vendor,
            "total_amount": amount,
        }),
    }


def _statement(sid, transactions):
    return {"id": sid, "transactions": transactions}


def test_exact_match_scores_high():
    receipts = [_receipt(1, date="2026-04-30", vendor="Starbucks", amount=12.50)]
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "STARBUCKS #123", "amount": 12.50},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert len(result["matches"]) == 1
    assert result["matches"][0]["match_score"] >= 0.8
    assert result["unmatched_receipts"] == []
    assert result["unmatched_transactions"] == []


def test_date_within_settlement_window_matches():
    """Corp-card transactions can post a few days after the receipt date."""
    receipts = [_receipt(1, date="2026-04-25", vendor="Uber", amount=24.50)]
    statements = [_statement(10, [
        {"date": "2026-04-28", "description": "UBER TRIP", "amount": 24.50},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert len(result["matches"]) == 1


def test_unmatched_when_amount_and_date_diverge():
    receipts = [_receipt(1, date="2026-01-01", vendor="Foo", amount=100.0)]
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "Bar", "amount": 5.0},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert result["matches"] == []
    assert len(result["unmatched_receipts"]) == 1
    assert len(result["unmatched_transactions"]) == 1


def test_each_transaction_only_used_once():
    receipts = [
        _receipt(1, date="2026-04-30", vendor="Starbucks", amount=12.50),
        _receipt(2, date="2026-04-30", vendor="Starbucks", amount=12.50),
    ]
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "STARBUCKS", "amount": 12.50},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert len(result["matches"]) == 1
    assert len(result["unmatched_receipts"]) == 1
