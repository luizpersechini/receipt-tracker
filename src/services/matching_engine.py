"""Matching engine for receipts and statement transactions."""

import json
from datetime import datetime
from difflib import SequenceMatcher


def match_receipts_to_statements(receipts: list, statements: list) -> dict:
    """
    Match receipts to statement transactions using fuzzy logic.

    Returns:
      {
        matches: [...],
        unmatched_receipts: [...],
        unmatched_transactions: [...],
      }
    """
    all_transactions = []
    for statement in statements:
        for txn in statement.get("transactions", []):
            all_transactions.append({**txn, "_statement_id": statement["id"]})

    matched_receipt_ids: set = set()
    matched_txn_indices: set = set()
    matches: list = []

    for receipt in receipts:
        meta = receipt.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        elif meta is None:
            meta = {}

        best_score = 0.0
        best_idx = None

        for idx, txn in enumerate(all_transactions):
            if idx in matched_txn_indices:
                continue
            score = _score(meta, txn)
            if score > best_score:
                best_score = score
                best_idx = idx

        # 0.5 allows amount+date hit even without vendor name
        if best_idx is not None and best_score >= 0.5:
            txn = all_transactions[best_idx]
            matched_receipt_ids.add(receipt["id"])
            matched_txn_indices.add(best_idx)
            matches.append(
                {
                    "receipt_id": receipt["id"],
                    "receipt_filename": receipt["filename"],
                    "receipt_date": meta.get("date"),
                    "receipt_vendor": meta.get("vendor_name", ""),
                    "receipt_amount": meta.get("total_amount"),
                    "statement_id": txn["_statement_id"],
                    "transaction_date": txn.get("date"),
                    "transaction_description": txn.get("description", ""),
                    "transaction_amount": txn.get("amount"),
                    "match_score": round(best_score, 3),
                    "transaction_data": {k: v for k, v in txn.items() if k != "_statement_id"},
                }
            )

    unmatched_receipts = [r for r in receipts if r["id"] not in matched_receipt_ids]
    unmatched_transactions = [
        {
            "transaction": {k: v for k, v in txn.items() if k != "_statement_id"},
            "statement_id": txn["_statement_id"],
        }
        for idx, txn in enumerate(all_transactions)
        if idx not in matched_txn_indices
    ]

    return {
        "matches": matches,
        "unmatched_receipts": unmatched_receipts,
        "unmatched_transactions": unmatched_transactions,
    }


def _score(meta: dict, txn: dict) -> float:
    """Weighted score: amount 50%, date 30%, vendor 20%.

    Currency-aware amount comparison: an international transaction on the
    statement carries both the BRL bill amount AND the native amount (CAD,
    USD, etc) in `raw_data`. Receipts are always recorded in their original
    currency. If the receipt's currency matches the txn's native currency,
    compare native-to-native — otherwise the BRL bill total wildly differs
    from the receipt amount and the match never fires.
    """
    receipt_amount = meta.get("total_amount")
    receipt_currency = (meta.get("currency") or "").upper() or None
    txn_amount = txn.get("amount")
    txn_currency = "BRL"

    raw = txn.get("raw_data") or {}
    native_ccy = (raw.get("original_currency") or "").upper() or None
    native_amount = raw.get("original_amount")

    # Prefer native-vs-native comparison when both ends agree on the
    # currency (e.g. CAD receipt + CAD original_amount on the statement).
    if (
        receipt_currency
        and native_ccy
        and native_amount is not None
        and receipt_currency == native_ccy
    ):
        txn_amount = native_amount
        txn_currency = native_ccy

    if receipt_amount and txn_amount:
        diff = abs(float(receipt_amount) - float(txn_amount))
        tolerance = max(0.02, abs(float(txn_amount)) * 0.01)
        amount_score = max(0.0, 1.0 - diff / tolerance) if diff <= tolerance * 5 else 0.0
    else:
        amount_score = 0.0
    # Currency-mismatch penalty: if both sides declare a currency and they
    # disagree, we can't trust the amount score even if the numbers happen
    # to land close (rare but real — 31 BRL vs 31 CAD shouldn't pair).
    if receipt_currency and txn_currency and receipt_currency != txn_currency:
        amount_score *= 0.3

    receipt_date = meta.get("date")
    txn_date = txn.get("date")

    if receipt_date and txn_date:
        try:
            rd = datetime.strptime(receipt_date, "%Y-%m-%d")
            td = datetime.strptime(txn_date, "%Y-%m-%d")
            diff_days = abs((rd - td).days)
            # Corporate card transactions may post up to 5 days after purchase
            if diff_days == 0:
                date_score = 1.0
            elif diff_days <= 5:
                date_score = 1.0 - diff_days / 6.0
            else:
                date_score = 0.0
        except ValueError:
            date_score = 0.0
    else:
        date_score = 0.0

    vendor = (meta.get("vendor_name") or "").lower().strip()
    desc = (txn.get("description") or "").lower().strip()

    if vendor and desc:
        if vendor in desc or desc in vendor:
            vendor_score = 1.0
        else:
            vendor_tokens = set(vendor.split())
            desc_tokens = set(desc.split())
            overlap = vendor_tokens & desc_tokens
            if overlap:
                vendor_score = len(overlap) / max(len(vendor_tokens), len(desc_tokens))
            else:
                vendor_score = SequenceMatcher(None, vendor, desc).ratio() * 0.5
    else:
        vendor_score = 0.0

    return amount_score * 0.50 + date_score * 0.30 + vendor_score * 0.20
