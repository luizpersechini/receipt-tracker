#!/usr/bin/env python
"""Side-by-side: local segmenter vs local+Jev pair judgments — READ-ONLY.

Ground truth = the crops you already confirmed. Every original in
data/receipts that has `_segN.png` siblings was split through the bbox
editor, so the stored crops are the receipt regions a human accepted. Each
crop is located back in the original by template matching (downscaled), and
both methods are scored against those boxes:

  n        — how many regions each method found vs how many you kept
  hits     — truth boxes that some method box covers with IoU >= 0.5
  IoU      — mean best-IoU over truth boxes (1.0 = identical framing)

"local" is exactly what production runs today (`_compute_boxes`). "jev"
re-runs the text-cluster path but decides cluster merges with Jev's
same/different verdict when it clears the confidence floor, and with the
current gap rule otherwise. Ends with per-call latency + tokens.

Usage: .venv/bin/python scripts/compare_segments.py [--limit N] [--no-typesafe] [--only STEM]
Never writes to the DB, the data dir, or any JSON store.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # HEIC originals
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass

from src.config import settings  # noqa: E402
from src.services import receipt_segmenter as rs  # noqa: E402
from src.services import typesafe_segmenter as tsg  # noqa: E402

ORIG_EXT = (".heic", ".heif", ".jpg", ".jpeg", ".png")
_SEG_RE = re.compile(r"^(.*)_seg(\d+)\.png$")


# ─── ground truth: locate stored crops in the original ──────────────────────


def _find_cases(receipts_dir: Path, only: str | None) -> list[tuple[Path, list[Path]]]:
    segs: dict[str, list[Path]] = {}
    for p in receipts_dir.iterdir():
        m = _SEG_RE.match(p.name)
        if m:
            segs.setdefault(m.group(1), []).append(p)
    cases = []
    for stem, crops in sorted(segs.items()):
        if only and only not in stem:
            continue
        orig = next(
            (
                receipts_dir / f"{stem}{e}"
                for e in ORIG_EXT
                if (receipts_dir / f"{stem}{e}").exists()
            ),
            None,
        )
        if orig is None:
            continue
        cases.append((orig, sorted(crops, key=lambda c: int(_SEG_RE.match(c.name).group(2)))))
    return cases


def _locate_crop(orig_gray_small: np.ndarray, crop_path: Path, f: int) -> dict | None:
    """Template-match a stored crop (downscaled by f) inside the original.
    Returns {x,y,w,h,score} in full-res pixels, or None if the match is weak
    (crop rotated/edited after commit)."""
    import cv2
    from PIL import Image

    crop = Image.open(crop_path).convert("L")
    cw, ch = crop.size
    small = np.array(crop.resize((max(1, cw // f), max(1, ch // f))))
    H, W = orig_gray_small.shape
    if small.shape[0] >= H or small.shape[1] >= W:
        return None
    res = cv2.matchTemplate(orig_gray_small, small, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < 0.75:
        return None
    return {"x": loc[0] * f, "y": loc[1] * f, "w": cw, "h": ch, "score": float(score)}


# ─── scoring ────────────────────────────────────────────────────────────────


def _iou(a: dict, b: dict) -> float:
    ax1, ay1 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    iw = max(0, min(ax1, bx1) - max(a["x"], b["x"]))
    ih = max(0, min(ay1, by1) - max(a["y"], b["y"]))
    inter = iw * ih
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def _score(truth: list[dict], boxes: list[dict]) -> tuple[int, float]:
    if not truth:
        return 0, 0.0
    best = [max((_iou(t, b) for b in boxes), default=0.0) for t in truth]
    return sum(1 for v in best if v >= 0.5), sum(best) / len(best)


# ─── the Jev-judged variant of the text-cluster path ────────────────────────


def jev_boxes(
    image_path: str, W: int, H: int, *, call_typesafe: bool
) -> tuple[list[dict], list[dict], dict | None]:
    """Returns (boxes, decisions, call_metrics) via the same
    `typesafe_segmenter.judged_cluster_rects` production uses. Empty boxes
    when fewer than 2 regions result (production falls through to the
    image-only methods there). call_metrics is None when Jev wasn't
    called."""
    ocr_mod = rs._text_box_extractor()
    if ocr_mod is None:
        return [], [], None
    items = ocr_mod.extract_text_boxes_from_file(image_path, (W, H))
    if len(items) < 2:
        return [], [], None
    try:
        out = tsg.judged_cluster_rects(items, W, H, call_typesafe=call_typesafe)
    except tsg.TypeSafeError as e:
        print(f"    typesafe error → geometric only: {e}")
        out = tsg.judged_cluster_rects(items, W, H, call_typesafe=False)
    boxes = [rs._pad_box(r, W, H) for r in out["rects"]]
    if len(boxes) < 2:
        return [], out["decisions"], out["metrics"]
    boxes.sort(key=lambda b: (b["y"] // max(50, H // 20), b["x"]))
    return boxes, out["decisions"], out["metrics"]


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-typesafe", action="store_true")
    ap.add_argument("--only", default=None, help="substring filter on the original's stem")
    args = ap.parse_args()

    have_key = bool((settings.typesafe_api_key or os.environ.get("TYPESAFE_API_KEY") or "").strip())
    use_ts = have_key and not args.no_typesafe
    if not use_ts:
        print(
            "Jev column SKIPPED —",
            "flag --no-typesafe" if args.no_typesafe else "TYPESAFE_API_KEY unset",
        )

    cases = _find_cases(settings.receipts_dir, args.only)
    if args.limit:
        cases = cases[: args.limit]
    print(f"{len(cases)} originals with confirmed crops\n")

    import cv2

    F = 8
    tot = {
        "truth": 0,
        "local_n": 0,
        "jev_n": 0,
        "local_hits": 0,
        "jev_hits": 0,
        "local_iou": 0.0,
        "jev_iou": 0.0,
    }
    calls = []
    all_dec = []
    print(
        f"{'original':<44} {'truth':>5} {'local':>5} {'jev':>5} {'l.hit':>5} {'j.hit':>5} {'l.IoU':>6} {'j.IoU':>6}  note"
    )
    for orig, crops in cases:
        pil = rs._open_as_rgb(str(orig))
        W, H = pil.size
        gray_small = np.array(pil.convert("L").resize((max(1, W // F), max(1, H // F))))
        truth = []
        skipped = 0
        for c in crops:
            loc = _locate_crop(gray_small, c, F)
            if loc:
                truth.append(loc)
            else:
                skipped += 1
        local = rs._compute_boxes(str(orig), pil) or [{"x": 0, "y": 0, "w": W, "h": H}]
        jev, decisions, metrics = jev_boxes(str(orig), W, H, call_typesafe=use_ts)
        note = ""
        if not jev:
            jev = local
            note = "jev: <2 regions → same fallback as local"
        if skipped:
            note += f" {skipped} crop(s) not located (rotated/edited?)"
        if metrics:
            calls.append(metrics)
        for d in decisions:
            d["orig"] = orig.name
        all_dec.extend(decisions)

        lh, li = _score(truth, local)
        jh, ji = _score(truth, jev)
        tot["truth"] += len(truth)
        tot["local_n"] += len(local)
        tot["jev_n"] += len(jev)
        tot["local_hits"] += lh
        tot["jev_hits"] += jh
        tot["local_iou"] += li * len(truth)
        tot["jev_iou"] += ji * len(truth)
        print(
            f"{orig.name[:44]:<44} {len(truth):>5} {len(local):>5} {len(jev):>5} {lh:>5} {jh:>5} {li:>6.2f} {ji:>6.2f}  {note}"
        )

    n = max(tot["truth"], 1)
    print("\n== Totals ==")
    print(f"  truth regions : {tot['truth']}")
    print(
        f"  local  : {tot['local_n']} regions, hits {tot['local_hits']}/{tot['truth']}, mean IoU {tot['local_iou'] / n:.3f}"
    )
    print(
        f"  jev    : {tot['jev_n']} regions, hits {tot['jev_hits']}/{tot['truth']}, mean IoU {tot['jev_iou'] / n:.3f}"
    )

    if use_ts:
        flips = [d for d in all_dec if d["jev"] and d["final"] != d["geometric"]]
        low = [
            d
            for d in all_dec
            if d["jev"] and d["jev"]["confidence"] < settings.typesafe_min_confidence
        ]
        judged = [d for d in all_dec if d["jev"]]
        print(
            f"\n== Pair decisions: {len(judged)} judged, {len(flips)} overrode the gap rule, "
            f"{len(low)} below floor {settings.typesafe_min_confidence} (kept geometric) =="
        )
        print(
            f"{'original':<34} {'pair':<7} {'gap%':>5} {'geo':<6} {'jev':<20} {'conf':>5}  a_head | b_head"
        )
        for d in sorted(
            all_dec,
            key=lambda d: (d["final"] == d["geometric"], -(d["jev"] or {}).get("confidence", 0)),
        ):
            if not d["jev"]:
                continue
            print(
                f"{d['orig'][:34]:<34} {d['i']:>2}-{d['j']:<3} {d['gap_frac'] * 100:>5.1f} "
                f"{'merge' if d['geometric'] else 'split':<6} "
                f"{d['jev']['choice'] + (' (blocked)' if d.get('blocked') else ''):<20} {d['jev']['confidence']:>5.2f}  "
                f"{d['a_head']!r} ({d['n_i']}) | {d['b_head']!r} ({d['n_j']})"
            )
        print("\n== Per-call metrics ==")
        for k, c in enumerate(calls, 1):
            print(
                f"  call {k}: {c['pairs']} pairs  {c['latency_s'] * 1000:.0f} ms  "
                f"in={c.get('input_tokens')} out={c.get('output_tokens')} tokens"
            )
        tin = sum(c.get("input_tokens") or 0 for c in calls)
        tout = sum(c.get("output_tokens") or 0 for c in calls)
        npairs = sum(c["pairs"] for c in calls)
        print(
            f"  total: {npairs} pairs in {len(calls)} calls, {sum(c['latency_s'] for c in calls):.1f}s, "
            f"{tin} in / {tout} out tokens (~{tin / max(npairs, 1):.0f} in-tokens per pair)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
