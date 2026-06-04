"""Box-review queue endpoints — drain the pending multi-receipt pages.

Pages queued by a PDF upload (every page) or a folder scan (multi-receipt
pages) are reviewed here: the user confirms/adjusts the boxes, then commits to
run OCR (one receipt per box) and drop the page from the queue.
"""

import asyncio
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, Response

from ..config import settings
from ..services import review_queue
from .receipts import commit_boxes_to_receipts

router = APIRouter(prefix="/api", tags=["review"])
logger = logging.getLogger(__name__)


@router.get("/review-queue")
async def get_review_queue():
    """Pages awaiting box review, each with its page image + suggested boxes."""
    items = await asyncio.to_thread(review_queue.list_items)
    out = [
        {
            "id": it["id"],
            "image_url": f"/api/preview/{quote(it['filename'])}",
            "image_size": {"w": it["image_w"], "h": it["image_h"]},
            "bboxes": it.get("bboxes") or [],
            "source": it.get("source") or "",
            "description": it.get("description"),
        }
        for it in items
    ]
    return JSONResponse({"items": out, "count": len(out)})


@router.post("/review-queue/{item_id}/commit")
async def commit_review_item(item_id: int, body: dict = Body(...)):
    """OCR the user-confirmed boxes for one queued page and drop it from the
    queue. Body: `{bboxes: [{x,y,w,h}, ...]}`."""
    item = await asyncio.to_thread(review_queue.get, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    bboxes = body.get("bboxes") if isinstance(body, dict) else None
    if not bboxes:
        raise HTTPException(status_code=400, detail="No boxes provided")
    saved = await commit_boxes_to_receipts(item["filename"], bboxes, item.get("description"))
    await asyncio.to_thread(review_queue.remove, item_id)
    return JSONResponse({"success": True, "count": len(saved), "receipts": saved})


@router.delete("/review-queue/{item_id}", status_code=204)
async def discard_review_item(item_id: int):
    """Drop a queued page without OCR; best-effort removal of its page image."""
    item = await asyncio.to_thread(review_queue.get, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    await asyncio.to_thread(review_queue.remove, item_id)
    try:
        p = (settings.receipts_dir / item["filename"]).resolve()
        if p.exists() and p.is_relative_to(settings.receipts_dir.resolve()):
            os.remove(p)
    except Exception as e:  # pragma: no cover — cleanup is best-effort
        logger.warning("review discard: couldn't remove %s: %s", item.get("filename"), e)
    return Response(status_code=204)
