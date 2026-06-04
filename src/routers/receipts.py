"""Receipt upload, listing, deletion, and expense-type endpoints."""

import asyncio
import logging
import shutil
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import database as db
from ..schemas.receipts import (
    METADATA_FIELDS,
    BulkDeleteRequest,
    ExpenseTypeUpdate,
    ReceiptCommitRequest,
    ReceiptEdit,
)
from ..services import review_queue
from ..services.llm_processor import ocr_crops, process_with_segments
from ..services.receipt_segmenter import segment_with_boxes

router = APIRouter(prefix="/api", tags=["receipts"])
logger = logging.getLogger(__name__)

# Limit concurrent OCR jobs — Ollama vision models are memory-heavy, running
# them in parallel on a laptop is what gets the machine stuck.
_ocr_semaphore = asyncio.Semaphore(max(1, settings.ocr_concurrency))


@router.post("/upload/receipt/preview")
async def upload_receipt_preview(file: UploadFile = File(...)):
    """
    First step of the two-phase upload flow: save the file to disk and run
    the segmenter to suggest receipt bounding boxes. Does NOT run OCR.

    The frontend uses the returned bboxes as the starting point for a manual
    bbox editor. The user can adjust / add / delete boxes before calling
    /commit, which performs the actual OCR.

    Special case: PDF uploads are rasterised page-by-page; each page is
    segmented and queued for box review (no OCR yet). The response carries
    `review_queued` (count) + `pages` so the frontend opens the review flow,
    letting the user confirm/adjust receipt boxes per page before OCR.
    """
    # Local imports keep cold start light and let the PDF path be tested
    # without dragging cv2 into image-only flows.
    from ..services.pdf_render import is_pdf, render_pdf_to_images

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"receipt_{timestamp}_{file.filename}"
        filepath = settings.receipts_dir / filename
        with open(filepath, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        # ── PDF path: rasterise → segment each page → queue EVERY page for
        # box review (no OCR yet). The frontend opens the editor per page so
        # the user can confirm/adjust receipt boxes before OCR.
        if is_pdf(str(filepath)):
            pages = await asyncio.to_thread(render_pdf_to_images, str(filepath))
            stem = filepath.stem
            queued = []
            for i, page in enumerate(pages, 1):
                page_name = f"{stem}_p{i}.png"
                page_path = settings.receipts_dir / page_name
                await asyncio.to_thread(
                    lambda p=page, pp=page_path: p.save(str(pp), "PNG", optimize=True)
                )
                boxes, (w, h) = await asyncio.to_thread(segment_with_boxes, str(page_path))
                item = await asyncio.to_thread(
                    review_queue.add,
                    filename=page_name,
                    image_w=w,
                    image_h=h,
                    bboxes=boxes,
                    source=f"{file.filename} · p{i}/{len(pages)}",
                    description=None,
                )
                queued.append(item["id"])
            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    "review_queued": len(queued),
                    "pages": len(pages),
                }
            )

        boxes, (w, h) = await asyncio.to_thread(segment_with_boxes, str(filepath))
        # Route through /api/preview/ instead of the raw static mount so
        # HEIC/HEIF uploads are transparently converted to JPEG on the fly —
        # browsers can't render HEIC natively, and without this the bbox
        # editor shows a broken-image icon.
        from urllib.parse import quote

        return JSONResponse(
            {
                "success": True,
                "filename": filename,
                "image_url": f"/api/preview/{quote(filename)}",
                "image_size": {"w": w, "h": h},
                "bboxes": boxes,
                "auto_segmented": len(boxes) > 1,
            }
        )
    except Exception as e:
        logger.exception("upload_receipt_preview failed")
        raise HTTPException(status_code=500, detail=str(e))


def _box_xywh(b) -> tuple:
    if isinstance(b, dict):
        return int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
    return int(b.x), int(b.y), int(b.w), int(b.h)


async def commit_boxes_to_receipts(filename: str, bboxes: list, description=None) -> list:
    """OCR each bounding box on a previously-saved page image and persist one
    receipt per box. Shared by /upload/receipt/commit and the review-queue
    commit. `bboxes` items may be pydantic BBox objects or {x,y,w,h} dicts.
    Returns the saved rows; raises HTTPException on bad input."""
    from pathlib import Path

    from PIL import Image

    filepath = settings.receipts_dir / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail=f"Uploaded file not found: {filename}")

    # Guard against path traversal — be defensive even though the filename
    # comes from our own /preview / review-queue responses.
    try:
        filepath.resolve().relative_to(settings.receipts_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        pil = await asyncio.to_thread(lambda: Image.open(filepath).convert("RGB"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not open image: {e}")

    img_w, img_h = pil.size
    crops = []
    for box in bboxes:
        bx, by, bw, bh = _box_xywh(box)
        # Clamp to image bounds — frontend math can drift by a pixel or two.
        x0 = max(0, min(bx, img_w - 1))
        y0 = max(0, min(by, img_h - 1))
        x1 = max(x0 + 1, min(bx + bw, img_w))
        y1 = max(y0 + 1, min(by + bh, img_h))
        crops.append(pil.crop((x0, y0, x1, y1)))

    async with _ocr_semaphore:
        items = await asyncio.to_thread(ocr_crops, crops)

    stem = Path(filename).stem
    saved = []
    for i, item in enumerate(items):
        meta = item["metadata"]
        crop = item["crop"]
        if description:
            meta["user_description"] = description

        if len(items) > 1 and crop is not None:
            row_filename = f"{stem}_seg{i + 1}.png"
            row_filepath = settings.receipts_dir / row_filename
            await asyncio.to_thread(crop.save, str(row_filepath), "PNG", optimize=True)
            row_filepath_str = str(row_filepath)
        else:
            row_filename = filename
            row_filepath_str = str(filepath)

        receipt_id = await asyncio.to_thread(
            db.save_receipt, row_filename, row_filepath_str, meta, description
        )
        saved.append({"receipt_id": receipt_id, "filename": row_filename, "metadata": meta})
    return saved


@router.post("/upload/receipt/commit")
async def upload_receipt_commit(body: ReceiptCommitRequest):
    """Second step of the two-phase upload flow: OCR each user-provided box on
    a file previously saved by /preview. One receipt row per box."""
    saved = await commit_boxes_to_receipts(body.filename, body.bboxes, body.description)
    return JSONResponse(
        {
            "success": True,
            "count": len(saved),
            "receipts": saved,
            "receipt_id": saved[0]["receipt_id"] if saved else None,
            "filename": saved[0]["filename"] if saved else None,
            "metadata": saved[0]["metadata"] if saved else None,
        }
    )


@router.post("/upload/receipt")
async def upload_receipt(file: UploadFile = File(...), description: str = Form(None)):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"receipt_{timestamp}_{file.filename}"
        filepath = settings.receipts_dir / filename

        with open(filepath, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        async with _ocr_semaphore:
            items = await asyncio.to_thread(process_with_segments, str(filepath))

        # Save one DB row per receipt detected. When the segmenter split
        # the image into multiple crops, persist each crop as its own file
        # so the thumbnail shows the right portion of the page.
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

        # Filename stem without extension, e.g. "receipt_20260513_120000_IMG_1234"
        from pathlib import Path

        stem = Path(filename).stem

        saved = []
        for i, item in enumerate(items):
            meta = item["metadata"]
            crop = item["crop"]
            if description:
                meta["user_description"] = description

            if crop is not None:
                # Multi-receipt scan: save this segment as its own PNG so the
                # row's thumbnail shows just this receipt, not the full page.
                row_filename = f"{stem}_seg{i + 1}.png"
                row_filepath = settings.receipts_dir / row_filename
                await asyncio.to_thread(
                    crop.save,
                    str(row_filepath),
                    "PNG",
                    optimize=True,
                )
                row_filepath_str = str(row_filepath)
            else:
                # Single-segment image — share the original upload.
                row_filename = filename
                row_filepath_str = str(filepath)

            receipt_id = await asyncio.to_thread(
                db.save_receipt,
                row_filename,
                row_filepath_str,
                meta,
                description,
            )
            saved.append({"receipt_id": receipt_id, "filename": row_filename, "metadata": meta})

        return JSONResponse(
            {
                "success": True,
                "count": len(saved),
                "receipts": saved,
                # Back-compat: surface the first row at the top level so older
                # frontend code (or external callers) keep working.
                "receipt_id": saved[0]["receipt_id"],
                "filename": saved[0]["filename"],
                "metadata": saved[0]["metadata"],
            }
        )
    except Exception as e:
        logger.exception("upload_receipt failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/receipts")
async def get_receipts():
    receipts = await asyncio.to_thread(db.get_all_receipts)
    return JSONResponse({"receipts": receipts})


@router.delete("/receipts/{receipt_id}")
async def remove_receipt(receipt_id: int):
    try:
        await asyncio.to_thread(db.delete_receipt, receipt_id)
        return JSONResponse({"success": True})
    except Exception as e:
        logger.exception("delete receipt failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/receipts/{receipt_id}/rotate")
async def rotate_receipt(receipt_id: int, degrees: int = 90):
    """Rotate the receipt's source image on disk by ±90° or 180°. Affects
    the thumbnail and lightbox preview — useful when a phone photo was
    taken sideways."""
    if degrees not in (-90, 90, 180, 270, -180, -270):
        raise HTTPException(status_code=400, detail="degrees must be ±90, ±180, or 270")

    row = await asyncio.to_thread(db.get_receipt_by_id, receipt_id)
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found")

    filepath_str = row.get("filepath") or ""
    if filepath_str.startswith("gmail://"):
        raise HTTPException(status_code=400, detail="Cannot rotate a Gmail-sourced PDF")

    from pathlib import Path
    from PIL import Image, ImageOps

    filepath = Path(filepath_str)
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail=f"File missing on disk: {filepath}")

    # Path-traversal guard: file must live under settings.receipts_dir
    try:
        filepath.resolve().relative_to(settings.receipts_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="File outside receipts_dir")

    def _rotate_in_place():
        # ImageOps.exif_transpose first so the rotation matches what the
        # user is SEEING in the browser, not the raw sensor orientation.
        img = Image.open(str(filepath))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        # PIL rotates COUNTER-clockwise; we want CW for positive degrees.
        rotated = img.rotate(-degrees, expand=True)
        # Preserve original extension where possible; PNG is safe fallback.
        ext = filepath.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            rotated.save(str(filepath), format="JPEG", quality=92, optimize=True)
        else:
            rotated.save(str(filepath), format="PNG", optimize=True)

    try:
        await asyncio.to_thread(_rotate_in_place)
    except Exception as e:
        logger.exception("rotate failed")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"success": True, "receipt_id": receipt_id, "degrees": degrees})


@router.post("/receipts/dedupe-uber")
async def dedupe_uber():
    """
    Find Uber-tagged receipts that share the same (date, amount, currency)
    and merge each group down to a single row. Preference: keep the row with
    a real PDF on disk (filename ends in `.pdf`) over CSV/synthetic ones.
    Returns counts of kept / removed.
    """
    import json as _json
    from collections import defaultdict

    def _do_dedupe():
        receipts = db.get_all_receipts()
        uber = [r for r in receipts if (r.get("filename") or "").startswith("uber_")]
        groups: dict = defaultdict(list)
        for r in uber:
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except _json.JSONDecodeError:
                continue
            d = meta.get("date")
            a = meta.get("total_amount")
            c = (meta.get("currency") or "").upper()
            if d is None or a is None or not c:
                continue
            # Round amount to 2dp to absorb tiny float discrepancies
            key = (d, round(float(a), 2), c)
            groups[key].append(r)

        kept = 0
        removed = 0
        groups_with_dupes = 0
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            groups_with_dupes += 1
            # Prefer the row whose filename ends in .pdf (rendered Gmail
            # receipt). If multiple, prefer the lowest id (earliest import).
            rows.sort(
                key=lambda r: (
                    0 if (r.get("filename") or "").endswith(".pdf") else 1,
                    r.get("id") or 0,
                )
            )
            keeper = rows[0]
            for victim in rows[1:]:
                db.delete_receipt(victim["id"])
                removed += 1
            kept += 1
        return {
            "groups_with_dupes": groups_with_dupes,
            "kept": kept,
            "removed": removed,
        }

    try:
        result = await asyncio.to_thread(_do_dedupe)
    except Exception as e:
        logger.exception("dedupe_uber failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **result})


@router.post("/receipts/backfill-country")
async def backfill_country():
    """
    Walk every receipt with a null `country` in metadata and try to fill it
    in: subdivision code (state) → ISO country, then city → ISO country.
    Also refines currency once country is known (e.g. Vancouver USD → CAD).

    Useful one-shot after the new Country column landed — older Uber CSV
    rows have city but no country.

    Returns counts of scanned / changed receipts.
    """
    import json as _json
    from src.services.llm_processor import (
        _normalize_country,
        _country_from_city,
        _refine_currency,
    )

    def _do_backfill():
        receipts = db.get_all_receipts()
        scanned = 0
        changed = 0
        for r in receipts:
            scanned += 1
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except _json.JSONDecodeError:
                continue
            if meta.get("country"):
                continue  # already set — don't touch
            country = (
                _normalize_country(meta.get("state"))
                or _country_from_city(meta.get("city"))
                # Last-ditch: scan the vendor_address for a literal country
                # name (covers compound cities like "Itabuna e Ilheus" where
                # the city map misses but the address says "…, Brazil").
                or _normalize_country(meta.get("vendor_address"))
            )
            if not country:
                continue
            patch = {"country": country}
            # Currency refinement piggybacks — once we know the country, fix
            # bare-symbol misreads (e.g. $5 on a Toronto receipt → CAD).
            refined = _refine_currency(meta.get("currency"), country)
            if refined and refined != meta.get("currency"):
                patch["currency"] = refined
            db.update_receipt(r["id"], column_patch={}, metadata_patch=patch)
            changed += 1
        return {"scanned": scanned, "changed": changed}

    try:
        result = await asyncio.to_thread(_do_backfill)
    except Exception as e:
        logger.exception("backfill_country failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **result})


@router.post("/receipts/strip-times")
async def strip_times_from_dates():
    """
    Walk every receipt and chop any clock time off the `date` field in
    metadata. Useful one-shot after the upload pipeline started stripping
    times — older rows already in the DB still carry the time tail.

    Returns counts of scanned / changed receipts.
    """
    import json as _json
    from src.services.llm_processor import _strip_time_from_date

    def _do_strip():
        receipts = db.get_all_receipts()
        scanned = 0
        changed = 0
        for r in receipts:
            scanned += 1
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except _json.JSONDecodeError:
                continue
            before = meta.get("date")
            if not before:
                continue
            after = _strip_time_from_date(before)
            if after == before:
                continue
            # Persist the cleaned date back to the metadata JSON
            db.update_receipt(r["id"], column_patch={}, metadata_patch={"date": after})
            changed += 1
        return {"scanned": scanned, "changed": changed}

    try:
        result = await asyncio.to_thread(_do_strip)
    except Exception as e:
        logger.exception("strip_times_from_dates failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **result})


@router.post("/receipts/normalize-dates")
async def normalize_dates():
    """
    Walk every receipt and normalise the `date` field in metadata to ISO
    YYYY-MM-DD. Useful one-shot after shipping the date normaliser — older
    rows are still in mixed locale formats ("25/03/2026", "01/abr", etc.).

    Keeps the original printed string when parsing fails, so the column is
    never silently emptied.
    """
    import json as _json

    from src.services.llm_processor import _normalize_date, _strip_time_from_date

    def _do_normalize():
        receipts = db.get_all_receipts()
        scanned = 0
        changed = 0
        for r in receipts:
            scanned += 1
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except _json.JSONDecodeError:
                continue
            before = meta.get("date")
            if not before:
                continue
            cleaned = _strip_time_from_date(before)
            after = _normalize_date(
                cleaned, language=meta.get("language"), country=meta.get("country")
            )
            if not after or after == before:
                continue
            db.update_receipt(r["id"], column_patch={}, metadata_patch={"date": after})
            changed += 1
        return {"scanned": scanned, "changed": changed}

    try:
        result = await asyncio.to_thread(_do_normalize)
    except Exception as e:
        logger.exception("normalize_dates failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **result})


@router.post("/receipts/{receipt_id}/reocr")
async def reocr_receipt(receipt_id: int):
    """
    Re-run OCR on the image already saved for this receipt and overwrite its
    metadata in place. Use after improving the OCR prompt / model or when you
    suspect a row's data is wrong but the image is fine. Receipt id is stable.

    Refuses Gmail-sourced rows (filepath="gmail://...") since they don't have
    a local image file in receipts_dir.
    """
    from pathlib import Path

    row = await asyncio.to_thread(db.get_receipt_by_id, receipt_id)
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found")

    filepath_str = row.get("filepath") or ""
    if not filepath_str or filepath_str.startswith("gmail://"):
        raise HTTPException(
            status_code=400,
            detail="Re-OCR only supports receipts backed by a local image file",
        )

    filepath = Path(filepath_str)
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail=f"File missing on disk: {filepath}")

    # Path-traversal guard: must live under receipts_dir
    try:
        filepath.resolve().relative_to(settings.receipts_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="File outside receipts_dir")

    async with _ocr_semaphore:
        items = await asyncio.to_thread(process_with_segments, str(filepath))

    if not items:
        raise HTTPException(status_code=500, detail="OCR returned nothing")

    # Take the first segment's metadata — this row maps to one receipt, so we
    # apply the first parse. If segmentation found more than one receipt in
    # the image and the user wants those as separate rows, they should upload
    # the original image fresh rather than re-OCR'ing one row.
    meta = items[0]["metadata"]
    if meta.get("error"):
        raise HTTPException(status_code=500, detail=f"OCR error: {meta['error']}")

    # Full replacement: pass every known metadata field, defaulting to None
    # for keys the new extraction left empty. update_receipt drops None keys
    # so stale values from the previous OCR don't survive.
    metadata_patch = {f: meta.get(f) for f in METADATA_FIELDS}

    ok = await asyncio.to_thread(db.update_receipt, receipt_id, {}, metadata_patch)
    if not ok:
        raise HTTPException(status_code=500, detail="DB update failed")

    return JSONResponse({"success": True, "receipt_id": receipt_id, "metadata": meta})


@router.post("/receipts/dedupe")
async def dedupe_all(dry_run: bool = False):
    """
    Generic dedup across ALL receipts. Two patterns merged into one keeper per group:

      1. Exact match: same normalised vendor + same date + same amount (rounded
         to 2dp). Pure duplicates from re-uploading the same image, or a multi-
         page scan producing twin rows.

      2. Tip-pair: same normalised vendor + same date, two rows where the lower
         amount is between 70% and 95% of the higher amount. Catches the very
         common case of a restaurant printing both a food slip (pre-tip total)
         and a card slip (post-tip total) as separate receipts — the food slip
         is the lower amount.

    Keeper selection: highest amount, then lowest id (earliest import).
    Rows where vendor is null/Unknown are skipped — they don't have a strong
    enough signal to auto-merge.

    Set `dry_run=true` to preview which ids would be removed without touching
    the database.
    """
    import json as _json
    import re as _re
    from collections import defaultdict

    def _norm_vendor(name: str) -> str:
        s = (name or "").lower().strip()
        # Drop trailing location qualifiers ("- Coal Harbour NRO", "Gastown")
        # and punctuation so "Ciclo Espresso – Coal Harbour" and
        # "Ciclo Espresso" group together.
        s = _re.split(r"[–—\-|·]", s, maxsplit=1)[0]
        s = _re.sub(r"[^a-z0-9 ]", " ", s)
        s = _re.sub(r"\s+", " ", s).strip()
        return s

    def _do_dedupe():
        receipts = db.get_all_receipts()
        # Group by (vendor_norm, date)
        groups: dict = defaultdict(list)
        for r in receipts:
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except _json.JSONDecodeError:
                continue
            vendor = meta.get("vendor_name") or ""
            v_norm = _norm_vendor(vendor)
            if not v_norm or v_norm == "unknown":
                continue
            date = meta.get("date")
            amount = meta.get("total_amount")
            if not date or amount is None:
                continue
            try:
                amount_f = round(float(amount), 2)
            except (TypeError, ValueError):
                continue
            groups[(v_norm, date)].append(
                {
                    "id": r.get("id"),
                    "amount": amount_f,
                    "filename": r.get("filename") or "",
                }
            )

        to_remove: list = []
        groups_with_dupes = 0

        for key, rows in groups.items():
            if len(rows) < 2:
                continue

            # Sort descending by amount, then by id ascending — keeper is row[0]
            rows.sort(key=lambda x: (-x["amount"], x["id"] or 0))
            keeper = rows[0]

            for victim in rows[1:]:
                # Pattern 1: same amount within a cent
                if abs(victim["amount"] - keeper["amount"]) <= 0.01:
                    to_remove.append(victim["id"])
                    continue
                # Pattern 2: tip-pair. Victim must be 70–95% of keeper's amount.
                ratio = victim["amount"] / keeper["amount"] if keeper["amount"] else 0
                if 0.70 <= ratio <= 0.95:
                    to_remove.append(victim["id"])
                    continue
                # Otherwise leave it — likely a legitimate second visit same day.

            if to_remove:
                groups_with_dupes += 1

        if not dry_run:
            for rid in to_remove:
                db.delete_receipt(rid)

        return {
            "groups_with_dupes": groups_with_dupes,
            "removed_ids": to_remove,
            "removed": len(to_remove),
            "dry_run": dry_run,
        }

    try:
        result = await asyncio.to_thread(_do_dedupe)
    except Exception as e:
        logger.exception("dedupe_all failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, **result})


@router.post("/receipts/bulk-delete")
async def bulk_delete(body: BulkDeleteRequest):
    """Delete multiple receipts in one call. Returns the count actually deleted."""

    def _delete_all(ids):
        for rid in ids:
            db.delete_receipt(rid)
        return len(ids)

    try:
        count = await asyncio.to_thread(_delete_all, body.ids)
    except Exception as e:
        logger.exception("bulk delete failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"success": True, "deleted": count})


@router.patch("/receipts/{receipt_id}/expense-type")
async def set_expense_type(receipt_id: int, body: ExpenseTypeUpdate):
    try:
        await asyncio.to_thread(db.update_receipt_expense_type, receipt_id, body.expense_type)
        return JSONResponse({"success": True, "expense_type": body.expense_type})
    except Exception as e:
        logger.exception("update expense_type failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/receipts/{receipt_id}")
async def edit_receipt(receipt_id: int, body: ReceiptEdit):
    """Partial edit of any receipt field. Use null to clear a metadata field."""
    patch = body.model_dump(exclude_unset=True)
    column_patch: dict = {}
    metadata_patch: dict = {}
    for key, value in patch.items():
        if key in METADATA_FIELDS:
            metadata_patch[key] = value
        else:
            column_patch[key] = value

    try:
        ok = await asyncio.to_thread(
            db.update_receipt,
            receipt_id,
            column_patch,
            metadata_patch,
        )
    except Exception as e:
        logger.exception("edit_receipt failed")
        raise HTTPException(status_code=500, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail=f"Receipt {receipt_id} not found")
    return JSONResponse({"success": True})
