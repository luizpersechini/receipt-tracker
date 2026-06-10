"""One-off SROIE diagnostic: run the real OCR pipeline over a seed-pinned
sample and dump EVERYTHING per receipt (ground truth, extracted total, vendor,
full Apple Vision OCR text) so parser misses can be analysed offline.

Usage:
  .venv/bin/python qa/diag_sroie.py --n 12 --seed 7 --out qa/diag_sroie_seed7.json

Uses the live configured backend/model (data/ocr_config.json) like the QA
corpus step does; no DB writes — only llm_processor.ocr_crops on PIL images.
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.services.llm_processor import ocr_crops  # noqa: E402
from src.services.money import parse_money  # noqa: E402


def _upscale(img, target=1500):
    w, h = img.size
    longest = max(w, h)
    if longest >= target:
        return img
    s = target / longest
    return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="qa/diag_sroie.json")
    args = ap.parse_args()

    from datasets import load_dataset

    ds = load_dataset("mp-02/sroie", split="test", streaming=True)
    names = ds.features["ner_tags"].feature.names
    tot_tag = next(i for i, nm in enumerate(names) if "total" in nm.lower())
    ds = ds.shuffle(seed=args.seed, buffer_size=64)
    samples = list(itertools.islice(ds, args.n))

    out = []
    for k, s in enumerate(samples, 1):
        toks = [w for w, t in zip(s["words"], s["ner_tags"]) if t == tot_tag]
        # abs(): SROIE prints some totals with a stray sign ('-140.65') — totals are positive
        cands = [abs(c) for c in (parse_money(w) for w in toks) if c]
        gt = max(cands) if cands else None
        meta = ocr_crops([_upscale(s["image"]).convert("RGB")])[0]["metadata"]
        got = meta.get("total_amount")
        ok = gt is not None and got is not None and abs(got - gt) <= max(0.01, abs(gt) * 0.005)
        out.append(
            {
                "i": k,
                "gt": gt,
                "got": got,
                "ok": ok,
                "gt_tokens": toks,
                "vendor": meta.get("vendor_name"),
                "currency": meta.get("currency"),
                "error": meta.get("error"),
                "ocr_text": meta.get("ocr_text") or "",
            }
        )
        print(f"[{k}/{len(samples)}] gt={gt} got={got} ok={ok}", flush=True)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    scored = [r for r in out if r["gt"] is not None]
    print(f"accuracy: {sum(r['ok'] for r in scored)}/{len(scored)} → {args.out}")


if __name__ == "__main__":
    main()
