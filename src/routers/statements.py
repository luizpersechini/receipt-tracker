"""Statement upload, listing, and deletion endpoints."""

import asyncio
import logging
import shutil
from datetime import datetime

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import database as db
from ..services.statement_parser import parse_statement

router = APIRouter(prefix="/api", tags=["statements"])
logger = logging.getLogger(__name__)


@router.post("/upload/statement")
async def upload_statement(file: UploadFile = File(...)):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"statement_{timestamp}_{file.filename}"
        filepath = settings.statements_dir / filename

        with open(filepath, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        # Dispatcher picks PDF vs CSV based on extension; same return shape.
        transactions = await asyncio.to_thread(parse_statement, str(filepath))
        statement_id = await asyncio.to_thread(
            db.save_statement,
            filename,
            str(filepath),
            transactions,
        )

        return JSONResponse(
            {
                "success": True,
                "statement_id": statement_id,
                "filename": filename,
                "transaction_count": len(transactions),
            }
        )
    except Exception as e:
        logger.exception("upload_statement failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/statements")
async def get_statements():
    statements = await asyncio.to_thread(db.get_all_statements)
    return JSONResponse({"statements": statements})


@router.delete("/statements/{statement_id}")
async def remove_statement(statement_id: int):
    try:
        await asyncio.to_thread(db.delete_statement, statement_id)
        from ..services import statement_categories, statement_notes

        await asyncio.to_thread(statement_notes.clear_statement, statement_id)
        await asyncio.to_thread(statement_categories.clear_statement, statement_id)
        return JSONResponse({"success": True})
    except Exception as e:
        logger.exception("delete statement failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/statements/{statement_id}/reparse")
async def reparse_statement(statement_id: int):
    """Re-run the parser over the saved file and overwrite the stored
    transactions. Useful after a parser bug fix — saves the user having to
    delete + re-upload (and lose any manual matches)."""
    statement = await asyncio.to_thread(db.get_statement_by_id, statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    filepath = statement.get("filepath")
    if not filepath:
        raise HTTPException(status_code=400, detail="Statement has no saved file path")
    try:
        transactions = await asyncio.to_thread(parse_statement, filepath)
        await asyncio.to_thread(db.replace_statement_transactions, statement_id, transactions)
    except Exception as e:
        logger.exception("reparse_statement failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, "transaction_count": len(transactions)})
