#!/usr/bin/env python3
"""
Receipt Tracker — disposable end-to-end QA harness.

Boots the FastAPI app in-process (TestClient) against a THROWAWAY db + data
dir under a tempfile — it never touches the live `data/`. Then it drives the
real flows end to end (optionally with the real OCR pipeline) and reports
every error with timing, so you can catch regressions before they hit live
data.

Usage:
    python qa/run_qa.py                 # real OCR (apple_vision + llama3.2:3b)
    python qa/run_qa.py --ocr mock      # stub OCR — fast, deterministic
    python qa/run_qa.py --loops 3       # run the battery 3 times
    python qa/run_qa.py --until-clean   # loop until a run is fully green (cap 5)
    python qa/run_qa.py --keep          # keep the temp dir for inspection

Exit code is 0 when every check passes (warnings allowed), 1 otherwise — so
it drops straight into CI or a pre-push hook.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# The app uses 3.10+ syntax (e.g. `dict | None`). Running this with an older
# system python3 (e.g. 3.9) fails with a cryptic TypeError deep in an import,
# so fail fast with the fix instead.
if sys.version_info < (3, 10):
    raise SystemExit(
        f"This project needs Python 3.10+ (you're on {sys.version.split()[0]}).\n"
        "Run the QA harness with the project venv interpreter:\n"
        "  .venv/bin/python qa/run_qa.py --corpus 8"
    )

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── disposable app ──────────────────────────────────────────────────────────


def build_client(
    tmp: Path,
    ocr_mode: str,
    text_model: str = "llama3.2:3b",
    backend: str = "apple_vision",
    structure: str = "ollama",
    anthropic_model: str = None,
):
    """Repoint ALL storage to a throwaway dir, init a fresh DB, return a
    TestClient. Asserts nothing points at the live data/ before continuing."""
    from src.config import ensure_dirs, settings

    data = tmp / "data"
    settings.data_dir = data
    settings.db_path = data / "qa.db"
    settings.receipts_dir = data / "receipts"
    settings.statements_dir = data / "statements"
    settings.reports_dir = data / "reports"
    settings.watch_log_path = data / "watch_log.jsonl"
    settings.watch_folder = ""  # never start the watcher
    settings.ocr_warmup = False  # don't preload a model
    # Real OCR runs a working on-device text-extraction path (Apple Vision on
    # macOS, PaddleOCR elsewhere) + small text LLM — NOT the ollama_vision/
    # glm-ocr one-shot (which loops on the prompt). The backend is selectable
    # via --backend so the corpus can A/B Apple Vision vs PaddleOCR.
    settings.ocr_backend = backend
    settings.ocr_text_model = text_model
    # Structuring backend (separate from OCR): "ollama" (local) or "anthropic"
    # (hosted Claude API — needs ANTHROPIC_API_KEY in the env). Lets the corpus
    # A/B a strong remote structurer against the local text model.
    settings.structure_backend = structure
    if anthropic_model:
        settings.anthropic_model = anthropic_model

    # Safety net: refuse to run if any path escaped the temp sandbox.
    for p in (
        settings.db_path,
        settings.receipts_dir,
        settings.statements_dir,
        settings.reports_dir,
        settings.data_dir,
    ):
        assert str(Path(p).resolve()).startswith(str(tmp.resolve())), f"unsafe path {p}"

    ensure_dirs()
    from src.db.database import init_db

    init_db()

    from fastapi.testclient import TestClient

    from src.main import app

    client = TestClient(app)

    if ocr_mode == "mock":
        import src.routers.receipts as rr

        def fake_ocr_crops(crops):
            return [
                {
                    "metadata": {
                        "vendor_name": "QA Vendor",
                        "total_amount": 12.34,
                        "currency": "CAD",
                        "date": "2026-05-01",
                        "category": "meals",
                    },
                    "crop": c,
                }
                for c in crops
            ]

        rr.ocr_crops = fake_ocr_crops

    return client


# ─── synthetic fixtures ──────────────────────────────────────────────────────


def _font(size: int):
    from PIL import ImageFont

    for p in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_receipt_image(path: Path, lines: list[str], size=(820, 1040)):
    """Render a clean printed-receipt image so Apple Vision reads it reliably."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = _font(34)
    y = 60
    for ln in lines:
        d.text((70, y), ln, fill="black", font=f)
        y += 58
    img.save(str(path))


def make_two_receipt_image(path: Path):
    """Two receipts with a wide horizontal gap → segmenter should find 2."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1800, 1100), "white")
    d = ImageDraw.Draw(img)
    f = _font(34)
    blocks = [
        (90, ["CAFE ALPHA", "Vancouver BC", "Total  11.20", "VISA 1234", "2026-05-02"]),
        (1080, ["BETA DINER", "Toronto ON", "Total  27.80", "VISA 5678", "2026-05-03"]),
    ]
    for x, lines in blocks:
        y = 90
        for ln in lines:
            d.text((x, y), ln, fill="black", font=f)
            y += 64
    img.save(str(path))


def make_statement_csv(path: Path):
    rows = [
        ["date", "description", "amount"],
        ["2026-05-02", "CAFE ALPHA VANCOUVER", "11.20"],
        ["2026-05-03", "BETA DINER TORONTO", "27.80"],
        ["2026-05-04", "UBER TRIP", "18.40"],
    ]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def make_pdf(path: Path, pages: int = 1):
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=420, height=600)
    doc.save(str(path))
    doc.close()


# ─── check registry ──────────────────────────────────────────────────────────


@dataclass
class Result:
    name: str
    ok: bool
    secs: float
    detail: str = ""
    note: str = ""  # non-fatal observation / warning


CHECKS: list = []


def check(name: str):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn

    return deco


class Warn(Exception):
    """Raise to record a non-fatal warning instead of a hard failure."""


def _post_file(client, url, filename, blob, mime, data=None):
    return client.post(url, files={"file": (filename, blob, mime)}, data=data or {})


# ─── checks ──────────────────────────────────────────────────────────────────


@check("health: GET /")
def _c_root(client, ctx):
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "Receipt Tracker" in r.text


@check("health: core GET endpoints")
def _c_endpoints(client, ctx):
    eps = [
        "/api/receipts",
        "/api/statements",
        "/api/stats",
        "/api/ocr/config",
        "/api/review-queue",
        "/api/watch-folder/status",
        "/api/matches",
    ]
    bad = []
    for e in eps:
        try:
            if client.get(e).status_code != 200:
                bad.append(f"{e}={client.get(e).status_code}")
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{e}!{ex}")
    assert not bad, "; ".join(bad)


@check("ocr config: QA env on selected text-extraction backend")
def _c_ocrcfg(client, ctx):
    cfg = client.get("/api/ocr/config").json()
    expected = ctx.get("backend", "apple_vision")
    assert cfg["backend"] == expected, f"{cfg.get('backend')} != {expected}"


@check("receipt: single-flow preview→commit→edit→delete")
def _c_single(client, ctx):
    img = ctx["tmp"] / "single.png"
    make_receipt_image(
        img,
        [
            "CAFE QATEST",
            "123 Main St",
            "Vancouver BC",
            "Latte 4.50",
            "Total 12.34",
            "VISA 1234",
            "2026-05-01",
        ],
    )
    with open(img, "rb") as f:
        prev = _post_file(
            client,
            "/api/upload/receipt/preview",
            "single.png",
            f.read(),
            "image/png",
            {"single": "true"},
        ).json()
    assert prev.get("forced_single") is True, prev
    assert len(prev["bboxes"]) == 1
    commit = client.post(
        "/api/upload/receipt/commit",
        json={"filename": prev["filename"], "bboxes": prev["bboxes"], "description": "qa"},
    ).json()
    assert commit.get("count", 0) >= 1, commit
    meta = commit["receipts"][0]["metadata"]
    rid = commit["receipts"][0]["receipt_id"]
    note = ""
    if ctx["ocr"] == "real":
        # Apple Vision should read the rendered text; flag if extraction is empty.
        if not meta.get("total_amount"):
            raise Warn(f"real OCR recovered no total_amount (vendor={meta.get('vendor_name')!r})")
        note = f"vendor={meta.get('vendor_name')!r} total={meta.get('total_amount')} {meta.get('currency')}"
    # edit + delete
    assert (
        client.patch(f"/api/receipts/{rid}", json={"description": "qa-edited"}).status_code == 200
    )
    assert client.delete(f"/api/receipts/{rid}").status_code == 200
    ids = [r["id"] for r in client.get("/api/receipts").json()["receipts"]]
    assert rid not in ids, "receipt survived delete"
    return note


@check("receipt: multi-segment preview→commit")
def _c_multi(client, ctx):
    img = ctx["tmp"] / "multi.png"
    make_two_receipt_image(img)
    with open(img, "rb") as f:
        prev = _post_file(
            client, "/api/upload/receipt/preview", "multi.png", f.read(), "image/png"
        ).json()
    nboxes = len(prev.get("bboxes") or [])
    if not prev.get("auto_segmented") or nboxes < 2:
        raise Warn(
            f"segmenter found {nboxes} box(es) on a 2-receipt image "
            f"(auto_segmented={prev.get('auto_segmented')}) — may be over-merging"
        )
    commit = client.post(
        "/api/upload/receipt/commit", json={"filename": prev["filename"], "bboxes": prev["bboxes"]}
    ).json()
    assert commit.get("count", 0) >= 1, commit
    # cleanup
    for r in commit.get("receipts", []):
        client.delete(f"/api/receipts/{r['receipt_id']}")
    return f"{nboxes} boxes → {commit.get('count')} receipts"


@check("statement: upload→list→complete→reopen→delete")
def _c_statement(client, ctx):
    csvp = ctx["tmp"] / "stmt.csv"
    make_statement_csv(csvp)
    with open(csvp, "rb") as f:
        up = _post_file(client, "/api/upload/statement", "itau.csv", f.read(), "text/csv").json()
    assert up.get("success") or up.get("statement_id") or up.get("transaction_count") is not None, (
        up
    )
    rows = client.get("/api/statements").json()["statements"]
    assert rows, "no statements after upload"
    sid = rows[0]["id"]
    a = client.post(f"/api/statements/{sid}/archive", json={"archived": True}).json()
    assert a["archived"] is True
    assert client.get("/api/statements").json()["statements"][0]["archived"] == 1
    client.post(f"/api/statements/{sid}/archive", json={"archived": False})
    assert client.delete(f"/api/statements/{sid}").status_code == 200
    ctx["stmt_txns"] = up.get("transaction_count")
    return f"{up.get('transaction_count')} txns parsed"


@check("matching: POST /match runs without error")
def _c_match(client, ctx):
    # A corp_card receipt + a statement that should pair on amount/date.
    img = ctx["tmp"] / "matchrcpt.png"
    make_receipt_image(img, ["CAFE ALPHA", "Vancouver BC", "Total 11.20", "2026-05-02"])
    with open(img, "rb") as f:
        prev = _post_file(
            client,
            "/api/upload/receipt/preview",
            "m.png",
            f.read(),
            "image/png",
            {"single": "true"},
        ).json()
    client.post(
        "/api/upload/receipt/commit", json={"filename": prev["filename"], "bboxes": prev["bboxes"]}
    )
    csvp = ctx["tmp"] / "m.csv"
    make_statement_csv(csvp)
    with open(csvp, "rb") as f:
        _post_file(client, "/api/upload/statement", "m.csv", f.read(), "text/csv")
    r = client.post("/api/match")
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    stats = client.get("/api/stats").json()
    return f"stats keys={sorted(stats)[:4]}"


@check("review queue: PDF preview queues pages, commit drains")
def _c_review(client, ctx):
    pdf = ctx["tmp"] / "doc.pdf"
    make_pdf(pdf, pages=2)
    with open(pdf, "rb") as f:
        prev = _post_file(
            client, "/api/upload/receipt/preview", "doc.pdf", f.read(), "application/pdf"
        ).json()
    assert prev.get("review_queued", 0) >= 1, prev
    q = client.get("/api/review-queue").json()
    assert q["count"] >= 1, q
    item = q["items"][0]
    commit = client.post(
        f"/api/review-queue/{item['id']}/commit",
        json={
            "bboxes": item["bboxes"]
            or [{"x": 0, "y": 0, "w": item["image_size"]["w"], "h": item["image_size"]["h"]}]
        },
    )
    assert commit.status_code == 200, f"{commit.status_code}: {commit.text[:200]}"
    after = client.get("/api/review-queue").json()
    assert all(it["id"] != item["id"] for it in after["items"]), "item not drained"
    # cleanup any receipts created
    for r in client.get("/api/receipts").json()["receipts"]:
        client.delete(f"/api/receipts/{r['id']}")
    return f"queued {prev['review_queued']} page(s)"


@check("report: expense report generates")
def _c_report(client, ctx):
    # seed one receipt in-period
    img = ctx["tmp"] / "rep.png"
    make_receipt_image(img, ["REPORT CAFE", "Vancouver BC", "Total 20.00", "2026-05-05"])
    with open(img, "rb") as f:
        prev = _post_file(
            client,
            "/api/upload/receipt/preview",
            "rep.png",
            f.read(),
            "image/png",
            {"single": "true"},
        ).json()
    client.post(
        "/api/upload/receipt/commit", json={"filename": prev["filename"], "bboxes": prev["bboxes"]}
    )
    try:
        r = client.post(
            "/api/reports/expense",
            json={
                "report_type": "all",
                "target_currency": "CAD",
                "period_start": "2026-05-01",
                "period_end": "2026-05-31",
            },
        )
    except Exception as ex:  # noqa: BLE001
        raise Warn(f"report raised (often FX/network offline): {ex}")
    if r.status_code != 200:
        raise Warn(f"report returned {r.status_code} (often FX/network): {r.text[:150]}")
    return "expense report ok"


# ─── real-world corpora: Portuguese + English receipts, scored ───────────────
# Focused on the receipts this app actually sees:
#   • EN — random real English receipts from the SROIE dataset (mp-02/sroie,
#     HuggingFace), with ground-truth totals reconstructed from the S-TOTAL
#     NER tag. Images are small (512px tall), so we upscale before OCR.
#   • PT — a random sample of YOUR OWN Brazilian (BRL) receipts from the live
#     data/, re-OCR'd and scored against the stored total (true ground truth
#     for the ones you've corrected; a regression signal for the rest).
# Both score total-amount accuracy, append to qa/corpus_scoreboard.jsonl
# (per-corpus), and print a trend. Need `datasets` (EN) / a live DB (PT);
# warn (never hard-fail) when unavailable. Real OCR only.

SCOREBOARD = ROOT / "qa" / "corpus_scoreboard.jsonl"


def _to_amount(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _amount_ok(got, gt):
    if got is None or gt is None:
        return False
    return abs(got - gt) <= max(0.01, abs(gt) * 0.005)  # cents + small relative


def _fmt_amt(v):
    if v is None:
        return "None"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _upscale(img, target=1500):
    from PIL import Image as _Im

    w, h = img.size
    longest = max(w, h)
    if longest >= target:
        return img
    scale = target / longest
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), _Im.LANCZOS)


def _append_scoreboard(ctx, corpus, scored, correct, acc, fields=None):
    from src.services import ocr_settings
    from src.config import settings

    # Label the row with whatever actually structured the text, so an
    # anthropic run isn't mislabeled with the unused local Ollama model.
    if (settings.structure_backend or "ollama").strip().lower() == "anthropic":
        structurer = f"anthropic:{settings.anthropic_model}"
    else:
        structurer = ocr_settings.effective_text_model()

    rec = {
        "ts": ctx["ts"],
        "corpus": corpus,
        "backend": ocr_settings.effective_backend(),
        "text_model": structurer,
        "n": len(scored),
        "correct": correct,
        "accuracy": round(acc, 3),
        "samples": [{"gt": r["gt"], "got": r["got"], "ok": r["ok"]} for r in scored],
    }
    if fields:
        # Per-field accuracy (own-pt scores total + vendor + date). Top-level
        # accuracy stays = total, so the existing trend keeps working.
        rec["fields"] = {
            k: {"correct": c, "n": n, "accuracy": round(a, 3)} for k, (c, n, a) in fields.items()
        }
    SCOREBOARD.parent.mkdir(parents=True, exist_ok=True)
    with open(SCOREBOARD, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _score_pairs(ctx, corpus, pairs):
    """pairs: list of (PIL image, gt_float). OCR each, score the extracted
    total, append the scoreboard, return a one-line note."""
    from src.services.llm_processor import ocr_crops

    rows = []
    for img, gt in pairs:
        try:
            meta = ocr_crops([img.convert("RGB")])[0]["metadata"]
            got = _to_amount(meta.get("total_amount"))
        except Exception:  # noqa: BLE001
            got = None
        rows.append({"gt": gt, "got": got, "ok": _amount_ok(got, gt)})
    scored = [r for r in rows if r["gt"] is not None]
    if not scored:
        raise Warn("no ground-truth totals in this sample")
    correct = sum(r["ok"] for r in scored)
    acc = correct / len(scored)
    _append_scoreboard(ctx, corpus, scored, correct, acc)
    miss = [f"{_fmt_amt(r['gt'])}→{_fmt_amt(r['got'])}" for r in scored if not r["ok"]][:6]
    note = f"{correct}/{len(scored)} totals correct ({acc:.0%})"
    if miss:
        note += " · gt→got misses: " + ", ".join(miss)
    return note


def _norm_vendor(s):
    """Lowercase, strip accents, collapse to alnum words — so 'Sorveteria
    Parada Rápida' == 'SORVETERIA PARADA RAPIDA'."""
    import unicodedata

    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _vendor_ok(got, gt):
    """None → no ground truth to score (gt blank / 'Unknown'). Else a fuzzy
    match tolerant of case, accents, and truncation."""
    t = _norm_vendor(gt)
    if not t or t == "unknown":
        return None
    g = _norm_vendor(got)
    if not g:
        return False
    if g in t or t in g:  # truncation either way
        return True
    import difflib

    return difflib.SequenceMatcher(None, g, t).ratio() >= 0.82


def _date_ok(got, gt):
    """None → no ground-truth date. Else exact YYYY-MM-DD match."""
    if not gt:
        return None
    return (got or "")[:10] == str(gt)[:10]


def _score_own_pt(ctx, pairs):
    """own-pt scorer: pairs are (PIL image, gt_total, gt_vendor, gt_date).
    Scores total + vendor + date against the stored values so we can iterate
    on vendor/date *with measurement*, not guesses. Appends a per-field
    scoreboard row and returns a multi-field note."""
    from src.services.llm_processor import ocr_crops

    tot, ven, dat, tot_miss, ven_miss = [], [], [], [], []
    for img, gt_total, gt_vendor, gt_date in pairs:
        try:
            meta = ocr_crops([img.convert("RGB")])[0]["metadata"]
        except Exception:  # noqa: BLE001
            meta = {}
        got_t = _to_amount(meta.get("total_amount"))
        if gt_total is not None:
            ok = _amount_ok(got_t, gt_total)
            tot.append(ok)
            if not ok:
                tot_miss.append(f"{_fmt_amt(gt_total)}→{_fmt_amt(got_t)}")
        v = _vendor_ok(meta.get("vendor_name"), gt_vendor)
        if v is not None:
            ven.append(v)
            if not v:
                ven_miss.append(f"{str(gt_vendor)[:16]}→{str(meta.get('vendor_name'))[:16]}")
        d = _date_ok(meta.get("date"), gt_date)
        if d is not None:
            dat.append(d)
    if not tot:
        raise Warn("no ground-truth totals in this sample")

    def _acc(xs):
        return (sum(xs), len(xs), (sum(xs) / len(xs)) if xs else 0.0)

    fields = {"total": _acc(tot), "vendor": _acc(ven), "date": _acc(dat)}
    tc, tn, ta = fields["total"]
    _append_scoreboard(
        ctx, "own-pt", [{"gt": None, "got": None, "ok": o} for o in tot], tc, ta, fields=fields
    )
    note = " · ".join(f"{k} {c}/{n} ({a:.0%})" for k, (c, n, a) in fields.items() if n)
    if ven_miss:
        note += " · vendor misses: " + ", ".join(ven_miss[:4])
    return note


@check("corpus EN: SROIE total accuracy (random sample, scored)")
def _c_sroie(client, ctx):
    if ctx["ocr"] != "real":
        raise Warn("needs --ocr real (mock can't read totals)")
    if ctx["corpus_n"] <= 0:
        raise Warn("corpus disabled (--corpus 0)")
    try:
        from datasets import load_dataset
    except ImportError:
        raise Warn("`uv pip install datasets` to enable the SROIE corpus")

    import itertools

    from src.services.money import parse_money

    try:
        ds = load_dataset("mp-02/sroie", split="test", streaming=True)
        names = ds.features["ner_tags"].feature.names
        tot_tag = next(i for i, nm in enumerate(names) if "total" in nm.lower())
        ds = ds.shuffle(seed=ctx["seed"], buffer_size=64)
        samples = list(itertools.islice(ds, ctx["corpus_n"]))
    except Exception as e:  # noqa: BLE001
        raise Warn(f"couldn't load SROIE (offline / HF down?): {str(e)[:110]}")

    pairs = []
    for s in samples:
        toks = [w for w, t in zip(s["words"], s["ner_tags"]) if t == tot_tag]
        # abs(): SROIE prints some totals with a stray sign ('-140.65') — totals are positive
        cands = [abs(c) for c in (parse_money(w) for w in toks) if c]
        gt = max(cands) if cands else None  # grand total = largest tagged amount
        pairs.append((_upscale(s["image"]), gt))  # tiny images → upscale for OCR
    return _score_pairs(ctx, "sroie-en", pairs)


@check("corpus PT: your own BRL receipts vs stored total (regression)")
def _c_own_pt(client, ctx):
    if ctx["ocr"] != "real":
        raise Warn("needs --ocr real")
    if ctx["corpus_n"] <= 0:
        raise Warn("corpus disabled (--corpus 0)")
    import sqlite3

    from PIL import Image

    real_db = ctx.get("real_db")
    if not real_db or not Path(real_db).exists():
        raise Warn("no live DB found to sample your own receipts from")
    try:
        con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT filepath, metadata FROM receipts").fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        raise Warn(f"couldn't read live DB read-only: {str(e)[:90]}")

    # Only real photographed receipts — exclude Uber rides (CSV/Gmail imports,
    # often PDFs with no receipt image) and any non-image file.
    img_ext = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
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
        if p.name.lower().startswith("uber_") or p.suffix.lower() not in img_ext:
            continue
        if p.exists():
            pool.append((fp, float(total), m.get("vendor_name"), m.get("date")))
    if not pool:
        raise Warn("no BRL image receipts with a total in your live data/")

    # Many of your BRL receipts are HEIC (iPhone) — register the opener so
    # they're not silently skipped (which would empty the sample).
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass

    sample = random.Random(ctx["seed"]).sample(pool, min(ctx["corpus_n"], len(pool)))
    pairs = []
    for fp, gt, vendor, date in sample:
        try:
            pairs.append((Image.open(fp), gt, vendor, date))
        except Exception:  # noqa: BLE001
            continue
    if not pairs:
        raise Warn("sampled receipts couldn't be opened (unsupported format?)")
    return _score_own_pt(ctx, pairs)


def _print_scoreboard_trend():
    if not SCOREBOARD.exists():
        return
    try:
        recs = [
            json.loads(x) for x in SCOREBOARD.read_text(encoding="utf-8").splitlines() if x.strip()
        ]
    except Exception:  # noqa: BLE001
        return
    by_corpus: dict = {}
    for r in recs:
        by_corpus.setdefault(r.get("corpus", "?"), []).append(r)
    if not by_corpus:
        return
    print("\n  Field-accuracy trend (last 3 runs per corpus)")
    print("  " + "─" * 64)
    for corpus, rs in by_corpus.items():
        for r in rs[-3:]:
            line = (
                f"    [{corpus:<8}] {r.get('ts', '?'):<20} {r.get('text_model', '?'):<13} "
                f"total {r.get('correct', 0)}/{r.get('n', 0)} {r.get('accuracy', 0):.0%}"
            )
            f = r.get("fields") or {}
            for k in ("vendor", "date"):
                if k in f and f[k].get("n"):
                    line += f"  {k} {f[k]['correct']}/{f[k]['n']} {f[k]['accuracy']:.0%}"
            print(line)


# ─── runner ──────────────────────────────────────────────────────────────────


def run_battery(client, ctx) -> list:
    results = []
    for name, fn in CHECKS:
        t0 = time.time()
        try:
            note = fn(client, ctx) or ""
            results.append(Result(name, True, time.time() - t0, note=note))
        except Warn as w:
            results.append(Result(name, True, time.time() - t0, note=f"WARN: {w}"))
        except Exception:  # noqa: BLE001
            results.append(
                Result(
                    name,
                    False,
                    time.time() - t0,
                    detail=traceback.format_exc().strip().splitlines()[-1],
                )
            )
    return results


def print_report(results, run_idx, total_runs):
    head = f"  QA run {run_idx}/{total_runs}" if total_runs > 1 else "  QA run"
    print(f"\n{head}  ·  {time.strftime('%H:%M:%S')}")
    print("  " + "─" * 64)
    for r in results:
        icon = "✓" if r.ok and not r.note.startswith("WARN") else ("!" if r.ok else "✗")
        line = f"  {icon} {r.name:<46} {r.secs:5.1f}s"
        if r.note:
            line += f"\n      {r.note}"
        if not r.ok:
            line += f"\n      → {r.detail}"
        print(line)
    fails = [r for r in results if not r.ok]
    warns = [r for r in results if r.ok and r.note.startswith("WARN")]
    print("  " + "─" * 64)
    print(
        f"  {len(results) - len(fails)}/{len(results)} passed · "
        f"{len(fails)} failed · {len(warns)} warning(s)"
    )
    return fails, warns


def main():
    ap = argparse.ArgumentParser(description="Receipt Tracker disposable QA harness")
    ap.add_argument("--ocr", choices=["real", "mock"], default="real")
    ap.add_argument(
        "--backend",
        choices=["apple_vision", "paddle_ocr", "ollama_vision"],
        default=None,
        help="text-extraction backend for real OCR (default: platform — "
        "apple_vision on macOS, paddle_ocr elsewhere)",
    )
    ap.add_argument(
        "--structure",
        choices=["ollama", "anthropic"],
        default="ollama",
        help="structuring backend: ollama (local) or anthropic (hosted Claude "
        "API — needs ANTHROPIC_API_KEY). Default ollama.",
    )
    ap.add_argument(
        "--anthropic-model",
        type=str,
        default=None,
        help="Claude model for --structure anthropic (default: ANTHROPIC_MODEL "
        "/ claude-opus-4-8). e.g. claude-haiku-4-5 for the cheap A/B.",
    )
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument(
        "--until-clean", action="store_true", help="loop until a fully green run (cap 5)"
    )
    ap.add_argument("--keep", action="store_true", help="keep the temp dir for inspection")
    ap.add_argument("--report", type=str, default="", help="write JSON results to this path")
    ap.add_argument(
        "--corpus",
        type=int,
        default=3,
        help="how many random CORD receipts to score each run (0 = off)",
    )
    ap.add_argument("--no-corpus", action="store_true", help="skip the CORD corpus step")
    ap.add_argument(
        "--text-model",
        type=str,
        default=None,
        help="structuring model for apple_vision (default: your configured "
        "effective text model, so the corpus measures what your app runs)",
    )
    ap.add_argument(
        "--corpus-seed",
        type=int,
        default=None,
        help="fix the corpus sample for a reproducible A/B (default: random each run)",
    )
    args = ap.parse_args()

    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("src").setLevel(logging.WARNING)

    # Default the structuring model to whatever the app is actually configured
    # to use (the persisted picker choice), so the corpus measures reality —
    # not a hardcoded 3b that may be far weaker than what you run.
    text_model = args.text_model
    real_db = None
    try:
        from src.services.ocr_settings import effective_text_model
        from src.config import settings as _real

        # Capture the live DB path BEFORE build_client repoints it to the
        # sandbox — the PT corpus reads your real receipts (read-only).
        real_db = str(Path(_real.db_path).resolve())
        if text_model is None:
            text_model = effective_text_model()
    except Exception:  # noqa: BLE001
        text_model = text_model or "llama3.2:3b"

    # Default the text-extraction backend to the platform's working one.
    backend = args.backend
    if backend is None:
        try:
            from src.config import _default_ocr_backend

            backend = _default_ocr_backend()
        except Exception:  # noqa: BLE001
            backend = "apple_vision"

    # When structuring on the hosted API, surface which Claude model is scored.
    structure_label = args.structure
    if args.structure == "anthropic":
        from src.config import settings as _s

        structure_label = f"anthropic:{args.anthropic_model or _s.anthropic_model}"

    tmp = Path(tempfile.mkdtemp(prefix="rt_qa_"))
    print(
        f"QA harness · ocr={args.ocr} · backend={backend} · "
        f"structure={structure_label} · text_model={text_model} · sandbox={tmp}"
    )
    all_results = []
    fails = warns = []
    try:
        client = build_client(
            tmp, args.ocr, text_model, backend, args.structure, args.anthropic_model
        )
        ctx = {
            "tmp": tmp,
            "ocr": args.ocr,
            "backend": backend,
            "structure": args.structure,
            "corpus_n": 0 if args.no_corpus else args.corpus,
            "seed": args.corpus_seed
            if args.corpus_seed is not None
            else random.randint(0, 1_000_000),  # fixed → reproducible A/B; else fresh
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "real_db": real_db,
        }
        max_runs = 5 if args.until_clean else max(1, args.loops)
        for i in range(1, max_runs + 1):
            results = run_battery(client, ctx)
            fails, warns = print_report(results, i, max_runs)
            all_results = results
            if args.until_clean and not fails:
                print("  ✓ clean run reached.")
                break
    finally:
        if args.keep:
            print(f"\n  (kept sandbox: {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.report:
        Path(args.report).write_text(
            json.dumps([r.__dict__ for r in all_results], indent=2), encoding="utf-8"
        )
        print(f"  report → {args.report}")

    _print_scoreboard_trend()

    if fails:
        print(f"\n✗ {len(fails)} check(s) FAILED")
        return 1
    if warns:
        print(f"\n! all checks passed with {len(warns)} warning(s) — review above")
    else:
        print("\n✓ all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
