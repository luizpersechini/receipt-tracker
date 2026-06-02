"""Per-transaction category overrides for statement items.

The parser guesses a category for each statement charge, which decides
its column on the report. The user can override that guess. But once a
receipt is matched to the charge, the **receipt's** category wins — so the
override only applies to unmatched (open) items. That precedence lives in
the report builder and the transactions endpoint; this module just stores
the override.

Keyed by (statement_id, transaction_index) in
data/statement_categories.json — separate from statements.transactions so
a re-parse doesn't wipe it. Same shape/handling as statement_notes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

# The canonical category set (matches receipts) plus a `fuel` value so the
# Combustível/Estacionamento column is reachable by an explicit choice.
VALID_CATEGORIES = {
    "meals",
    "transportation",
    "fuel",
    "travel",
    "lodging",
    "office_supplies",
    "other",
}


def _path() -> Path:
    return settings.data_dir / "statement_categories.json"


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("statement_categories.json unreadable (%s)", e)
        return {}


def _write(data: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_categories(statement_id: int) -> dict[int, str]:
    raw = _read().get(str(statement_id), {})
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def get_category(statement_id: int, transaction_index: int) -> str | None:
    return get_categories(statement_id).get(int(transaction_index))


def set_category(statement_id: int, transaction_index: int, category: str | None) -> None:
    """Save (or clear, when blank/invalid) a category override."""
    data = _read()
    sid = str(statement_id)
    idx = str(int(transaction_index))
    cat = (category or "").strip().lower()
    bucket = data.get(sid) or {}
    if cat in VALID_CATEGORIES:
        bucket[idx] = cat
    else:
        bucket.pop(idx, None)
    if bucket:
        data[sid] = bucket
    else:
        data.pop(sid, None)
    _write(data)


def clear_statement(statement_id: int) -> None:
    data = _read()
    if data.pop(str(statement_id), None) is not None:
        _write(data)
