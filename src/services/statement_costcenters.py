"""Per-transaction 'Centro de Custo+Uneg' overrides.

The report's Centro de Custo+Uneg column defaults to the profile value
(e.g. 20101/AA1) on every row. On the real form the Uneg suffix varies per
line (AA1, AA2 … AA10) to allocate a charge to a specific trip/project, so
the user can override the value per line.

Keyed by (statement_id, transaction_index) in
data/statement_costcenters.json — separate from statements.transactions so a
re-parse doesn't wipe them. Same shape/handling as statement_notes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import settings
from .json_store import locked, write_json

logger = logging.getLogger(__name__)


def _path() -> Path:
    return settings.data_dir / "statement_costcenters.json"


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("statement_costcenters.json unreadable (%s)", e)
        return {}


def _write(data: dict) -> None:
    write_json(_path(), data)


def get_cost_centers(statement_id: int) -> dict[int, str]:
    """Return {transaction_index: cost_center} for one statement (int keys)."""
    raw = _read().get(str(statement_id), {})
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def get_cost_center(statement_id: int, transaction_index: int) -> str | None:
    return get_cost_centers(statement_id).get(int(transaction_index))


@locked(_path)
def set_cost_center(statement_id: int, transaction_index: int, value: str | None) -> None:
    """Save (or, when blank, clear) a cost-center override for one transaction."""
    data = _read()
    sid = str(statement_id)
    idx = str(int(transaction_index))
    text = (value or "").strip()
    bucket = data.get(sid) or {}
    if text:
        bucket[idx] = text
    else:
        bucket.pop(idx, None)
    if bucket:
        data[sid] = bucket
    else:
        data.pop(sid, None)
    _write(data)


@locked(_path)
def clear_statement(statement_id: int) -> None:
    """Drop all cost-center overrides for a statement (called when deleted)."""
    data = _read()
    if data.pop(str(statement_id), None) is not None:
        _write(data)


@locked(_path)
def remap_statement(statement_id: int, mapping: dict) -> None:
    """Re-key this statement's per-index entries after a reparse changed row
    order (old index -> new index). Entries whose old index isn't in
    `mapping` are dropped — better gone than attached to the wrong charge."""
    data = _read()
    sid = str(statement_id)
    bucket = data.get(sid) or {}
    if not bucket:
        return
    new_bucket = {}
    for k, v in bucket.items():
        try:
            old = int(k)
        except (TypeError, ValueError):
            continue
        if old in mapping:
            new_bucket[str(mapping[old])] = v
    if new_bucket:
        data[sid] = new_bucket
    else:
        data.pop(sid, None)
    _write(data)
