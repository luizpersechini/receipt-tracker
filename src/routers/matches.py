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
    """Match corp-card receipts against statement transactions."""
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

        for statement in statements:
            await asyncio.to_thread(db.clear_matches_for_statement, statement["id"])
        for receipt in receipts:
            await asyncio.to_thread(db.update_receipt_match_status, receipt["id"], "pending")

        result = await asyncio.to_thread(match_receipts_to_statements, receipts, statements)

        for match in result["matches"]:
            await asyncio.to_thread(
                db.save_match,
                match["receipt_id"],
                match["statement_id"],
                match["transaction_data"],
                match["match_score"],
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
    # Index matches by transaction_index so the lookup below is O(1).
    matches_by_idx = {
        m["transaction_index"]: m for m in matches if m.get("transaction_index") is not None
    }

    txns = []
    for i, t in enumerate(statement.get("transactions") or []):
        raw = t.get("raw_data") or {}
        native_ccy = raw.get("original_currency")
        # `amount` is always the BRL bill total (that's what the statement
        # actually charges). For international rows the parser also captured
        # the native currency amount + the FX rate — surface those so the
        # UI can show both and the matcher can compare receipts in their
        # original currency.
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
            "match": None,
        }
        m = matches_by_idx.get(i)
        if m:
            rmeta = _receipt_metadata(m.get("receipt_metadata"))
            meta["match"] = {
                "id": m["id"],
                "receipt_id": m["receipt_id"],
                "receipt_vendor": rmeta.get("vendor_name"),
                "receipt_amount": rmeta.get("total_amount"),
                "receipt_currency": rmeta.get("currency"),
                "score": m.get("match_score"),
                "kind": m.get("match_kind") or "auto",
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

    # Reject duplicate: that specific transaction slot already has a match.
    existing = await asyncio.to_thread(db.get_matches_for_statement, body.statement_id)
    if any(m.get("transaction_index") == body.transaction_index for m in existing):
        raise HTTPException(
            status_code=409,
            detail="That transaction is already matched. Unlink it first.",
        )
    # And the receipt itself can only be matched once.
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
