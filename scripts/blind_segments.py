#!/usr/bin/env python
"""Blind A/B page: today's auto segmentation vs the Jev-judged variant — READ-ONLY.

For every original in data/receipts that was split into crops, run both
methods on the ORIGINAL photo (no human edits involved), draw each method's
boxes on a downscaled copy, and emit a static HTML page that shows the two
renderings side by side with the sides shuffled per case. You click which
one is better; the page keeps a tally, and "Export" downloads a JSON of
your votes with the hidden mapping so the result can be scored.

Cases where both methods produce identical boxes are skipped (nothing to
judge).

Usage: .venv/bin/python scripts/blind_segments.py [--out DIR] [--limit N] [--only STEM]
Never writes to the DB or the data dir; output goes to --out (default qa/blind_out).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass

from PIL import ImageDraw  # noqa: E402

from compare_segments import _find_cases, jev_boxes  # noqa: E402
from src.config import settings  # noqa: E402
from src.services import receipt_segmenter as rs  # noqa: E402

MAX_H = 1100  # rendered image height


def _render(pil, boxes: list[dict], out: Path, colour=(230, 40, 40)) -> tuple[int, int]:
    W, H = pil.size
    scale = min(1.0, MAX_H / H)
    img = pil.resize((int(W * scale), int(H * scale))) if scale < 1 else pil.copy()
    d = ImageDraw.Draw(img)
    t = max(3, int(6 * scale))
    for i, b in enumerate(boxes, 1):
        x0, y0 = b["x"] * scale, b["y"] * scale
        x1, y1 = (b["x"] + b["w"]) * scale, (b["y"] + b["h"]) * scale
        d.rectangle([x0, y0, x1, y1], outline=colour, width=t)
        d.rectangle([x0, y0, x0 + 34, y0 + 30], fill=colour)
        d.text((x0 + 8, y0 + 6), str(i), fill=(255, 255, 255))
    img.save(out, "JPEG", quality=82)
    return img.size


HTML = """<!doctype html>
<meta charset="utf-8">
<title>Segmentation blind test</title>
<style>
 body{font:15px system-ui;margin:0;background:#111;color:#eee}
 header{position:sticky;top:0;background:#1b1b1b;padding:10px 16px;display:flex;gap:18px;align-items:center;border-bottom:1px solid #333;z-index:2}
 .case{padding:18px 16px;border-bottom:1px solid #2a2a2a}
 .case h3{margin:0 0 10px;font-weight:500;color:#aaa}
 .pair{display:flex;gap:14px}
 .side{flex:1;text-align:center}
 .side img{max-width:100%;max-height:78vh;border:2px solid #333;border-radius:4px;background:#000}
 .side.sel img{border-color:#4caf50}
 .side.sel .btn{background:#4caf50;color:#000}
 .btn{display:inline-block;margin-top:8px;padding:8px 18px;border-radius:6px;background:#333;cursor:pointer;user-select:none}
 .same{text-align:center;margin-top:8px}
 .same .btn{background:#2a2a2a}
 .same.sel .btn{background:#ffb300;color:#000}
 .n{color:#888;font-size:13px}
 .reveal{display:none;color:#ffb300;font-size:13px;margin-top:6px}
 body.revealed .reveal{display:block}
 button{padding:6px 12px;border-radius:6px;border:0;background:#444;color:#eee;cursor:pointer}
</style>
<header>
 <b>Which segmentation is better?</b>
 <span id="tally" class="n"></span>
 <span style="flex:1"></span>
 <button onclick="exportJson()">Export results</button>
 <button onclick="document.body.classList.toggle('revealed')">Reveal</button>
 <button onclick="if(confirm('Clear all votes?')){localStorage.removeItem(STORE_NAME);votes={};paint()}">Reset</button>
</header>
<div id="cases"></div>
<script>
const CASES = __CASES__;
const STORE_NAME = 'blind_segments_votes_v1';
let votes = {};
try { votes = JSON.parse(localStorage.getItem(STORE_NAME) || '{}'); } catch (e) { votes = {}; }
function vote(id, v){ votes[id] = v; try{localStorage.setItem(STORE_NAME, JSON.stringify(votes));}catch(e){} paint(); }
function paint(){
  let a=0,b=0,s=0, local=0, jev=0;
  for (const c of CASES){
    const v = votes[c.id];
    const el = document.getElementById('c'+c.id);
    el.querySelector('.L').classList.toggle('sel', v==='left');
    el.querySelector('.R').classList.toggle('sel', v==='right');
    el.querySelector('.same').classList.toggle('sel', v==='same');
    if (!v) continue;
    if (v==='same') s++; else { const m = v==='left' ? c.left : c.right; if (m==='local') local++; else jev++; }
  }
  const done = Object.keys(votes).length;
  document.getElementById('tally').textContent = `${done}/${CASES.length} voted · same: ${s}` +
    (document.body.classList.contains('revealed') ? ` · local: ${local} · jev: ${jev}` : '');
}
function exportJson(){
  const out = CASES.map(c => ({id:c.id, file:c.file, vote:votes[c.id]||null, left:c.left, right:c.right,
    winner: !votes[c.id] ? null : votes[c.id]==='same' ? 'same' : (votes[c.id]==='left' ? c.left : c.right)}));
  const blob = new Blob([JSON.stringify(out, null, 1)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'blind_segments_votes.json'; a.click();
}
const root = document.getElementById('cases');
CASES.forEach((c, i) => {
  const d = document.createElement('div'); d.className='case'; d.id='c'+c.id;
  d.innerHTML = `<h3>${i+1}. ${c.file} <span class="n">(${c.nL} vs ${c.nR} regions)</span></h3>
   <div class="pair">
    <div class="side L"><img src="${c.imgL}" loading="lazy"><div class="btn" onclick="vote('${c.id}','left')">Left is better</div><div class="reveal">${c.left}</div></div>
    <div class="side R"><img src="${c.imgR}" loading="lazy"><div class="btn" onclick="vote('${c.id}','right')">Right is better</div><div class="reveal">${c.right}</div></div>
   </div>
   <div class="same"><div class="btn" onclick="vote('${c.id}','same')">Same / can't tell</div></div>`;
  root.appendChild(d);
});
document.querySelector('header button:nth-of-type(2)').addEventListener('click', paint);
paint();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "qa" / "blind_out"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    have_key = bool((settings.typesafe_api_key or os.environ.get("TYPESAFE_API_KEY") or "").strip())
    if not have_key:
        print("TYPESAFE_API_KEY unset — nothing to compare")
        return 1

    out = Path(args.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    cases = _find_cases(settings.receipts_dir, args.only)
    if args.limit:
        cases = cases[: args.limit]

    seen_stems = set()
    page_cases = []
    skipped_same = 0
    for orig, _crops in cases:
        stem = orig.stem
        # the May test photo was uploaded nine times — judge it once
        key = stem.split("_", 3)[-1] if stem.startswith("receipt_") else stem
        if key in seen_stems:
            continue
        seen_stems.add(key)

        pil = rs._open_as_rgb(str(orig))
        W, H = pil.size
        local = rs._compute_boxes(str(orig), pil) or [{"x": 0, "y": 0, "w": W, "h": H}]
        jev, _dec, _m = jev_boxes(str(orig), W, H, call_typesafe=True)
        if not jev:
            jev = local  # production would fall through to the same image methods
        norm = lambda bs: sorted((b["x"], b["y"], b["w"], b["h"]) for b in bs)  # noqa: E731
        if norm(local) == norm(jev):
            skipped_same += 1
            print(f"  same boxes, skipped: {orig.name}")
            continue

        cid = hashlib.sha1(stem.encode()).hexdigest()[:8]
        img_local = out / "img" / f"{cid}_local.jpg"
        img_jev = out / "img" / f"{cid}_jev.jpg"
        _render(pil, local, img_local)
        _render(pil, jev, img_jev)
        left_is_local = int(cid[-1], 16) % 2 == 0
        page_cases.append(
            {
                "id": cid,
                "file": orig.name,
                "left": "local" if left_is_local else "jev",
                "right": "jev" if left_is_local else "local",
                "imgL": f"img/{img_local.name if left_is_local else img_jev.name}",
                "imgR": f"img/{img_jev.name if left_is_local else img_local.name}",
                "nL": len(local) if left_is_local else len(jev),
                "nR": len(jev) if left_is_local else len(local),
            }
        )
        print(f"  {orig.name}: local {len(local)} / jev {len(jev)} regions")

    (out / "index.html").write_text(
        HTML.replace("__CASES__", json.dumps(page_cases)), encoding="utf-8"
    )
    (out / "cases.json").write_text(json.dumps(page_cases, indent=1), encoding="utf-8")
    print(
        f"\n{len(page_cases)} cases written to {out / 'index.html'} ({skipped_same} identical, skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
