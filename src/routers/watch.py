"""Watch-folder status + on-demand scan endpoints."""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..services.folder_watcher import (
    count_pending,
    get_watch_log,
    resolve_inbox,
    scan_folder,
)

router = APIRouter(prefix="/api/watch-folder", tags=["watch"])

# Guard so two concurrent scan requests can't double-process the inbox.
_scan_lock = asyncio.Lock()


@router.get("/status")
async def watch_folder_status():
    inbox = await asyncio.to_thread(resolve_inbox)
    pending = await asyncio.to_thread(count_pending)
    return JSONResponse(
        {
            "enabled": True,
            "folder": str(inbox),
            "pending": pending,
            "last_processed": get_watch_log(10),
        }
    )


@router.post("/scan")
async def watch_folder_scan():
    """Process every supported file currently in the inbox. Sequential —
    OCR is CPU/Neural-Engine heavy, so we don't fan out."""
    if _scan_lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    async with _scan_lock:
        try:
            summary = await asyncio.to_thread(scan_folder)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **summary})
