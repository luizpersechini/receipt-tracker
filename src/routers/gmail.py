"""Gmail-backed Uber receipt sync endpoints."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from ..config import settings
from ..services import gmail_ingest

router = APIRouter(prefix="/api/gmail", tags=["gmail"])
logger = logging.getLogger(__name__)


@router.get("/status")
async def get_status():
    """Report whether Gmail ingestion is set up (credentials + token present)."""
    return JSONResponse(gmail_ingest.status())


@router.post("/credentials")
async def upload_credentials(file: UploadFile = File(...)):
    """
    Replace the stored OAuth client (credentials.json) with a fresh one and
    invalidate any existing token. Use this when the original Google Cloud
    project was deleted or the OAuth client was rotated.

    The uploaded payload must be a Google "Desktop app" OAuth client JSON —
    we validate the `installed` block has the required fields before
    overwriting anything on disk.
    """
    body = await file.read()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid JSON.")

    # Google's downloaded file looks like
    # {"installed":{"client_id":"...","client_secret":"...","redirect_uris":[...]}}
    inst = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(inst, dict) or not inst.get("client_id") or not inst.get("client_secret"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Doesn't look like a Desktop OAuth client JSON — missing "
                "`installed.client_id` / `client_secret`. Download the file "
                "via Google Cloud Console → APIs & Services → Credentials."
            ),
        )

    cred_path = settings.gmail_credentials_path
    token_path = settings.gmail_token_path
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_bytes(body)

    # The old token is tied to the old client_id — drop it so the next
    # auth/start call re-runs the full consent flow.
    if token_path.exists():
        try:
            token_path.unlink()
        except OSError as e:
            logger.warning("Could not remove stale token at %s: %s", token_path, e)

    return JSONResponse({
        "success": True,
        "credentials_path": str(cred_path),
        "token_cleared": True,
        "client_id": inst.get("client_id"),
    })


@router.post("/auth/start")
async def auth_start():
    """
    Trigger Google's installed-app OAuth consent flow.

    Opens the user's default browser to Google's consent screen, then writes
    the resulting token to disk. This endpoint blocks for up to 5 minutes
    waiting for the user to finish consenting; if they don't, returns an
    error.
    """
    if not settings.gmail_credentials_path.exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"credentials.json not found at {settings.gmail_credentials_path}. "
                "Download a Desktop OAuth client from Google Cloud Console and "
                "drop the file there first."
            ),
        )
    try:
        result = await asyncio.to_thread(gmail_ingest.run_oauth_flow, 300)
    except Exception as e:
        logger.exception("gmail auth_start crashed")
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "OAuth failed"))
    return JSONResponse(result)


@router.post("/sync")
async def sync(
    business_only: bool = Query(False, description="Skip emails without a Business Profile hint"),
    max_messages: int = Query(100, ge=1, le=500),
    after: Optional[str] = Query(None, description="Only fetch emails on/after this date (YYYY-MM-DD)"),
    before: Optional[str] = Query(None, description="Only fetch emails on/before this date (YYYY-MM-DD)"),
    attach_only: bool = Query(False, description="Only attach PDFs to existing CSV-imported rides; don't create new rows for unmatched emails"),
):
    """
    Fetch and import any unprocessed Uber receipt emails from the connected
    Gmail account. Optionally restrict by date range. Idempotent — already-
    imported messages get the `rt-processed` label and are skipped on
    subsequent runs.
    """
    if not gmail_ingest.is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Gmail isn't connected. Place credentials.json and run "
                "scripts/gmail_auth.py once."
            ),
        )
    try:
        result = await asyncio.to_thread(
            gmail_ingest.sync_new_receipts,
            business_only=business_only,
            max_messages=max_messages,
            after=after,
            before=before,
            attach_only=attach_only,
        )
    except Exception as e:
        logger.exception("gmail sync failed")
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("success"):
        # Surface the categorised error_kind via a structured detail so the
        # UI can pick the right recovery action (reconnect vs new creds).
        raise HTTPException(
            status_code=400,
            detail={
                "error": result.get("error", "Sync failed"),
                "error_kind": result.get("error_kind", "unknown"),
            },
        )
    return JSONResponse(result)


@router.get("/debug")
async def debug_preview(
    max_messages: int = Query(5, ge=1, le=25),
    include_html: bool = Query(False, description="Return up to 8 KB of raw HTML per message"),
):
    """
    Non-destructive parse preview. Returns what the parser sees for the most
    recent matching messages. Doesn't import, doesn't label.
    """
    if not gmail_ingest.is_configured():
        raise HTTPException(status_code=400, detail="Gmail not connected.")
    result = await asyncio.to_thread(
        gmail_ingest.debug_preview, max_messages, include_html,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Debug failed"))
    return JSONResponse(result)


@router.post("/render-pdfs")
async def render_pdfs(max_messages: int = Query(200, ge=1, le=1000)):
    """
    Render PDFs for Gmail-imported receipts that don't yet have one (they
    still have a `gmail://msg_id` pointer instead of a real file). After
    this, their thumbnail in the UI becomes a clickable PDF.
    """
    if not gmail_ingest.is_configured():
        raise HTTPException(status_code=400, detail="Gmail not connected.")
    result = await asyncio.to_thread(gmail_ingest.render_pdfs_for_existing, max_messages)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Render failed"))
    return JSONResponse(result)


@router.post("/reset")
async def reset_labels(only_unimported: bool = Query(True)):
    """
    Remove the rt-processed label from messages so they're re-scanned on the
    next sync. By default skips messages that successfully made it into the
    receipts table — those don't need re-importing.
    """
    if not gmail_ingest.is_configured():
        raise HTTPException(status_code=400, detail="Gmail not connected.")
    result = await asyncio.to_thread(gmail_ingest.reset_processed_label, only_unimported)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Reset failed"))
    return JSONResponse(result)
