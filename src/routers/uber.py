"""Bulk-import Uber trip-history CSVs as receipts (no OCR)."""

import asyncio
import logging
import shutil
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import database as db
from ..services.uber_csv import is_uber_csv, parse_uber_csv

router = APIRouter(prefix="/api", tags=["uber"])
logger = logging.getLogger(__name__)


@router.post("/upload/uber-csv")
async def upload_uber_csv(
    file: UploadFile = File(...),
    business_only: bool = Query(
        False, description="Skip rides that don't look like Business Profile rides"
    ),
    after: Optional[str] = Query(
        None, description="Only import rides on/after this date (YYYY-MM-DD)"
    ),
    before: Optional[str] = Query(
        None, description="Only import rides on/before this date (YYYY-MM-DD)"
    ),
):
    """
    Upload an Uber rider trip-history CSV (the file Uber emails you when you
    request your data at uber.com/privacy/downloadyourdata). Each row becomes
    a receipt entry — no OCR, no model load.

    Re-uploading the same CSV is safe: rides already imported are skipped
    based on trip-id dedup.
    """
    try:
        # Persist the file so we can parse it from disk
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"uber_{timestamp}_{file.filename}"
        filepath = settings.statements_dir / filename
        with open(filepath, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        if not await asyncio.to_thread(is_uber_csv, str(filepath)):
            raise HTTPException(
                status_code=400,
                detail=(
                    "This doesn't look like an Uber rider trip-history CSV. "
                    "Get yours at uber.com/privacy/downloadyourdata."
                ),
            )

        rows = await asyncio.to_thread(
            parse_uber_csv,
            str(filepath),
            business_only=business_only,
            after=after,
            before=before,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("upload_uber_csv failed")
        raise HTTPException(status_code=500, detail=str(e))

    imported = 0
    skipped_dup = 0
    skipped_cross_source = 0
    errors: list[str] = []

    for row in rows:
        synthetic_name = row.synthetic_filename()
        existing = await asyncio.to_thread(db.get_receipt_by_filename, synthetic_name)
        if existing:
            skipped_dup += 1
            continue

        # Cross-source dedup: same ride may already be in the DB from Gmail.
        # Prefer the exact trip-id link (same synthetic stem, .pdf/.eml ext);
        # fall back to (date, amount, currency) — restricted to GMAIL rows,
        # otherwise the second of two legitimate same-day same-fare rides in
        # this very CSV would dedupe against the first and silently vanish.
        stem = synthetic_name.rsplit(".", 1)[0]
        cross = None
        for ext in (".pdf", ".eml"):
            cross = await asyncio.to_thread(db.get_receipt_by_filename, stem + ext)
            if cross:
                break
        if not cross:
            cross = await asyncio.to_thread(
                db.find_uber_receipt_by_date_amount,
                row.date,
                row.fare,
                row.currency,
                0.01,
                "gmail",
            )
        if cross:
            skipped_cross_source += 1
            continue

        try:
            await asyncio.to_thread(
                db.save_receipt,
                synthetic_name,
                str(filepath),
                row.to_metadata(),
                f"Uber · {row.begin_address or ''} → {row.end_address or ''}".strip(" ·→"),
            )
            imported += 1
        except Exception as e:  # don't let one bad row kill the import
            logger.exception("uber import: row %s failed", row.trip_id)
            errors.append(f"{row.trip_id}: {e}")

    return JSONResponse(
        {
            "success": True,
            "imported": imported,
            "skipped_duplicates": skipped_dup,
            "skipped_cross_source": skipped_cross_source,
            "parsed_rows": len(rows),
            "errors": errors,
            "business_only": business_only,
        }
    )
