"""History of generated report PDFs.

Every report the app builds (statement or reimbursement) is saved to
`data/reports/` and indexed in `data/reports/history.json` so the user can
re-download past reports from the Reports tab. The index is a list of
records, newest first.

Stored files + index live under `data/` which is gitignored (they embed
receipt images and personal data).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from .json_store import locked, write_json

logger = logging.getLogger(__name__)

# Every report download appends a record + a PDF copy; without a cap the
# reports dir grows forever. Oldest records (and their files) get pruned.
MAX_HISTORY = 100


def _dir() -> Path:
    d = settings.reports_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _dir() / "history.json"


def _read() -> list[dict]:
    p = _index_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("report history index unreadable (%s)", e)
        return []


def _write(records: list[dict]) -> None:
    write_json(_index_path(), records)


def _norm(rec: Optional[dict]) -> Optional[dict]:
    """Default the membership-editing fields for records written before this
    feature: missing `version` → 1, missing `pdf_stale` → False, missing
    `updated_at` → `created_at`. Non-destructive (applied on read)."""
    if rec is None:
        return None
    rec.setdefault("version", 1)
    rec.setdefault("pdf_stale", False)
    rec.setdefault("updated_at", rec.get("created_at"))
    return rec


def _next_id(records: list[dict]) -> int:
    """Never reuse an id: consider both the index AND report_*.pdf files on
    disk, so deleting the newest record can't recycle its id (a stale client
    holding the old id would download a different report)."""
    highest = max((int(r.get("id", 0)) for r in records), default=0)
    for f in _dir().glob("report_*.pdf"):
        try:
            highest = max(highest, int(f.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return highest + 1


@locked(_index_path)
def record(
    pdf_bytes: bytes,
    *,
    kind: str,
    title: str,
    cardholder: Optional[str] = None,
    currency: Optional[str] = None,
    period: Optional[str] = None,
    download_name: Optional[str] = None,
    created_at: Optional[str] = None,
    receipt_ids: Optional[list] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Save a generated PDF + append an index record. Returns the record.

    `kind` is 'statement' or 'reimbursement'. `created_at` lets the caller
    pass a timestamp (tests); defaults to now. `receipt_ids` records which
    receipts went into the report, so marking it completed can flip exactly
    those to `reimbursed`.
    """
    records = _read()
    next_id = _next_id(records)
    fname = f"report_{next_id}.pdf"
    (_dir() / fname).write_bytes(pdf_bytes)

    ts = created_at or datetime.now().isoformat(timespec="seconds")
    rec = {
        "id": next_id,
        "created_at": ts,
        "kind": kind,
        "title": title,
        "cardholder": cardholder or "",
        "user_id": user_id,
        "currency": currency or "",
        "period": period or "",
        "size": len(pdf_bytes),
        "file": fname,
        "download_name": download_name or fname,
        "receipt_ids": list(receipt_ids or []),
        "completed": False,
        "completed_at": None,
        # Membership-editing fields (see the reimbursement report-editor feature):
        # mutable receipt_ids, re-issue version stamp, and a stale flag set on edit.
        "version": 1,
        "pdf_stale": False,
        "updated_at": ts,
    }
    records.insert(0, rec)  # newest first
    for old in records[MAX_HISTORY:]:
        try:
            (_dir() / old["file"]).unlink(missing_ok=True)
        except OSError:
            pass
    records = records[:MAX_HISTORY]
    _write(records)
    logger.info("report history: saved #%d (%s, %d bytes)", next_id, kind, len(pdf_bytes))
    return rec


def list_history() -> list[dict]:
    return [_norm(r) for r in _read()]


def get_record(rid: int) -> Optional[dict]:
    for r in _read():
        if int(r.get("id", -1)) == int(rid):
            return _norm(r)
    return None


@locked(_index_path)
def set_completed(rid: int, completed: bool) -> Optional[dict]:
    """Flip a report's `completed` flag (and stamp `completed_at`). Returns the
    updated record, or None if no such report. Caller flips the linked
    receipts' status based on the returned record's `receipt_ids`."""
    records = _read()
    updated = None
    for r in records:
        if int(r.get("id", -1)) == int(rid):
            r["completed"] = bool(completed)
            r["completed_at"] = datetime.now().isoformat(timespec="seconds") if completed else None
            updated = r
            break
    if updated is None:
        return None
    _write(records)
    return updated


@locked(_index_path)
def update_membership(rid: int, add: list, remove: list) -> Optional[dict]:
    """Mutate a report's `receipt_ids` membership set: remove `remove`, then
    append any new `add` (order preserved, deduped). Marks the PDF stale and
    stamps `updated_at`; does NOT bump `version` (that happens on re-issue) or
    touch receipt statuses (the caller applies those per the truth-table).
    Returns the updated record, or None if no such report.

    Validation (single membership, reimbursement-type) is the caller's job —
    this store only has the JSON index, not the receipts table."""
    records = _read()
    rec = None
    for r in records:
        if int(r.get("id", -1)) == int(rid):
            rec = r
            break
    if rec is None:
        return None
    remove_set = {int(i) for i in (remove or [])}
    ids = [int(i) for i in (rec.get("receipt_ids") or []) if int(i) not in remove_set]
    for i in add or []:
        if int(i) not in ids:
            ids.append(int(i))
    rec["receipt_ids"] = ids
    rec["pdf_stale"] = True
    rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write(records)
    return _norm(rec)


@locked(_index_path)
def reissue(rid: int, pdf_bytes: bytes) -> Optional[dict]:
    """Overwrite the report's PDF with freshly-rendered `pdf_bytes`, bump
    `version`, clear `pdf_stale`, refresh `size`/`updated_at`. Returns the
    updated record, or None if no such report. Caller guards against an empty
    membership set."""
    records = _read()
    rec = None
    for r in records:
        if int(r.get("id", -1)) == int(rid):
            rec = r
            break
    if rec is None:
        return None
    (_dir() / rec["file"]).write_bytes(pdf_bytes)
    rec["version"] = int(rec.get("version") or 1) + 1
    rec["pdf_stale"] = False
    rec["size"] = len(pdf_bytes)
    rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write(records)
    logger.info(
        "report history: re-issued #%d → v%d (%d bytes)", rid, rec["version"], len(pdf_bytes)
    )
    return _norm(rec)


def read_pdf(rid: int) -> Optional[bytes]:
    rec = get_record(rid)
    if not rec:
        return None
    fp = _dir() / rec["file"]
    if not fp.exists():
        return None
    return fp.read_bytes()


@locked(_index_path)
def delete_record(rid: int) -> bool:
    records = _read()
    keep, removed = [], None
    for r in records:
        if int(r.get("id", -1)) == int(rid):
            removed = r
        else:
            keep.append(r)
    if removed is None:
        return False
    # Best-effort remove the file too.
    try:
        (_dir() / removed["file"]).unlink(missing_ok=True)
    except OSError:
        pass
    _write(keep)
    return True
