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

logger = logging.getLogger(__name__)


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
    _index_path().write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


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
) -> dict:
    """Save a generated PDF + append an index record. Returns the record.

    `kind` is 'statement' or 'reimbursement'. `created_at` lets the caller
    pass a timestamp (tests); defaults to now.
    """
    records = _read()
    next_id = max((int(r.get("id", 0)) for r in records), default=0) + 1
    fname = f"report_{next_id}.pdf"
    (_dir() / fname).write_bytes(pdf_bytes)

    rec = {
        "id": next_id,
        "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "cardholder": cardholder or "",
        "currency": currency or "",
        "period": period or "",
        "size": len(pdf_bytes),
        "file": fname,
        "download_name": download_name or fname,
    }
    records.insert(0, rec)  # newest first
    _write(records)
    logger.info("report history: saved #%d (%s, %d bytes)", next_id, kind, len(pdf_bytes))
    return rec


def list_history() -> list[dict]:
    return _read()


def get_record(rid: int) -> Optional[dict]:
    for r in _read():
        if int(r.get("id", -1)) == int(rid):
            return r
    return None


def read_pdf(rid: int) -> Optional[bytes]:
    rec = get_record(rid)
    if not rec:
        return None
    fp = _dir() / rec["file"]
    if not fp.exists():
        return None
    return fp.read_bytes()


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
