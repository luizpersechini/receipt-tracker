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
    from .pdf_render import is_pdf

    # An e-mailed PDF is one receipt per page (page 1 is rasterised by the
    # pipeline); photos go through the segmenter like any upload.
    return process_with_segments(str(filepath), skip_segmentation=is_pdf(str(filepath)))


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


# ─── Per-transaction receipt finder ──────────────────────────────────────────
#
# "Find receipts in Gmail" on the reconcile board: for each open statement
# line run the charge query, score every email by amount / date / vendor /
# attachment evidence, and hand back ranked candidates. Import + match is a
# separate, explicit step (`import_and_match`).

_NUM_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})(?![\w])|(?<![\w.,])\d+[.,]\d{2}(?![\w])")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
AMOUNT_TOLERANCE = 0.07  # IOF + FX spread between the email and the card line
DATE_WINDOW_BEFORE = 4
DATE_WINDOW_AFTER = 7


def _body_text(message: dict) -> str:
    text = gmail_ingest.extract_plain_body(message)
    if not text:
        html_ = gmail_ingest.extract_html_body(message) or ""
        import html as _html

        text = _html.unescape(_TAG_RE.sub(" ", html_))
    return _WS_RE.sub(" ", text or "")


def _numbers_in(text: str) -> list[float]:
    from .money import parse_money

    out = []
    for m in _NUM_RE.finditer(text or ""):
        v = parse_money(m.group(0))
        if v is not None and v > 0:
            out.append(float(v))
    return out


def _targets(txn: dict) -> list[float]:
    raw = txn.get("raw_data") or {}
    vals = []
    for v in (txn.get("amount"), raw.get("original_amount")):
        try:
            f = abs(float(v))
            if f > 0:
                vals.append(f)
        except (TypeError, ValueError):
            pass
    return vals


def score_candidate(txn: dict, summary: dict, body: str) -> tuple[float, list[str]]:
    """Evidence score in [0, 1] with human-readable reasons."""
    score = 0.0
    reasons: list[str] = []
    nums = _numbers_in(f"{summary.get('subject') or ''} {summary.get('snippet') or ''} {body}")
    best = None
    for t in _targets(txn):
        for n in nums:
            if abs(n - t) < 0.005:
                best = ("exact", t)
                break
            if abs(n - t) / t <= AMOUNT_TOLERANCE and (best is None or best[0] != "exact"):
                best = ("near", n)
        if best and best[0] == "exact":
            break
    if best and best[0] == "exact":
        score += 0.5
        reasons.append(f"amount {best[1]:.2f} found")
    elif best:
        score += 0.3
        reasons.append(f"amount {best[1]:.2f} within {int(AMOUNT_TOLERANCE * 100)}%")

    try:
        from datetime import date

        gap = abs((date.fromisoformat((summary.get("date") or "")[:10]) - date.fromisoformat((txn.get("date") or "")[:10])).days)
        if gap <= 2:
            score += 0.2
            reasons.append(f"{gap} day(s) apart")
        elif gap <= DATE_WINDOW_AFTER:
            score += 0.1
            reasons.append(f"{gap} days apart")
    except ValueError:
        pass

    words = [w.lower() for w in _DROP_TOKENS.split(txn.get("description") or "") if len(w) >= 3]
    hay = f"{summary.get('from') or ''} {summary.get('subject') or ''}".lower()
    hit = next((w for w in words if w in hay), None)
    if hit:
        score += 0.2
        reasons.append(f'"{hit}" in sender/subject')

    if summary.get("attachments"):
        score += 0.1
        reasons.append("has attachment")
    return min(1.0, round(score, 2)), reasons


def find_for_transactions(
    statement: dict,
    indices: list[int],
    *,
    max_per_txn: int = 3,
    max_messages: int = 15,
    service=None,
) -> list[dict]:
    """[{index, query, candidates:[summary + score + reasons]}] per line.
    Messages are fetched once per run and shared between lines."""
    service = service or gmail_ingest.get_service()
    if service is None:
        raise RuntimeError("Gmail isn't connected")
    txns = statement.get("transactions") or []
    label_id = _find_label_id(service, settings.gmail_processed_label)
    cache: dict[str, tuple[dict, str]] = {}
    out = []
    for i in indices:
        if i < 0 or i >= len(txns):
            continue
        t = txns[i]
        q = build_query(query_for_charge(t.get("description") or "", t.get("date")))
        try:
            ids = gmail_ingest.list_message_ids(service, q, max_messages)
        except Exception as e:  # noqa: BLE001
            logger.warning("gmail find failed for line %s: %s", i, e)
            out.append({"index": i, "query": q, "candidates": [], "error": str(e)})
            continue
        cands = []
        for mid in ids:
            if mid not in cache:
                msg = gmail_ingest.fetch_message(service, mid)
                cache[mid] = (summarize(msg, label_id), _body_text(msg))
            summary, body = cache[mid]
            score, reasons = score_candidate(t, summary, body)
            cands.append({**summary, "score": score, "reasons": reasons})
        cands.sort(key=lambda c: (-c["score"], c.get("date") or ""))
        out.append({"index": i, "query": q, "candidates": cands[:max_per_txn]})
    return out


def import_and_match(
    statement_id: int,
    transaction_index: int,
    message_id: str,
    attachment_ids: Optional[list[str]] = None,
    *,
    service=None,
) -> dict:
    """Import the email, then pair its first usable receipt row with the
    statement line as a manual match. Extra rows (multi-receipt pages)
    stay unmatched on the board."""
    statement = db.get_statement_by_id(statement_id)
    if not statement:
        raise LookupError("Statement not found")
    txns = statement.get("transactions") or []
    if transaction_index < 0 or transaction_index >= len(txns):
        raise LookupError("transaction_index out of range")
    result = import_message(message_id, attachment_ids, service=service)
    rows = result.get("receipts") or []
    usable = [r for r in rows if not (r.get("metadata") or {}).get("error")]
    match_id = None
    warning = None
    if not usable and rows:
        # OCR failed on every row: keep the rows (re-run OCR from the table)
        # but don't pin a broken receipt to the line.
        warning = f"imported but not matched — OCR error: {rows[0]['metadata'].get('error')}"
    if usable:
        rid = usable[0]["receipt_id"]
        match_id = db.save_match(
            rid,
            statement_id,
            txns[transaction_index],
            None,
            match_kind="manual",
            transaction_index=transaction_index,
        )
        db.update_receipt_match_status(rid, "matched")
    return {
        **result,
        "statement_id": statement_id,
        "transaction_index": transaction_index,
        "match_id": match_id,
        "warning": warning,
    }
