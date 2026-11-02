"""Charge ↔ refund cancellation: a card charge and its exact estorno net to
zero, so neither needs a receipt.

Pairs are computed on read (no DB column) from the statements' parsed
transactions: same normalised description, same absolute amount to the
cent, opposite signs, within `WINDOW_DAYS` of each other. A refund can land
on a later bill, so pairing runs across every statement; same-statement
pairs win over cross-statement ones, then the nearest date. Each row is
used at most once, and a charge that already has a receipt matched to it
is never cancelled (the receipt claims it).
"""

from __future__ import annotations

import re
from datetime import date

WINDOW_DAYS = 60

Key = tuple[int, int]  # (statement_id, transaction_index)
_WS = re.compile(r"\s+")


def _norm_desc(s: str | None) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _cents(amount) -> int | None:
    try:
        return round(float(amount) * 100)
    except (TypeError, ValueError):
        return None


def _day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None


def _is_credit(txn: dict, cents: int) -> bool:
    return cents < 0 or bool((txn.get("raw_data") or {}).get("credit"))


def find_cancellations(
    statements: list[dict],
    locked: set[Key] | None = None,
    *,
    window_days: int = WINDOW_DAYS,
) -> dict[Key, Key]:
    """Return {(sid, idx): (partner_sid, partner_idx)} for both sides of every
    cancelled pair. `locked` holds (sid, idx) of charges that have a receipt
    matched — they are skipped."""
    locked = locked or set()
    charges: dict[tuple[str, int], list[tuple[Key, date]]] = {}
    credits: dict[tuple[str, int], list[tuple[Key, date]]] = {}
    for s in statements:
        sid = s.get("id")
        for i, t in enumerate(s.get("transactions") or []):
            cents = _cents(t.get("amount"))
            d = _day(t.get("date"))
            if cents is None or cents == 0 or d is None:
                continue
            key = (_norm_desc(t.get("description")), abs(cents))
            if _is_credit(t, cents):
                credits.setdefault(key, []).append(((sid, i), d))
            elif (sid, i) not in locked:
                charges.setdefault(key, []).append(((sid, i), d))

    out: dict[Key, Key] = {}
    for key, chs in charges.items():
        crs = credits.get(key)
        if not crs:
            continue
        cands = []
        for ck, cd in chs:
            for rk, rd in crs:
                gap = abs((rd - cd).days)
                if gap <= window_days:
                    cands.append((0 if ck[0] == rk[0] else 1, gap, ck, rk))
        cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
        for _, _, ck, rk in cands:
            if ck in out or rk in out:
                continue
            out[ck] = rk
            out[rk] = ck
    return out


def locked_from_matches(matches: list[dict]) -> set[Key]:
    """(statement_id, transaction_index) for every persisted match row."""
    out: set[Key] = set()
    for m in matches:
        sid, idx = m.get("statement_id"), m.get("transaction_index")
        if sid is not None and idx is not None:
            out.add((sid, idx))
    return out


def partner_info(partner: Key | None, statements_by_id: dict[int, dict]) -> dict | None:
    """JSON-ready pointer to the other side of a pair (for the UI/report)."""
    if partner is None:
        return None
    sid, idx = partner
    s = statements_by_id.get(sid) or {}
    txns = s.get("transactions") or []
    t = txns[idx] if 0 <= idx < len(txns) else {}
    return {
        "statement_id": sid,
        "index": idx,
        "date": t.get("date"),
        "statement_filename": s.get("filename"),
    }
