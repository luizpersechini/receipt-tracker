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
import json
import random
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
        pool.append((fp, float(total)))
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


def extract_total(image_path: str):
    """OCR one receipt with the current config → (total_float_or_None, seconds)."""
    from PIL import Image

    from src.services.llm_processor import ocr_crops

    t0 = time.time()
    try:
        img = Image.open(image_path).convert("RGB")
        meta = ocr_crops([img])[0]["metadata"]
        v = meta.get("total_amount")
        got = float(v) if v not in (None, "") else None
    except Exception:  # noqa: BLE001
        got = None
    return got, time.time() - t0


def ok(got, gt) -> bool:
    return got is not None and abs(got - gt) <= max(0.01, abs(gt) * 0.005)


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

    print(f"OCR bake-off · {len(sample)} BRL receipts · seed {args.seed}\n")

    # results[label] = list of (got, sec, ok) aligned to sample order
    results: dict = {c[0]: [] for c in configs}
    for label, backend, vmodel, tmodel in configs:
        set_config(backend, vmodel, tmodel)
        for fp, gt in sample:
            got, sec = extract_total(fp)
            results[label].append((got, sec, ok(got, gt)))

    # ── table ──
    labels = [c[0] for c in configs]
    name_w = max(18, *(len(Path(fp).stem[-26:]) for fp, _ in sample))
    header = f"  {'receipt':<{name_w}}  {'truth':>9}  " + "  ".join(f"{lb:>14}" for lb in labels)
    print(header)
    print("  " + "─" * (len(header) - 2))
    for i, (fp, gt) in enumerate(sample):
        cells = []
        for lb in labels:
            got, sec, good = results[lb][i]
            mark = "✓" if good else "✗"
            cells.append(f"{(_fmt(got) + ' ' + mark):>14}")
        print(f"  {Path(fp).stem[-26:]:<{name_w}}  {_fmt(gt):>9}  " + "  ".join(cells))

    # ── summary ──
    print("  " + "─" * (len(header) - 2))
    summ = {}
    for lb in labels:
        rs = results[lb]
        correct = sum(1 for *_, g in rs if g)
        avg = sum(s for _, s, _ in rs) / len(rs)
        summ[lb] = (correct, len(rs), avg)
    acc_row = (
        "  "
        + f"{'accuracy':<{name_w}}  {'':>9}  "
        + "  ".join(
            f"{(str(c) + '/' + str(n) + ' ' + f'{c / n:.0%}'):>14}"
            for (c, n, _) in (summ[lb] for lb in labels)
        )
    )
    time_row = (
        "  "
        + f"{'avg time/receipt':<{name_w}}  {'':>9}  "
        + "  ".join(f"{f'{a:.1f}s':>14}" for (_, _, a) in (summ[lb] for lb in labels))
    )
    print(acc_row)
    print(time_row)

    # ── verdict ──
    best = max(labels, key=lambda lb: (summ[lb][0] / summ[lb][1], -summ[lb][2]))
    c, n, a = summ[best]
    print(f"\n  ▶ winner: {best} — {c}/{n} ({c / n:.0%}), {a:.1f}s/receipt")
    print("    (ground truth = stored totals; ones you've corrected are human-verified,")
    print("     the rest reflect what the app produced — a slight edge to the current pipeline.)")
    return 0


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


if __name__ == "__main__":
    sys.exit(main())
