#!/usr/bin/env python3
"""
OCR bake-off — same receipts, multiple configs, scored against known totals.

Runs a FIXED random sample of your own BRL image receipts (ground truth = the
stored total) through each OCR config and prints a side-by-side accuracy +
timing table, so you can decide which pipeline to run.

Configs out of the box:
  • apple_vision + llama3.2:3b   (Apple OCR → small text LLM structures it)
  • apple_vision + llama3.1:8b   (same, bigger structurer)
  • glm-ocr (ollama_vision)      (one vision model does OCR + structuring)

Usage (use the venv interpreter — the app needs Python 3.10+):
  .venv/bin/python qa/bakeoff.py                  # 5 receipts, seed 42
  .venv/bin/python qa/bakeoff.py --n 8 --seed 7
  .venv/bin/python qa/bakeoff.py --apple-models llama3.2:3b,llama3.1:8b --glm glm-ocr:latest

Read-only on your data/ — it OCRs images, never writes to the DB.
"""

from __future__ import annotations

import argparse
import difflib
import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Needs Python 3.10+ (you're on {sys.version.split()[0]}). "
        "Run: .venv/bin/python qa/bakeoff.py"
    )

IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}


def sample_brl_receipts(n: int, seed: int):
    """Return [(image_path, stored_total)] — a fixed random sample of real BRL
    image receipts (Uber rides / non-image files excluded)."""
    from src.config import settings

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass

    db = Path(settings.db_path).resolve()
    if not db.exists():
        raise SystemExit(f"No DB at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT filepath, metadata FROM receipts").fetchall()
    con.close()

    pool = []
    for r in rows:
        try:
            m = json.loads(r["metadata"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        total, fp = m.get("total_amount"), r["filepath"]
        if not (total and fp) or (m.get("currency") or "").upper() != "BRL":
            continue
        p = Path(fp)
        if p.name.lower().startswith("uber_") or p.suffix.lower() not in IMG_EXT or not p.exists():
            continue
        pool.append((fp, m))  # full stored metadata = ground truth for all fields
    if not pool:
        raise SystemExit("No BRL image receipts with a total found in your data/.")
    return random.Random(seed).sample(pool, min(n, len(pool)))


def set_config(backend: str, vision_model: str | None, text_model: str | None):
    """Pin the runtime OCR getters for this config (overrides the picker file)."""
    from src.services import ocr_settings

    ocr_settings.effective_backend = lambda: backend
    if vision_model:
        ocr_settings.effective_vision_model = lambda: vision_model
    if text_model:
        ocr_settings.effective_text_model = lambda: text_model


def extract_meta(image_path: str):
    """OCR one receipt with the current config → (metadata_dict, seconds)."""
    from PIL import Image

    from src.services.llm_processor import ocr_crops

    t0 = time.time()
    try:
        img = Image.open(image_path).convert("RGB")
        meta = ocr_crops([img])[0]["metadata"] or {}
    except Exception:  # noqa: BLE001
        meta = {}
    return meta, time.time() - t0


# Fields to score and how. Vendor is fuzzy; the rest are exact-after-normalise.
FIELDS = ["total_amount", "currency", "date", "vendor_name", "category"]


def _norm_vendor(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def field_match(field, got, gt):
    """True/False if scorable, None when there's no ground truth to score."""
    if gt in (None, "", "Unknown") or (field == "total_amount" and not gt):
        return None
    if got in (None, ""):
        return False
    if field == "total_amount":
        try:
            g = float(got)
        except (TypeError, ValueError):
            return False
        return abs(g - float(gt)) <= max(0.01, abs(float(gt)) * 0.005)
    if field == "currency":
        return str(got).upper().strip() == str(gt).upper().strip()
    if field == "date":
        return str(got).strip()[:10] == str(gt).strip()[:10]  # ISO date part
    if field == "category":
        return str(got).lower().strip() == str(gt).lower().strip()
    if field == "vendor_name":
        a, b = _norm_vendor(got), _norm_vendor(gt)
        if not a or not b:
            return False
        if a in b or b in a:
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.82
    return None


def main():
    ap = argparse.ArgumentParser(description="OCR bake-off: glm vs apple_vision")
    ap.add_argument("--n", type=int, default=5, help="receipts to test")
    ap.add_argument("--seed", type=int, default=42, help="fixed sample seed")
    ap.add_argument("--apple-models", default="llama3.2:3b,llama3.1:8b")
    ap.add_argument("--glm", default="glm-ocr:latest", help="glm model (blank to skip)")
    args = ap.parse_args()

    import logging

    logging.getLogger("src").setLevel(logging.ERROR)

    sample = sample_brl_receipts(args.n, args.seed)
    configs = []  # (label, backend, vision_model, text_model)
    for tm in [m.strip() for m in args.apple_models.split(",") if m.strip()]:
        configs.append((f"apple+{tm.split(':')[0]}", "apple_vision", None, tm))
    if args.glm:
        configs.append((f"glm:{args.glm.split(':')[0]}", "ollama_vision", args.glm, None))

    labels = [c[0] for c in configs]
    print(f"OCR bake-off · {len(sample)} BRL receipts · seed {args.seed}")
    print("  fields: " + ", ".join(FIELDS) + "\n")

    # raw[label] = list of (metadata, sec) aligned to sample order
    raw: dict = {lb: [] for lb in labels}
    for label, backend, vmodel, tmodel in configs:
        set_config(backend, vmodel, tmodel)
        for fp, gt_meta in sample:
            meta, sec = extract_meta(fp)
            raw[label].append((meta, sec))

    # score[label][field] = (correct, scored); times[label] = avg sec
    score: dict = {lb: {f: [0, 0] for f in FIELDS} for lb in labels}
    times: dict = {}
    for lb in labels:
        times[lb] = sum(s for _, s in raw[lb]) / len(raw[lb])
        for i, (fp, gt_meta) in enumerate(sample):
            meta, _ = raw[lb][i]
            for f in FIELDS:
                res = field_match(f, meta.get(f), gt_meta.get(f))
                if res is None:
                    continue
                score[lb][f][1] += 1
                score[lb][f][0] += 1 if res else 0

    # ── per-field table (rows = fields, cols = configs) ──
    fw = max(len(f) for f in FIELDS)
    col = max(14, *(len(lb) for lb in labels))
    print("  " + f"{'field':<{fw}}  " + "  ".join(f"{lb:>{col}}" for lb in labels))
    print("  " + "─" * (fw + 2 + (col + 2) * len(labels)))

    def _cell(c, n):
        return f"{(str(c) + '/' + str(n) + ' ' + (f'{c / n:.0%}' if n else '—')):>{col}}"

    for f in FIELDS:
        print("  " + f"{f:<{fw}}  " + "  ".join(_cell(*score[lb][f]) for lb in labels))

    # overall (all fields pooled) + time
    print("  " + "─" * (fw + 2 + (col + 2) * len(labels)))
    overall = {
        lb: (sum(score[lb][f][0] for f in FIELDS), sum(score[lb][f][1] for f in FIELDS))
        for lb in labels
    }
    print("  " + f"{'OVERALL':<{fw}}  " + "  ".join(_cell(*overall[lb]) for lb in labels))
    print(
        "  "
        + f"{'avg time/rcpt':<{fw}}  "
        + "  ".join(f"{f'{times[lb]:.1f}s':>{col}}" for lb in labels)
    )

    # ── verdict ──
    best = max(labels, key=lambda lb: (overall[lb][0] / max(1, overall[lb][1]), -times[lb]))
    c, n = overall[best]
    print(
        f"\n  ▶ winner: {best} — {c}/{n} fields ({c / max(1, n):.0%}), {times[best]:.1f}s/receipt"
    )
    print("    (ground truth = stored totals; ones you've corrected are human-verified,")
    print("     the rest reflect what the app produced — a slight edge to the current pipeline.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
