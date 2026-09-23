"""TypeSafe (Jev) pair judgments for receipt segmentation.

The segmenter finds receipts on a photo by clustering on-device text boxes,
then merges clusters whose gap is under a fixed threshold. That rule has no
idea what the text says, so a receipt's fiscal footer ("Tributos totais",
"Emissão", chave de acesso) ends up as its own region, or two receipts that
touch get glued together.

This module asks Jev one closed-set question per nearby cluster pair — same
receipt or different receipts — from the *text* of both clusters. Geometry
stays local; Jev never produces coordinates. Callers apply the confidence
floor (`settings.typesafe_min_confidence`) and keep the geometric decision
below it, so it can only override when it is sure.

Single HTTP seam: reuses `typesafe_categorizer._post` so tests stub one
function for both features.
"""

from __future__ import annotations

import logging
import time

from ..config import settings
from . import typesafe_categorizer as _ts

logger = logging.getLogger(__name__)

TypeSafeError = _ts.TypeSafeError

MAX_LINES_PER_SIDE = 12  # head + tail lines sent per cluster
MAX_LINE_CHARS = 60
MAX_PAIRS = 40  # one request per image; nearest pairs first

PAIR_CRITERIA = {
    "same_receipt": (
        "Both blocks are parts of ONE printed receipt or fiscal document: a "
        "merchant header above an items table, items above a totals/payment "
        "block, or a totals block above its fiscal footer (NFC-e number, "
        "'Emissão' date, chave de acesso, protocolo de autorização, QR-code "
        "note, 'Consumidor não identificado', 'Tributos totais'/IBPT line, "
        "'obrigado, volte sempre'). The second block does NOT start a new "
        "merchant header and does NOT carry a separate grand total for a "
        "different purchase."
    ),
    "different_receipts": (
        "The blocks are TWO separate receipts or documents: each has its own "
        "merchant name/CNPJ header, or its own VALOR TOTAL/TOTAL/payment "
        "block, or they describe different purchases (different merchants, "
        "dates or totals). A card-machine slip next to a fiscal receipt for "
        "the same purchase still counts as different documents."
    ),
}


def _trim_lines(lines: list[str]) -> list[str]:
    """Head + tail of a cluster's lines, shortened. The header and the totals/
    footer are what decide the question; the middle of an items table is
    noise."""
    clean = [(ln or "").strip()[:MAX_LINE_CHARS] for ln in lines if (ln or "").strip()]
    if len(clean) <= MAX_LINES_PER_SIDE:
        return clean
    half = MAX_LINES_PER_SIDE // 2
    return clean[:half] + ["…"] + clean[-half:]


def _layout(pair: dict) -> str:
    rel = pair.get("b_relative_to_a") or "near"
    gap = pair.get("gap_frac")
    gap_txt = f" with a gap of about {gap * 100:.0f}% of the photo" if gap is not None else ""
    return f"block b is {rel} block a{gap_txt}"


def build_payload(pairs: list[dict]) -> dict:
    """State = `pairs[]` with each side's lines + layout; one choice question
    per pair pointing at it by path so every question shares one state."""
    state = {
        "pairs": [
            {
                "a": {"lines": _trim_lines(p.get("a_lines") or [])},
                "b": {"lines": _trim_lines(p.get("b_lines") or [])},
                "layout": _layout(p),
            }
            for p in pairs
        ]
    }
    questions = {}
    for i in range(len(pairs)):
        questions[f"q{i}"] = {
            "type": "choice",
            "instructions": (
                f"`pairs[{i}].a.lines` and `pairs[{i}].b.lines` are the OCR text lines of "
                f"two nearby text blocks on one photo of paper receipts lying on a table; "
                f"`pairs[{i}].layout` says where b sits relative to a. Are the two blocks "
                "parts of the SAME receipt, or two DIFFERENT receipts? Receipts are mostly "
                "Brazilian (NFC-e/DANFE, cupom fiscal, card-machine slips) or international; "
                "OCR text is noisy and lines may be truncated."
            ),
            "criteria": PAIR_CRITERIA,
        }
    return {"state": state, "model": settings.typesafe_model, "questions": questions}


def judge_pairs(pairs: list[dict]) -> dict:
    """Judge up to MAX_PAIRS cluster pairs in ONE request.

    Each pair: {a_lines: [str], b_lines: [str], b_relative_to_a: str,
    gap_frac: float}. Returns {"results": [{same_receipt: bool, choice,
    confidence, probabilities}], "latency_s", "usage"} in input order.
    Raises TypeSafeError on config/transport failure — callers keep the
    geometric decision."""
    if not pairs:
        return {"results": [], "latency_s": 0.0, "usage": {}}
    if len(pairs) > MAX_PAIRS:
        raise ValueError(f"{len(pairs)} pairs exceeds MAX_PAIRS={MAX_PAIRS}")
    payload = build_payload(pairs)
    t0 = time.perf_counter()
    body = _ts._post(payload)
    latency = time.perf_counter() - t0

    answers = body.get("answers") or {}
    results = []
    for i in range(len(pairs)):
        ans = answers.get(f"q{i}") or {}
        choice = ans.get("choice")
        if choice not in PAIR_CRITERIA:
            logger.warning("TypeSafe returned unexpected pair choice %r", choice)
            choice, conf = "different_receipts", 0.0
        else:
            conf = float(ans.get("confidence") or 0.0)
        results.append(
            {
                "same_receipt": choice == "same_receipt",
                "choice": choice,
                "confidence": conf,
                "probabilities": ans.get("probabilities") or {},
            }
        )
    return {"results": results, "latency_s": latency, "usage": body.get("usage") or {}}


def is_enabled() -> bool:
    return (getattr(settings, "segment_backend", "local") or "local").strip().lower() == "typesafe"


def decide_or_fallback(result: dict | None, geometric: bool, *, label: str = "") -> bool:
    """Confidence policy for one pair: Jev's verdict at or above the floor,
    else the geometric rule's. Logs every fallback."""
    floor = float(settings.typesafe_min_confidence or 0.0)
    if not result:
        return geometric
    if result["confidence"] >= floor:
        return bool(result["same_receipt"])
    logger.info(
        "typesafe segmenter: %s conf %.2f < %.2f — keeping geometric %s%s",
        result["choice"],
        result["confidence"],
        floor,
        "merge" if geometric else "split",
        f" ({label})" if label else "",
    )
    return geometric


# ─── Judged clustering (used by the segmenter when enabled, and by the
# comparison scripts) ───────────────────────────────────────────────────────

PAIR_MAX_GAP_FRAC = 0.20  # only ask about cluster pairs closer than this


def cluster_indices(bboxes: list, eps: float) -> list[list[int]]:
    """Union-find over text boxes (gap < eps → same cluster), returning index
    groups so callers can recover each cluster's text."""
    from .receipt_segmenter import _bbox_gap

    n = len(bboxes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _bbox_gap(bboxes[i], bboxes[j]) < eps:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def relative_position(a: tuple, b: tuple) -> str:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if by >= ay + ah * 0.8:
        return "below"
    if by + bh <= ay + ah * 0.2:
        return "above"
    if bx >= ax + aw * 0.8:
        return "to the right of"
    if bx + bw <= ax + aw * 0.2:
        return "to the left of"
    return "overlapping"


def resolve_merges(
    n: int, cands: list[tuple], results: list, geometric, floor: float
) -> tuple[list[list[int]], list[dict]]:
    """Turn per-pair verdicts into cluster groups.

    `cands` = [(gap, i, j)], `results` = Jev result per cand (or None),
    `geometric(i, j, gap)` = today's rule. A confident "different" is a hard
    wall; confident "same" merges apply strongest-first, then geometric
    merges; any merge that would join groups holding a wall is skipped.
    Returns (groups of cluster indices, decisions for reporting)."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    walls: set[tuple[int, int]] = set()
    decisions = []
    for (g, i, j), res in zip(cands, results):
        geo = bool(geometric(i, j, g))
        confident = bool(res) and res["confidence"] >= floor
        same = decide_or_fallback(res, geo, label=f"pair {i}-{j}")
        if confident and not res["same_receipt"]:
            walls.add((i, j))
        decisions.append(
            {
                "i": i,
                "j": j,
                "gap": g,
                "geometric": geo,
                "jev": res,
                "final": same,
                "rank": res["confidence"] if confident else -1.0,
            }
        )

    def blocked(i: int, j: int) -> bool:
        gi = {k for k in range(n) if find(k) == find(i)}
        gj = {k for k in range(n) if find(k) == find(j)}
        return any((min(a, b), max(a, b)) in walls for a in gi for b in gj)

    for d in sorted((d for d in decisions if d["final"]), key=lambda d: -d["rank"]):
        i, j = d["i"], d["j"]
        if find(i) == find(j):
            continue
        if blocked(i, j):
            d["final"] = False
            d["blocked"] = True
            continue
        parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)
    return list(groups.values()), decisions


def judged_cluster_rects(text_items: list[dict], W: int, H: int, *, call_typesafe: bool = True) -> dict:
    """The text-cluster segmentation with Jev deciding merges.

    Same first pass as `receipt_segmenter._text_cluster_boxes` (union-find
    at CLUSTER_EPS_FRAC), then every cluster pair within PAIR_MAX_GAP_FRAC —
    singletons included, so a lone footer can be absorbed — is judged in one
    request. Groups that stay under CLUSTER_MIN_BOXES text boxes are dropped
    as noise, as today.

    Returns {"rects": [(x,y,w,h)], "decisions": [...], "metrics": {...}|None}.
    Raises TypeSafeError only if `call_typesafe` and the request fails —
    the caller decides to fall back to the geometric path."""
    from . import receipt_segmenter as rs

    bboxes = [it["bbox"] for it in text_items]
    maxdim = max(W, H)
    groups = cluster_indices(bboxes, maxdim * rs.CLUSTER_EPS_FRAC)
    rects = [rs._cluster_bbox([bboxes[i] for i in g]) for g in groups]
    merge_gap = maxdim * rs.CLUSTER_MERGE_GAP_FRAC
    pair_gap = maxdim * PAIR_MAX_GAP_FRAC

    def lines(g: list[int]) -> list[str]:
        return [
            text_items[i].get("text") or ""
            for i in sorted(g, key=lambda i: (bboxes[i][1], bboxes[i][0]))
        ]

    cands = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            g = rs._bbox_gap(rects[i], rects[j])
            if g < pair_gap:
                cands.append((g, i, j))
    cands.sort()
    cands = cands[:MAX_PAIRS]

    def geometric(i: int, j: int, g: float) -> bool:
        sized = len(groups[i]) >= rs.CLUSTER_MIN_BOXES and len(groups[j]) >= rs.CLUSTER_MIN_BOXES
        return sized and g < merge_gap

    results: list = [None] * len(cands)
    metrics = None
    if call_typesafe and cands:
        pairs = [
            {
                "a_lines": lines(groups[i]),
                "b_lines": lines(groups[j]),
                "b_relative_to_a": relative_position(rects[i], rects[j]),
                "gap_frac": g / maxdim,
            }
            for (g, i, j) in cands
        ]
        out = judge_pairs(pairs)
        results = out["results"]
        metrics = {"pairs": len(pairs), "latency_s": out["latency_s"], **(out["usage"] or {})}

    floor = float(settings.typesafe_min_confidence or 0.0)
    merged, decisions = resolve_merges(len(rects), cands, results, geometric, floor)
    for d in decisions:
        d["a_head"] = (lines(groups[d["i"]]) or [""])[0][:28]
        d["b_head"] = (lines(groups[d["j"]]) or [""])[0][:28]
        d["n_i"] = len(groups[d["i"]])
        d["n_j"] = len(groups[d["j"]])
        d["gap_frac"] = d["gap"] / maxdim

    out_rects = []
    for ks in merged:
        if sum(len(groups[k]) for k in ks) < rs.CLUSTER_MIN_BOXES:
            continue
        out_rects.append(rs._cluster_bbox([rects[k] for k in ks]))
    return {"rects": out_rects, "decisions": decisions, "metrics": metrics}
