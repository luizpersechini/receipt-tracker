"""Receipt Tracker — FastAPI app entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ensure_dirs, settings
from .db.database import init_db
from .routers import (
    fx,
    gmail,
    matches,
    ocr,
    preview,
    receipts,
    reports,
    review,
    statements,
    uber,
    watch,
)
from .services.folder_watcher import start_watcher, stop_watcher
from .services.llm_processor import warmup as warmup_ocr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.watch_folder:
        start_watcher(settings.watch_folder)
    if settings.ocr_warmup:
        # Run in a thread so the (slow) model load doesn't block the event
        # loop during startup. Server is ready as soon as warmup is dispatched.
        import asyncio

        asyncio.create_task(asyncio.to_thread(warmup_ocr))
    try:
        yield
    finally:
        stop_watcher()


ensure_dirs()

app = FastAPI(title="Receipt Tracker", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/receipts", StaticFiles(directory=str(settings.receipts_dir)), name="receipts")

app.include_router(receipts.router)
app.include_router(statements.router)
app.include_router(uber.router)
app.include_router(gmail.router)
app.include_router(matches.router)
app.include_router(fx.router)
app.include_router(reports.router)
app.include_router(watch.router)
app.include_router(preview.router)
app.include_router(ocr.router)
app.include_router(review.router)


@app.get("/")
async def root():
    # no-cache forces the browser to revalidate index.html on every load, so a
    # new app.js/app.css ?v= bump is always picked up. Without this the browser
    # serves a stale index.html (with the old ?v=) and never fetches the new
    # bundle — the cause of repeated "I changed code but the UI didn't update".
    # The bundles themselves stay cacheable; the ?v= query busts them.
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})
