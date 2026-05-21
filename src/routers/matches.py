"""Match-trigger and match-listing endpoints."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..db import database as db
from ..services.matching_engine import match_receipts_to_statements

router = APIRouter(prefix="/api", tags=["matches"])
logger = logging.getLogger(__name__)


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
                match["receipt_id"], match["statement_id"],
                match["transaction_data"], match["match_score"],
            )
            await asyncio.to_thread(db.update_receipt_match_status, match["receipt_id"], "matched")

        for receipt in result["unmatched_receipts"]:
            await asyncio.to_thread(db.update_receipt_match_status, receipt["id"], "unmatched")

        return JSONResponse({
            "success": True,
            "matches": result["matches"],
            "unmatched_receipts": [
                {"id": r["id"], "filename": r["filename"], "metadata": r["metadata"],
                 "description": r["description"], "created_at": r["created_at"]}
                for r in result["unmatched_receipts"]
            ],
            "unmatched_transactions": result["unmatched_transactions"],
            "total_matched": len(result["matches"]),
            "total_unmatched_receipts": len(result["unmatched_receipts"]),
            "total_unmatched_transactions": len(result["unmatched_transactions"]),
        })
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
