"""Ad-hoc Gmail search + import: find receipt emails for any vendor and pull
them into the receipts table through the normal OCR pipeline.

Sits next to the Uber ingest (`gmail_ingest`) and reuses its OAuth client,
MIME helpers and processed label. Searches are read-only; an import writes
the attachment (or a PDF rendered from the email body) under
`settings.receipts_dir`, runs OCR like an upload would, and labels the
message `settings.gmail_processed_label` so it is hidden from later searches
unless the caller asks for imported messages too.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings
from ..db import database as db
from . import gmail_ingest
from .uploads import safe_name

logger = logging.getLogger(__name__)

# Default scope: skip marketing and social mail (user's choice).
EXCLUDE_CATEGORIES = "-category:promotions -category:social"

# Attachment types the OCR pipeline can open.
IMPORTABLE_MIME = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
}
MAX_RESULTS = 100


# ─── Query ───────────────────────────────────────────────────────────────────


def build_query(q: str, *, include_imported: bool = False, exclude_categories: bool = True) -> str:
    parts = [q.strip()]
    if exclude_categories:
        parts.append(EXCLUDE_CATEGORIES)
    if not include_imported and settings.gmail_processed_label:
        parts.append(f"-label:{settings.gmail_processed_label}")
    return " ".join(p for p in parts if p)


_DROP_TOKENS = re.compile(r"[^A-Za-zÀ-ÿ]+")


def query_for_charge(description: str, date_iso: Optional[str]) -> str:
    """Gmail query for one statement line: the alphabetic words of the
    descriptor (≥3 chars, max 3), OR-ed because card descriptors carry
    noise tokens ("SUB", "COM") that rarely appear in the email, plus a
    date window around the charge that keeps the OR from matching too much."""
    words = [w for w in _DROP_TOKENS.split(description or "") if len(w) >= 3][:3]
    if len(words) > 1:
        q = "(" + " OR ".join(words) + ")"
    else:
        q = words[0] if words else (description or "").strip()
    if date_iso:
        try:
            d = datetime.fromisoformat(date_iso[:10]).date()
            from datetime import timedelta

            a = (d - timedelta(days=4)).strftime("%Y/%m/%d")
            b = (d + timedelta(days=7)).strftime("%Y/%m/%d")
            q = f"{q} after:{a} before:{b}"
        except ValueError:
            pass
    return q


# ─── Search ──────────────────────────────────────────────────────────────────


def _find_label_id(service, name: str) -> Optional[str]:
    """Label id by display name, without creating it."""
    try:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("gmail labels list failed: %s", e)
        return None
    for lbl in labels:
        if lbl.get("name") == name:
            return lbl.get("id")
    return None


def _attachments(message: dict) -> list[dict]:
    out: list[dict] = []

    def walk(part):
        body = part.get("body") or {}
        mime = (part.get("mimeType") or "").lower()
        fname = part.get("filename") or ""
        if body.get("attachmentId") and (mime in IMPORTABLE_MIME or _ext_ok(fname)):
            out.append(
                {
                    "id": body["attachmentId"],
                    "filename": fname or f"attachment{IMPORTABLE_MIME.get(mime, '')}",
                    "mime": mime,
                    "size": int(body.get("size") or 0),
                }
            )
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(message.get("payload") or {})
    return out


def _ext_ok(filename: str) -> bool:
    return Path(filename).suffix.lower() in set(IMPORTABLE_MIME.values())


def _message_date(message: dict) -> Optional[str]:
    ms = message.get("internalDate")
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


def summarize(message: dict, processed_label_id: Optional[str]) -> dict:
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "date": _message_date(message),
        "from": gmail_ingest.header(message, "From"),
        "subject": gmail_ingest.header(message, "Subject"),
        "snippet": message.get("snippet"),
        "attachments": _attachments(message),
        "has_html": gmail_ingest.extract_html_body(message) is not None,
        "imported": bool(
            processed_label_id and processed_label_id in (message.get("labelIds") or [])
        ),
    }


def search(q: str, max_results: int = 25, *, include_imported: bool = False, service=None) -> dict:
    service = service or gmail_ingest.get_service()
    if service is None:
        raise RuntimeError("Gmail isn't connected")
    query = build_query(q, include_imported=include_imported)
    n = max(1, min(int(max_results), MAX_RESULTS))
    ids = gmail_ingest.list_message_ids(service, query, n)
    label_id = _find_label_id(service, settings.gmail_processed_label)
    rows = [summarize(gmail_ingest.fetch_message(service, mid), label_id) for mid in ids]
    return {"query": query, "messages": rows}


# ─── Import ──────────────────────────────────────────────────────────────────


def fetch_attachment(service, msg_id: str, att_id: str) -> bytes:
    res = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=msg_id, id=att_id)
        .execute()
    )
    return base64.urlsafe_b64decode(res.get("data") or "")


def _save_rows(filename: str, filepath: Path, items: list[dict], description: str) -> list[dict]:
    """Persist OCR output like the upload endpoint: one row per detected
    receipt, crops saved as their own PNGs."""
    if not items:
        items = [
            {
                "metadata": {"vendor_name": "Unknown", "category": "other", "error": "no receipt"},
                "crop": None,
            }
        ]
    stem = Path(filename).stem
    saved = []
    for i, item in enumerate(items):
        meta = item["metadata"]
        crop = item.get("crop")
        if crop is not None:
            row_filename = f"{stem}_seg{i + 1}.png"
            row_path = settings.receipts_dir / row_filename
            crop.save(str(row_path), "PNG", optimize=True)
        else:
            row_filename, row_path = filename, filepath
        rid = db.save_receipt(row_filename, str(row_path), meta, description)
        saved.append({"receipt_id": rid, "filename": row_filename, "metadata": meta})
    return saved


def _ocr(filepath: Path) -> list[dict]:
    from .llm_processor import process_with_segments

    return process_with_segments(str(filepath))


def import_message(
    msg_id: str, attachment_ids: Optional[list[str]] = None, *, service=None
) -> dict:
    """Import one email as receipt row(s).

    With attachments (the given ids, or every importable one when None):
    each file is saved and OCR'd. Without any: the HTML body is rendered to
    a PDF and OCR'd. The message is then labelled as processed.
    """
    service = service or gmail_ingest.get_service()
    if service is None:
        raise RuntimeError("Gmail isn't connected")
    message = gmail_ingest.fetch_message(service, msg_id)
    subject = gmail_ingest.header(message, "Subject") or ""
    description = f"Gmail: {subject}"[:200] if subject else "Gmail"
    settings.receipts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    atts = _attachments(message)
    if attachment_ids is not None:
        wanted = set(attachment_ids)
        atts = [a for a in atts if a["id"] in wanted]

    saved: list[dict] = []
    files: list[str] = []
    if atts:
        for k, a in enumerate(atts, 1):
            data = fetch_attachment(service, msg_id, a["id"])
            if not data:
                continue
            name = safe_name(a["filename"]) or f"attachment{k}"
            if not Path(name).suffix:
                name += IMPORTABLE_MIME.get(a["mime"], ".bin")
            filename = f"gmail_{ts}_{k}_{name}"
            path = settings.receipts_dir / filename
            path.write_bytes(data)
            files.append(filename)
            saved.extend(_save_rows(filename, path, _ocr(path), description))
    else:
        html = gmail_ingest.extract_html_body(message)
        if not html:
            raise ValueError("Email has no importable attachment and no HTML body")
        from . import receipt_pdf

        if not receipt_pdf.is_available():
            raise RuntimeError(
                "PDF rendering (WeasyPrint) is unavailable — cannot import a body-only email"
            )
        pdf = receipt_pdf.render_html_to_pdf(html)
        filename = f"gmail_{ts}_{safe_name(subject)[:60] or 'email'}.pdf"
        path = settings.receipts_dir / filename
        path.write_bytes(pdf)
        files.append(filename)
        saved.extend(_save_rows(filename, path, _ocr(path), description))

    if saved:
        _mark_processed(service, msg_id)
    return {"message_id": msg_id, "files": files, "receipts": saved}


def _mark_processed(service, msg_id: str) -> None:
    try:
        label_id = gmail_ingest._get_or_create_label_id(service, settings.gmail_processed_label)
        service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [label_id]}
        ).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not label %s as processed: %s", msg_id, e)
