"""
Watch-folder ingestion.
Monitors a local directory for new image/PDF files and processes them
through the OCR pipeline, saving results to the DB.

Supported formats: .jpg .jpeg .png .heic .heif .pdf
"""

import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import settings

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf"}

# Subfolders inside the inbox where files land after a scan.
PROCESSED_SUBDIR = "processed"
FAILED_SUBDIR = "failed"

_observer = None

# Serialises ingestion between the watchdog thread and a manual scan so the
# same inbox file can never be processed twice (or OCR'd concurrently).
_ingest_mutex = threading.Lock()


# ─── Inbox resolution ───────────────────────────────────────────────────────


def resolve_inbox() -> Path:
    """Return the folder to scan. Uses settings.watch_folder if configured,
    otherwise defaults to <data_dir>/inbox so the feature works out of the
    box. The folder (and its processed/ subfolder) are created if missing."""
    folder = (settings.watch_folder or "").strip()
    inbox = Path(folder).expanduser() if folder else (settings.data_dir / "inbox")
    inbox = inbox.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / PROCESSED_SUBDIR).mkdir(parents=True, exist_ok=True)
    return inbox


def _pending_files(inbox: Path) -> list:
    """Supported files sitting directly in the inbox (not in processed/ or
    failed/), oldest first so a batch imports in arrival order."""
    out = []
    for p in inbox.iterdir():
        if p.is_dir():
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            out.append(p)
    out.sort(key=_safe_mtime)
    return out


def _safe_mtime(p: Path) -> float:
    # A file can vanish between iterdir() and stat() (watcher moved it).
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def count_pending() -> int:
    try:
        return len(_pending_files(resolve_inbox()))
    except Exception as e:
        logger.warning("count_pending failed: %s", e)
        return 0


def _append_log(entry: dict):
    settings.watch_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.watch_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_watch_log(n: int = 10) -> list:
    """Return the last `n` log entries (newest first). Reads only the file's
    tail — the JSONL log grows forever and used to be slurped whole on every
    status poll."""
    path = settings.watch_log_path
    if not path.exists():
        return []
    tail_bytes = 64 * 1024
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        chunk = f.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # drop the (probably truncated) first line
    entries: list = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= n:
            break
    return entries


def _wait_until_stable(src: Path, timeout: float = 30.0) -> None:
    """Block until the file size stops changing — sync clients (iCloud,
    network shares, phone transfers) write in chunks, and OCR on a truncated
    file produces garbage."""
    last = -1
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            size = src.stat().st_size
        except OSError:
            return
        if size == last and size > 0:
            return
        last = size
        time.sleep(0.5)


def process_new_file(source_path: str) -> dict:
    """Watchdog path: ingest one newly detected inbox file through the SAME
    pipeline as a manual scan (`_ingest_file` — PDF rasterising, segmentation
    and box-review queueing included), then move it to processed/ (failed/ on
    error) so a later scan can never re-import it."""
    src = Path(source_path)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "source": str(src),
        "filename": src.name,
        "status": "error",
        "receipt_id": None,
        "error": None,
    }

    _wait_until_stable(src)

    with _ingest_mutex:
        if not src.exists():
            # A concurrent scan grabbed it first — already handled, stay quiet.
            logger.debug("Watch folder: %s already processed elsewhere", src.name)
            entry["status"] = "skipped"
            entry["error"] = "already processed by a concurrent scan"
            return entry

        suffix = src.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            entry["error"] = f"Unsupported extension: {suffix}"
            _append_log(entry)
            return entry

        try:
            res = _ingest_file(src)
            entry.update(
                {
                    "status": res["status"],
                    "count": res["count"],
                    "receipt_ids": res["receipt_ids"],
                    "receipt_id": res["receipt_ids"][0] if res["receipt_ids"] else None,
                    "vendor": res.get("vendor"),
                    "amount": res.get("amount"),
                    "currency": res.get("currency"),
                    "queued": res.get("queued", 0),
                }
            )
            if res.get("ocr_warning"):
                entry["ocr_warning"] = res["ocr_warning"]
            processed_dir = src.parent / PROCESSED_SUBDIR
            processed_dir.mkdir(parents=True, exist_ok=True)
            _move_into(src, processed_dir)
            logger.info(
                "Watch folder: processed %s → %d receipt(s), %d page(s) queued",
                src.name,
                res["count"],
                res.get("queued", 0),
            )
        except Exception as e:
            entry["error"] = str(e)
            logger.error("Watch folder: failed to process %s: %s", src.name, e)
            failed_dir = src.parent / FAILED_SUBDIR
            failed_dir.mkdir(parents=True, exist_ok=True)
            _move_into(src, failed_dir)

    _append_log(entry)
    return entry


# ─── On-demand folder scan ──────────────────────────────────────────────────


def _ingest_file(src: Path) -> dict:
    """Process one inbox file end-to-end: render (if PDF) → segment → OCR →
    save one DB row per detected receipt (with per-segment crop thumbnails).

    Returns a result dict. Raises nothing — errors are captured in the dict
    so a batch keeps going.
    """
    from ..db.database import save_receipt
    from . import review_queue
    from .llm_processor import process_with_segments
    from .pdf_render import is_pdf, render_pdf_to_images
    from .receipt_segmenter import segment_with_boxes

    result = {
        "filename": src.name,
        "source": str(src),
        "status": "error",
        "count": 0,
        "receipt_ids": [],
        "error": None,
    }

    settings.receipts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1) Materialise the input as one or more image files in receipts_dir.
    #    PDFs are rasterised page-by-page; images are copied verbatim.
    #
    # `is_pdf_source` routes every PDF page to box review below (step 2),
    # matching a direct PDF upload — a scanned PDF page often holds several
    # receipts, so the user confirms the boxes before OCR rather than
    # trusting an auto-split.
    image_paths: list = []
    is_pdf_source = is_pdf(str(src))
    if is_pdf_source:
        pages = render_pdf_to_images(str(src))  # may raise → caught by caller
        stem = src.stem
        for i, page in enumerate(pages, 1):
            page_name = f"receipt_{timestamp}_{stem}_p{i}.png"
            page_path = settings.receipts_dir / page_name
            page.save(str(page_path), "PNG", optimize=True)
            image_paths.append(page_path)
    else:
        dest_name = f"receipt_{timestamp}_{src.name}"
        dest_path = settings.receipts_dir / dest_name
        shutil.copy2(str(src), str(dest_path))
        image_paths.append(dest_path)

    # 2) Per page: decide queue-for-review vs OCR-now.
    #    - PDF pages are ALWAYS queued (same as a direct PDF upload) so the
    #      user confirms the receipt boxes on every page before OCR.
    #    - An image page is queued only when the segmenter splits it into >1
    #      receipt; a single-receipt image is OCR'd straight through.
    all_ids: list = []
    queued_pages = 0
    first_meta: Optional[dict] = None
    total_pages = len(image_paths)
    for i, img_path in enumerate(image_paths, 1):
        boxes, (w, h) = segment_with_boxes(str(img_path))
        if is_pdf_source or len(boxes) > 1:
            src_label = (
                f"{src.name} · p{i}/{total_pages} (scan)"
                if is_pdf_source and total_pages > 1
                else f"{src.name} (scan)"
            )
            review_queue.add(
                filename=img_path.name,
                image_w=w,
                image_h=h,
                bboxes=boxes,
                source=src_label,
                description="imported from folder scan",
            )
            queued_pages += 1
            continue

        items = process_with_segments(str(img_path), skip_segmentation=True)
        if not items:
            items = [
                {
                    "metadata": {
                        "vendor_name": "Unknown",
                        "category": "other",
                        "error": "no receipt",
                    },
                    "crop": None,
                }
            ]
        stem = img_path.stem
        for i, item in enumerate(items):
            meta = item.get("metadata") or {}
            crop = item.get("crop")
            if first_meta is None:
                first_meta = meta
            if crop is not None:
                row_filename = f"{stem}_seg{i + 1}.png"
                row_filepath = settings.receipts_dir / row_filename
                crop.save(str(row_filepath), "PNG", optimize=True)
                row_filepath_str = str(row_filepath)
            else:
                row_filename = img_path.name
                row_filepath_str = str(img_path)
            rid = save_receipt(
                filename=row_filename,
                filepath=row_filepath_str,
                metadata=meta,
                description="imported from folder scan",
            )
            all_ids.append(rid)

    result["status"] = "ok"
    result["count"] = len(all_ids)
    result["queued"] = queued_pages
    result["receipt_ids"] = all_ids
    if first_meta:
        result["vendor"] = first_meta.get("vendor_name")
        result["amount"] = first_meta.get("total_amount")
        result["currency"] = first_meta.get("currency")
        if first_meta.get("error"):
            result["ocr_warning"] = first_meta["error"]
    return result


def scan_folder() -> dict:
    """Process every supported file currently sitting in the inbox.

    On success a file is moved to inbox/processed/; on hard failure it's
    moved to inbox/failed/ — either way the inbox is left clean so a repeat
    scan never re-imports the same file. Returns a batch summary.
    """
    inbox = resolve_inbox()
    processed_dir = inbox / PROCESSED_SUBDIR
    failed_dir = inbox / FAILED_SUBDIR
    processed_dir.mkdir(parents=True, exist_ok=True)

    pending = _pending_files(inbox)
    summary = {
        "folder": str(inbox),
        "scanned": len(pending),
        "imported_receipts": 0,
        "files_ok": 0,
        "files_failed": 0,
        "queued": 0,
        "results": [],
    }

    for src in pending:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "source": str(src),
            "filename": src.name,
            "status": "error",
            "receipt_id": None,
            "error": None,
        }
        with _ingest_mutex:
            if not src.exists():
                # The live watcher ingested it while this scan was running.
                summary["scanned"] -= 1
                continue
            try:
                res = _ingest_file(src)
                entry.update(
                    {
                        "status": res["status"],
                        "count": res["count"],
                        "receipt_ids": res["receipt_ids"],
                        "receipt_id": res["receipt_ids"][0] if res["receipt_ids"] else None,
                        "vendor": res.get("vendor"),
                        "amount": res.get("amount"),
                        "currency": res.get("currency"),
                        "queued": res.get("queued", 0),
                    }
                )
                if res.get("ocr_warning"):
                    entry["ocr_warning"] = res["ocr_warning"]
                summary["imported_receipts"] += res["count"]
                summary["queued"] += res.get("queued", 0)
                summary["files_ok"] += 1
                _move_into(src, processed_dir)
            except Exception as e:
                entry["error"] = str(e)
                summary["files_failed"] += 1
                logger.error("Folder scan: failed on %s: %s", src.name, e)
                failed_dir.mkdir(parents=True, exist_ok=True)
                _move_into(src, failed_dir)

        _append_log(entry)
        summary["results"].append(entry)

    logger.info(
        "Folder scan: %d file(s), %d receipt(s), %d ok / %d failed",
        summary["scanned"],
        summary["imported_receipts"],
        summary["files_ok"],
        summary["files_failed"],
    )
    return summary


def _move_into(src: Path, dest_dir: Path) -> None:
    """Move src into dest_dir, disambiguating name collisions."""
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{int(time.time())}{src.suffix}"
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        logger.warning("Could not move %s → %s: %s", src, dest, e)


def _make_handler():
    try:
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return None

    class ReceiptHandler(FileSystemEventHandler):
        def _handle(self, path: Path):
            if path.parent.name in (PROCESSED_SUBDIR, FAILED_SUBDIR):
                return
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                logger.info("Watch folder: detected new file %s", path.name)
                try:
                    process_new_file(str(path))
                except Exception as e:
                    logger.error("Watch folder handler error: %s", e)

        def on_created(self, event):
            if not event.is_directory:
                self._handle(Path(event.src_path))

        def on_moved(self, event):
            # Files dragged/mv'd into the folder emit on_moved, not on_created.
            if not event.is_directory:
                self._handle(Path(event.dest_path))

    return ReceiptHandler()


def start_watcher(folder_path: str) -> Optional[object]:
    """Start a background watchdog observer. Returns None if unavailable."""
    global _observer

    try:
        from watchdog.observers import Observer
    except ImportError:
        logger.warning("watchdog package not installed — watch folder disabled")
        return None

    folder = Path(folder_path).expanduser().resolve()
    if not folder.exists():
        logger.warning("Watch folder %s does not exist — creating it", folder)
        folder.mkdir(parents=True, exist_ok=True)

    handler = _make_handler()
    if handler is None:
        return None

    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()
    _observer = observer
    logger.info("Watch folder started: %s", folder)
    return observer


def stop_watcher():
    global _observer
    if _observer and _observer.is_alive():
        _observer.stop()
        _observer.join()
        _observer = None
