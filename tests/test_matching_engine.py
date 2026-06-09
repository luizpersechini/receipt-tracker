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


def test_amount_alone_does_not_match():
    """Regression: an exact round amount with no date/vendor support used to
    clear the 0.5 threshold and pair unrelated receipts months apart."""
    receipts = [_receipt(1, date="2026-01-05", vendor="Padaria Sol", amount=50.00)]
    statements = [_statement(10, [
        {"date": "2026-06-02", "description": "POSTO SHELL BR", "amount": 50.00},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert result["matches"] == []


def test_global_assignment_prevents_stealing():
    """Regression: receipt 1 (weak match) used to claim the transaction first,
    leaving receipt 2 (near-perfect match) unmatched."""
    receipts = [
        _receipt(1, date="2026-04-28", vendor="Cafe", amount=12.50),
        _receipt(2, date="2026-04-30", vendor="Starbucks", amount=12.50),
    ]
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "STARBUCKS #123", "amount": 12.50},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert len(result["matches"]) == 1
    assert result["matches"][0]["receipt_id"] == 2


def test_no_date_receipt_still_matches_on_strong_vendor():
    receipts = [_receipt(1, date=None, vendor="Starbucks", amount=12.50)]
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "STARBUCKS #123", "amount": 12.50},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    assert len(result["matches"]) == 1


def test_unmatched_transactions_hide_internal_keys():
    receipts = []
    statements = [_statement(10, [
        {"date": "2026-04-30", "description": "X", "amount": 1.0},
    ])]
    result = match_receipts_to_statements(receipts, statements)
    txn = result["unmatched_transactions"][0]["transaction"]
    assert "_statement_id" not in txn
    assert "_txn_index" not in txn
