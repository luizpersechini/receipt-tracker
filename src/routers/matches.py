"""Match-trigger and match-listing endpoints."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import database as db
from ..services.matching_engine import match_receipts_to_statements

router = APIRouter(prefix="/api", tags=["matches"])
logger = logging.getLogger(__name__)


class ManualMatchRequest(BaseModel):
    receipt_id: int
    statement_id: int
    transaction_index: int


@router.post("/match")
async def run_match():
    """Match corp-card receipts against statement transactions.

    Manual matches are preserved. We:
      1. Find every manual match across all statements.
      2. Wipe ONLY the auto-matches (manual rows stay put).
      3. Exclude manually-matched receipts from the matching pool.
      4. Tell the matching engine which transaction_indices to skip per
         statement so it can't re-match a txn that's already manually
         paired to something else.
      5. Reset only receipts that AREN'T manually matched.
    """
    try:
        all_receipts, statements = await asyncio.gather(
            asyncio.to_thread(db.get_all_receipts),
            asyncio.to_thread(db.get_all_statements),
        )
        receipts = [r for r in all_receipts if r.get("expense_type") != "reimbursement"]

        if not receipts:
            return JSONResponse({"success": False, "message": "No corp card receipts to match"})
        if not statements:
            return JSONResponse({"success": False, "message": "No statement uploaded yet"})

        # Collect manual locks BEFORE wiping auto-matches.
        manual_receipt_ids: set[int] = set()
        # statement_id → set of locked transaction_indices
        manual_locked_txns: dict[int, set[int]] = {}
        for statement in statements:
            ms = await asyncio.to_thread(db.get_matches_for_statement, statement["id"])
            for m in ms:
                if m.get("match_kind") == "manual":
                    manual_receipt_ids.add(m["receipt_id"])
                    if m.get("transaction_index") is not None:
                        manual_locked_txns.setdefault(statement["id"], set()).add(
                            m["transaction_index"]
                        )

        for statement in statements:
            await asyncio.to_thread(db.clear_auto_matches_for_statement, statement["id"])
        # Reset match_status only for receipts NOT pinned by a manual match.
        for receipt in receipts:
            if receipt["id"] not in manual_receipt_ids:
                await asyncio.to_thread(db.update_receipt_match_status, receipt["id"], "pending")

        eligible_receipts = [r for r in receipts if r["id"] not in manual_receipt_ids]
        result = await asyncio.to_thread(
            match_receipts_to_statements,
            eligible_receipts,
            statements,
            manual_locked_txns,
        )

        for match in result["matches"]:
            # Pass `transaction_index` so the reconcile board can anchor
            # this match to a specific row in the statement (otherwise it
            # renders as if nothing is paired — see audit notes).
            await asyncio.to_thread(
                db.save_match,
                match["receipt_id"],
                match["statement_id"],
                match["transaction_data"],
                match["match_score"],
                match_kind="auto",
                transaction_index=match.get("transaction_index"),
            )
            await asyncio.to_thread(db.update_receipt_match_status, match["receipt_id"], "matched")

        for receipt in result["unmatched_receipts"]:
            await asyncio.to_thread(db.update_receipt_match_status, receipt["id"], "unmatched")

        return JSONResponse(
            {
                "success": True,
                "matches": result["matches"],
                "unmatched_receipts": [
                    {
                        "id": r["id"],
                        "filename": r["filename"],
                        "metadata": r["metadata"],
                        "description": r["description"],
                        "created_at": r["created_at"],
                    }
                    for r in result["unmatched_receipts"]
                ],
                "unmatched_transactions": result["unmatched_transactions"],
                "total_matched": len(result["matches"]),
                "total_unmatched_receipts": len(result["unmatched_receipts"]),
                "total_unmatched_transactions": len(result["unmatched_transactions"]),
            }
        )
    except Exception as e:
        logger.exception("run_match failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/matches")
async def get_matches():
    matches = await asyncio.to_thread(db.get_all_matches)
    return JSONResponse({"matches": matches})


@router.get("/stats")
async def get_stats():
    try:
        stats = await asyncio.to_thread(db.get_statistics)
        return JSONResponse(stats)
    except Exception as e:
        logger.exception("get_stats failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Reconcile board endpoints ─────────────────────────────────────────────


def _receipt_metadata(rcpt_metadata_json: str) -> dict:
    try:
        return json.loads(rcpt_metadata_json or "{}")
    except json.JSONDecodeError:
        return {}


@router.get("/statements/{statement_id}/transactions")
async def get_statement_transactions(statement_id: int):
    """Return one statement's parsed transactions, each annotated with its
    current match (if any). Drives the reconcile board's left pane."""
    statement = await asyncio.to_thread(db.get_statement_by_id, statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")

    matches = await asyncio.to_thread(db.get_matches_for_statement, statement_id)
    # Group matches by transaction_index — N:1 is allowed now (multiple
    # receipts cumulatively pay one statement charge, e.g. hotel folio
    # split into room + restaurant + parking receipts).
    matches_by_idx: dict[int, list] = {}
    for m in matches:
        idx = m.get("transaction_index")
        if idx is None:
            continue
        matches_by_idx.setdefault(idx, []).append(m)

    txns = []
    for i, t in enumerate(statement.get("transactions") or []):
        raw = t.get("raw_data") or {}
        native_ccy = raw.get("original_currency")
        # `amount` is always the BRL bill total (that's what the statement
        # actually charges). For international rows the parser also captured
        # the native currency amount + the FX rate — surface those so the
        # UI can show both and the matcher can compare receipts in their
        # original currency.
        ms = matches_by_idx.get(i) or []
        # Stable sort by match id so older pairings render first.
        ms.sort(key=lambda m: m.get("id") or 0)
        match_list = []
        for m in ms:
            rmeta = _receipt_metadata(m.get("receipt_metadata"))
            match_list.append(
                {
                    "id": m["id"],
                    "receipt_id": m["receipt_id"],
                    "receipt_vendor": rmeta.get("vendor_name"),
                    "receipt_amount": rmeta.get("total_amount"),
                    "receipt_currency": rmeta.get("currency"),
                    "score": m.get("match_score"),
                    "kind": m.get("match_kind") or "auto",
                }
            )
        meta = {
            "index": i,
            "date": t.get("date"),
            "description": t.get("description"),
            "amount": t.get("amount"),
            "currency": "BRL",
            "category": t.get("category"),
            "city": raw.get("city"),
            "original_amount": raw.get("original_amount"),
            "original_currency": native_ccy if native_ccy and native_ccy != "BRL" else None,
            "fx_to_brl": raw.get("fx_to_brl"),
            # `matches` is the new shape; `match` keeps the first entry for
            # any older code paths that hadn't migrated. New consumers
            # should read `matches`.
            "matches": match_list,
            "match": match_list[0] if match_list else None,
        }
        txns.append(meta)

    return JSONResponse(
        {
            "statement": {
                "id": statement["id"],
                "filename": statement["filename"],
                "transaction_count": len(statement.get("transactions") or []),
            },
            "transactions": txns,
        }
    )


@router.post("/matches")
async def create_manual_match(body: ManualMatchRequest):
    """Manual drag-drop pair of one receipt to one statement transaction.
    Refuses if either side is already matched on this transaction index."""
    # Validate both ends exist.
    statement = await asyncio.to_thread(db.get_statement_by_id, body.statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    transactions = statement.get("transactions") or []
    if body.transaction_index < 0 or body.transaction_index >= len(transactions):
        raise HTTPException(status_code=400, detail="transaction_index out of range")

    receipt = await asyncio.to_thread(db.get_receipt_by_id, body.receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    # A transaction CAN take multiple receipts (cumulative pairing — e.g.
    # one hotel folio charge split across room + restaurant + parking
    # receipts). But a single receipt is still 1:1 — pairing it against
    # two transactions would double-count it on reports.
    existing = await asyncio.to_thread(db.get_matches_for_statement, body.statement_id)
    if any(m.get("receipt_id") == body.receipt_id for m in existing):
        raise HTTPException(
            status_code=409,
            detail="That receipt is already matched on this statement.",
        )

    txn = transactions[body.transaction_index]
    match_id = await asyncio.to_thread(
        db.save_match,
        body.receipt_id,
        body.statement_id,
        txn,
        None,  # match_score (manual pairs have no auto-score)
        match_kind="manual",
        transaction_index=body.transaction_index,
    )
    await asyncio.to_thread(db.update_receipt_match_status, body.receipt_id, "matched")
    return JSONResponse({"id": match_id, "kind": "manual", "score": None})


@router.delete("/matches/{match_id}", status_code=204)
async def delete_match(match_id: int):
    """Unlink a match (auto or manual). Flips the linked receipt back to
    'pending' so it shows up on the board's right pane again."""
    match = await asyncio.to_thread(db.get_match_by_id, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    await asyncio.to_thread(db.delete_match_by_id, match_id)
    # Flip the receipt — only if it has no other matches across statements.
    receipt_id = match["receipt_id"]
    remaining = await asyncio.to_thread(db.get_all_matches)
    if not any(m["receipt_id"] == receipt_id for m in remaining):
        await asyncio.to_thread(db.update_receipt_match_status, receipt_id, "pending")
    return JSONResponse(None, status_code=204)
