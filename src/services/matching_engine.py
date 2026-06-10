"""Matching engine for receipts and statement transactions."""

import json
from datetime import datetime
from difflib import SequenceMatcher


def match_receipts_to_statements(
    receipts: list,
    statements: list,
    locked_txns: dict | None = None,
) -> dict:
    """
    Match receipts to statement transactions using fuzzy logic.

    `locked_txns` maps statement_id → set of transaction_index values that
    are already manually matched and must not be re-paired here. The
    matching engine skips them so a manual pairing never gets clobbered by
    a subsequent auto-match run.

    Returns:
      {
        matches: [...],
        unmatched_receipts: [...],
        unmatched_transactions: [...],
      }
    """
    locked_txns = locked_txns or {}
    all_transactions = []
    for statement in statements:
        # Preserve the txn's position inside its parent statement so the
        # match row can persist `transaction_index`. Without it, the
        # reconcile board can't anchor a match to a specific row.
        locked = locked_txns.get(statement["id"], set())
        for txn_index, txn in enumerate(statement.get("transactions", [])):
            if txn_index in locked:
                continue  # already paired manually — don't offer it
            try:
                if float(txn.get("amount") or 0) < 0:
                    # Credit/estorno — money coming back, no receipt expected.
                    # Receipts are positive, so the amount can never support a
                    # match, but exact date + identical vendor alone reaches
                    # the 0.5 gate and would pair the refund with the original
                    # purchase's receipt.
                    continue
            except (TypeError, ValueError):
                pass
            all_transactions.append(
                {
                    **txn,
                    "_statement_id": statement["id"],
                    "_txn_index": txn_index,
                }
            )

    metas: list = []
    for receipt in receipts:
        meta = receipt.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        elif meta is None:
            meta = {}
        metas.append(meta)

    # Score every (receipt, txn) pair, then assign best-first GLOBALLY.
    # Per-receipt greedy assignment let an early receipt steal a transaction
    # from a later receipt that matched it far better.
    pairs: list = []
    for r_i, receipt in enumerate(receipts):
        meta = metas[r_i]
        for t_i, txn in enumerate(all_transactions):
            amount_s, date_s, vendor_s = _score_parts(meta, txn)
            total = amount_s * 0.50 + date_s * 0.30 + vendor_s * 0.20
            # Gate: an exact amount alone scores 0.5 (plus fuzzy-vendor
            # noise) and used to clear the threshold — round amounts are
            # common enough that unrelated receipts months apart paired up.
            # Require date support, or a genuinely similar vendor for
            # receipts with no usable date.
            if total >= 0.5 and (date_s > 0 or vendor_s >= 0.5):
                pairs.append((total, r_i, t_i))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    matched_receipt_ids: set = set()
    matched_txn_indices: set = set()
    used_receipts: set = set()
    matches: list = []

    for total, r_i, t_i in pairs:
        if r_i in used_receipts or t_i in matched_txn_indices:
            continue
        receipt = receipts[r_i]
        meta = metas[r_i]
        txn = all_transactions[t_i]
        used_receipts.add(r_i)
        matched_receipt_ids.add(receipt["id"])
        matched_txn_indices.add(t_i)
        matches.append(
            {
                "receipt_id": receipt["id"],
                "receipt_filename": receipt["filename"],
                "receipt_date": meta.get("date"),
                "receipt_vendor": meta.get("vendor_name", ""),
                "receipt_amount": meta.get("total_amount"),
                "statement_id": txn["_statement_id"],
                "transaction_index": txn["_txn_index"],
                "transaction_date": txn.get("date"),
                "transaction_description": txn.get("description", ""),
                "transaction_amount": txn.get("amount"),
                "match_score": round(total, 3),
                "transaction_data": {
                    k: v for k, v in txn.items() if k not in ("_statement_id", "_txn_index")
                },
            }
        )
    # Keep statement order stable for consumers that render the match list.
    matches.sort(key=lambda m: (m["statement_id"], m["transaction_index"]))

    unmatched_receipts = [r for r in receipts if r["id"] not in matched_receipt_ids]
    unmatched_transactions = [
        {
            "transaction": {
                k: v for k, v in txn.items() if k not in ("_statement_id", "_txn_index")
            },
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
    """Weighted total — kept for tests/back-compat; see _score_parts."""
    amount_s, date_s, vendor_s = _score_parts(meta, txn)
    return amount_s * 0.50 + date_s * 0.30 + vendor_s * 0.20


def _score_parts(meta: dict, txn: dict) -> tuple:
    """Component scores (amount, date, vendor), weighted 50/30/20 by _score.

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

    return amount_score, date_score, vendor_score
