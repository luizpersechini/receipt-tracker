"""Roster of report users (cardholders) for the expense-report front page.

Each user is a full report profile (same fields as report_profile) plus an
`id` and a display `nome`. Stored as a JSON list in data/report_users.json
(gitignored — holds CPF/RG and bank details).

The point of the roster: a company has several cardholders, each with their
own cost centre / bank / name. When you generate a report for a statement,
`guess_user_for_statement` reads the cardholder name off the statement
(filename + first-page PDF text) and fuzzy-matches it to a user so the right
profile is pre-selected.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from .json_store import locked, write_json
from .report_profile import DEFAULT_PROFILE

logger = logging.getLogger(__name__)


def _path() -> Path:
    return settings.data_dir / "report_users.json"


def _read() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("report_users.json unreadable (%s)", e)
        return []


def _write(users: list[dict]) -> None:
    write_json(_path(), users)


def _normalise(data: dict, uid: int) -> dict:
    """Return a profile dict merged over defaults, carrying the given id."""
    out = {
        **DEFAULT_PROFILE,
        **{
            k: ("" if v is None else str(v))
            for k, v in (data or {}).items()
            if k in DEFAULT_PROFILE
        },
    }
    out["id"] = uid
    return out


def list_users() -> list[dict]:
    return _read()


def get_user(uid: int) -> Optional[dict]:
    for u in _read():
        if int(u.get("id", -1)) == int(uid):
            return u
    return None


@locked(_path)
def create_user(data: dict) -> dict:
    users = _read()
    next_id = max((int(u.get("id", 0)) for u in users), default=0) + 1
    user = _normalise(data, next_id)
    users.append(user)
    _write(users)
    return user


@locked(_path)
def update_user(uid: int, patch: dict) -> Optional[dict]:
    users = _read()
    for i, u in enumerate(users):
        if int(u.get("id", -1)) == int(uid):
            merged = {
                **u,
                **{
                    k: ("" if v is None else str(v))
                    for k, v in (patch or {}).items()
                    if k in DEFAULT_PROFILE
                },
            }
            merged["id"] = int(uid)
            users[i] = merged
            _write(users)
            return merged
    return None


@locked(_path)
def delete_user(uid: int) -> bool:
    users = _read()
    new = [u for u in users if int(u.get("id", -1)) != int(uid)]
    if len(new) == len(users):
        return False
    _write(new)
    return True


# ─── Cardholder name guessing ───────────────────────────────────────────────

# Portuguese name particles + generic statement words that aren't part of a
# person's name — dropped before matching so they can't create false hits.
_STOPWORDS = {
    "de",
    "da",
    "do",
    "dos",
    "das",
    "e",
    "statement",
    "cartao",
    "cartão",
    "corporativo",
    "fatura",
    "itau",
    "itaú",
    "final",
    "visa",
    "master",
    "mastercard",
    "ltda",
    "sa",
}


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-zÀ-ÿ]{3,}", (text or "").lower())
    return {t for t in raw if t not in _STOPWORDS}


def extract_cardholder_text(statement: dict) -> str:
    """Best-effort blob of text likely to contain the cardholder name:
    the filename plus the first page of the statement PDF."""
    parts: list[str] = []
    fn = statement.get("filename") or ""
    # Strip the saved "statement_YYYYMMDD_HHMMSS_" prefix if present.
    fn = re.sub(r"^statement_\d{8}_\d{6}_", "", fn)
    parts.append(fn)

    fp = statement.get("filepath")
    if fp and str(fp).lower().endswith(".pdf"):
        try:
            import fitz

            doc = fitz.open(fp)
            try:
                # First page carries the holder name on Itaú statements.
                parts.append(doc[0].get_text())
            finally:
                doc.close()
        except Exception as e:  # pragma: no cover
            logger.debug("cardholder text extract failed: %s", e)
    return " ".join(parts)


def guess_user_for_statement(statement: dict) -> Optional[dict]:
    """Return the roster user whose name best matches the statement's
    cardholder text, or None if nothing matches confidently."""
    users = _read()
    if not users:
        return None
    cand = _tokens(extract_cardholder_text(statement))
    if not cand:
        return None

    best: Optional[dict] = None
    best_score = 0
    for u in users:
        ut = _tokens(u.get("nome") or "")
        if not ut:
            continue
        overlap = ut & cand
        # Require at least one "distinctive" (len>=4) token to overlap so a
        # bare common first name doesn't win on its own.
        distinctive = any(len(t) >= 4 for t in overlap)
        score = len(overlap)
        if score > best_score and distinctive:
            best_score = score
            best = u
    if best:
        logger.info(
            "guessed user '%s' for statement %s (score=%d)",
            best.get("nome"),
            statement.get("id"),
            best_score,
        )
    return best
