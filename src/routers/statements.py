"""Statement upload, listing, and deletion endpoints."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import database as db
from ..services.statement_parser import build_index_mapping, parse_statement
from ..services.uploads import safe_name, save_upload

router = APIRouter(prefix="/api", tags=["statements"])
logger = logging.getLogger(__name__)


@router.post("/upload/statement")
async def upload_statement(file: UploadFile = File(...)):
    filepath = None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"statement_{timestamp}_{safe_name(file.filename)}"
        filepath = settings.statements_dir / filename
        await save_upload(file, filepath)

        # Dispatcher picks PDF vs CSV based on extension; same return shape.
        transactions = await asyncio.to_thread(parse_statement, str(filepath))
        statement_id = await asyncio.to_thread(
            db.save_statement,
            filename,
            str(filepath),
            transactions,
        )

        payload = {
            "success": True,
            "statement_id": statement_id,
            "filename": filename,
            "transaction_count": len(transactions),
        }
        # A 0-transaction PDF used to be indistinguishable from a good upload
        # in the UI. Diagnose why (scan vs unrecognized layout) and say so.
        if not transactions and filepath.suffix.lower() == ".pdf":
            from ..services.statement_pdf_parser import zero_parse_warning

            payload["warning"] = await asyncio.to_thread(zero_parse_warning, str(filepath))

        return JSONResponse(payload)
    except HTTPException:
        raise  # e.g. 413 from save_upload — don't flatten to 500
    except Exception as e:
        logger.exception("upload_statement failed")
        if filepath is not None:  # parse failed — don't leave the orphan file
            filepath.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/statements")
async def get_statements():
    statements = await asyncio.to_thread(db.get_all_statements)
    return JSONResponse({"statements": statements})


@router.delete("/statements/{statement_id}")
async def remove_statement(statement_id: int):
    try:
        await asyncio.to_thread(db.delete_statement, statement_id)
        from ..services import (
            statement_categories,
            statement_costcenters,
            statement_labels,
            statement_notes,
        )

        await asyncio.to_thread(statement_notes.clear_statement, statement_id)
        await asyncio.to_thread(statement_categories.clear_statement, statement_id)
        await asyncio.to_thread(statement_labels.clear_statement, statement_id)
        await asyncio.to_thread(statement_costcenters.clear_statement, statement_id)
        return JSONResponse({"success": True})
    except Exception as e:
        logger.exception("delete statement failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/statements/{statement_id}/archive")
async def archive_statement(statement_id: int, body: dict = Body(default={})):
    """Mark a statement complete (archived, hidden from the active list) or
    reopen it. Body: `{"archived": true|false}` (defaults to true). The
    attached receipts are left as-is — they're already 'matched' and hidden
    from the active receipts board."""
    archived = bool(body.get("archived", True)) if isinstance(body, dict) else True
    changed = await asyncio.to_thread(db.set_statement_archived, statement_id, archived)
    if not changed:
        raise HTTPException(status_code=404, detail="Statement not found")
    return JSONResponse({"success": True, "archived": archived})


@router.post("/statements/{statement_id}/reparse")
async def reparse_statement(statement_id: int):
    """Re-run the parser over the saved file and overwrite the stored
    transactions. Useful after a parser bug fix — saves the user having to
    delete + re-upload (and lose any manual matches).

    Matches and per-row overrides (notes/labels/categories/cost centers) are
    keyed by transaction index, so when the reparse changes row order/count
    they are REMAPPED by content (date+amount+description). Rows that no
    longer exist drop their matches/overrides instead of silently
    re-anchoring to a different charge; affected receipts go back to
    'pending'."""
    statement = await asyncio.to_thread(db.get_statement_by_id, statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    filepath = statement.get("filepath")
    if not filepath:
        raise HTTPException(status_code=400, detail="Statement has no saved file path")
    try:
        old_txns = statement.get("transactions") or []
        transactions = await asyncio.to_thread(parse_statement, filepath)
        await asyncio.to_thread(db.replace_statement_transactions, statement_id, transactions)

        mapping = build_index_mapping(old_txns, transactions)
        identity = len(old_txns) == len(transactions) and all(
            mapping.get(i) == i for i in range(len(old_txns))
        )
        remap_stats = {"matches_kept": 0, "matches_dropped": 0, "receipts_released": 0}
        if not identity:
            from ..services import (
                statement_categories,
                statement_costcenters,
                statement_labels,
                statement_notes,
            )

            remap_stats = await asyncio.to_thread(
                db.remap_matches_for_statement, statement_id, mapping
            )
            for store in (
                statement_notes,
                statement_labels,
                statement_categories,
                statement_costcenters,
            ):
                await asyncio.to_thread(store.remap_statement, statement_id, mapping)
    except Exception as e:
        logger.exception("reparse_statement failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(
        {
            "success": True,
            "transaction_count": len(transactions),
            "reindexed": not identity,
            **remap_stats,
        }
    )
