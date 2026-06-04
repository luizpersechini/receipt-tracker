"""Pending box-review queue.

Pages with multiple receipts — from a direct PDF upload (every page) or a
folder scan (only pages the segmenter splits into >1 receipt) — wait here for
the user to confirm/adjust the receipt bounding boxes before OCR runs. Each
item points at a page image already saved in receipts_dir plus the segmenter's
suggested boxes. Stored in data/review_queue.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from ..config import settings

logger = logging.getLogger(__name__)


def _path() -> Path:
    return settings.data_dir / "review_queue.json"


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {"next_id": 1, "items": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            d.setdefault("next_id", 1)
            return d
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("review_queue.json unreadable (%s)", e)
    return {"next_id": 1, "items": []}


def _write(d: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def add(
    *,
    filename: str,
    image_w: int,
    image_h: int,
    bboxes: list,
    source: str = "",
    description: Optional[str] = None,
) -> dict:
    """Queue one page (already saved as `filename` in receipts_dir) with its
    suggested bounding boxes. Returns the created item (with its id)."""
    d = _read()
    item = {
        "id": d["next_id"],
        "filename": filename,
        "image_w": int(image_w),
        "image_h": int(image_h),
        "bboxes": bboxes or [],
        "source": source or "",
        "description": description,
    }
    d["items"].append(item)
    d["next_id"] = d["next_id"] + 1
    _write(d)
    return item


def list_items() -> list:
    return _read()["items"]


def get(item_id: int) -> Optional[dict]:
    for it in _read()["items"]:
        if it.get("id") == int(item_id):
            return it
    return None


def remove(item_id: int) -> bool:
    d = _read()
    before = len(d["items"])
    d["items"] = [it for it in d["items"] if it.get("id") != int(item_id)]
    if len(d["items"]) != before:
        _write(d)
        return True
    return False


def count() -> int:
    return len(_read()["items"])
