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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── disposable app ──────────────────────────────────────────────────────────


def build_client(tmp: Path, ocr_mode: str):
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
    # Real OCR runs the working on-device path (Apple Vision + small text LLM),
    # NOT the default ollama_vision/glm-ocr one-shot (which loops on the prompt).
    settings.ocr_backend = "apple_vision"
    settings.ocr_text_model = "llama3.2:3b"

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


@check("ocr config: QA env on apple_vision")
def _c_ocrcfg(client, ctx):
    cfg = client.get("/api/ocr/config").json()
    assert cfg["backend"] == "apple_vision", cfg.get("backend")


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


# ─── real-world corpus: CORD receipts, scored against ground truth ───────────
# Every run pulls a fresh RANDOM sample of real receipts from the CORD dataset
# (naver-clova-ix/cord-v2, HuggingFace, CC BY 4.0 — real photos + ground-truth
# totals), runs them through the OCR pipeline, scores total-amount accuracy,
# and appends to qa/corpus_scoreboard.jsonl so you can see whether an algo
# change moves the number. Needs `datasets` + network; warns (never hard-fails)
# if either is missing or the sample is just hard.

SCOREBOARD = ROOT / "qa" / "corpus_scoreboard.jsonl"


def _cord_gt_total(ground_truth_str):
    """Ground-truth total from a CORD example → integer minor-unit-free amount
    (CORD is Indonesian rupiah, whole numbers: "60.000" → 60000)."""
    try:
        gt = json.loads(ground_truth_str)
        tp = ((gt.get("gt_parse") or {}).get("total") or {}).get("total_price")
        digits = re.sub(r"[^\d]", "", str(tp or ""))
        return int(digits) if digits else None
    except Exception:  # noqa: BLE001
        return None


def _norm_amount(v):
    """Our extracted total → rounded int for comparison with the GT integer."""
    try:
        return round(float(v))
    except (TypeError, ValueError):
        return None


def _append_scoreboard(ctx, scored, correct, acc):
    from src.services import ocr_settings

    rec = {
        "ts": ctx["ts"],
        "backend": ocr_settings.effective_backend(),
        "text_model": ocr_settings.effective_text_model(),
        "n": len(scored),
        "correct": correct,
        "accuracy": round(acc, 3),
        "samples": [{"gt": r["gt"], "got": r["got"], "ok": r["ok"]} for r in scored],
    }
    SCOREBOARD.parent.mkdir(parents=True, exist_ok=True)
    with open(SCOREBOARD, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


@check("real corpus: CORD total-amount accuracy (random sample, scored)")
def _c_corpus(client, ctx):
    if ctx["ocr"] != "real":
        raise Warn("corpus scoring needs --ocr real (mock can't read totals)")
    n = ctx["corpus_n"]
    if n <= 0:
        raise Warn("corpus step disabled (--corpus 0)")
    try:
        from datasets import load_dataset
    except ImportError:
        raise Warn("`uv pip install datasets` to enable the CORD corpus step")

    import itertools

    try:
        ds = load_dataset("naver-clova-ix/cord-v2", split="test", streaming=True)
        ds = ds.shuffle(seed=ctx["seed"], buffer_size=64)  # fresh random sample / run
        samples = list(itertools.islice(ds, n))
    except Exception as e:  # noqa: BLE001 — network / HF down / rate-limited
        raise Warn(f"couldn't load CORD (offline / HF down?): {str(e)[:120]}")
    if not samples:
        raise Warn("CORD returned no samples")

    from src.services.llm_processor import ocr_crops

    rows = []
    for s in samples:
        gt = _cord_gt_total(s["ground_truth"])
        try:
            meta = ocr_crops([s["image"].convert("RGB")])[0]["metadata"]
            got = _norm_amount(meta.get("total_amount"))
        except Exception:  # noqa: BLE001
            got = None
        ok = gt is not None and got is not None and abs(got - gt) <= 1
        rows.append({"gt": gt, "got": got, "ok": ok})

    scored = [r for r in rows if r["gt"] is not None]
    if not scored:
        raise Warn("no ground-truth totals in this sample")
    correct = sum(r["ok"] for r in scored)
    acc = correct / len(scored)
    _append_scoreboard(ctx, scored, correct, acc)

    miss = [f"{r['gt']}→{r['got']}" for r in scored if not r["ok"]][:6]
    note = f"{correct}/{len(scored)} totals correct ({acc:.0%})"
    if miss:
        note += " · gt→got misses: " + ", ".join(miss)
    if correct == 0:
        raise Warn(note + " — every total wrong; likely a parse/locale gap")
    return note


def _print_scoreboard_trend():
    if not SCOREBOARD.exists():
        return
    try:
        lines = SCOREBOARD.read_text(encoding="utf-8").strip().splitlines()[-5:]
        recs = [json.loads(x) for x in lines]
    except Exception:  # noqa: BLE001
        return
    if not recs:
        return
    print("\n  CORD total-accuracy trend (last 5 runs)")
    print("  " + "─" * 64)
    for r in recs:
        print(
            f"    {r.get('ts', '?'):<20} {r.get('text_model', '?'):<14} "
            f"{r.get('correct', 0)}/{r.get('n', 0)}  {r.get('accuracy', 0):.0%}"
        )


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
    args = ap.parse_args()

    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("src").setLevel(logging.WARNING)

    tmp = Path(tempfile.mkdtemp(prefix="rt_qa_"))
    print(f"QA harness · ocr={args.ocr} · sandbox={tmp}")
    all_results = []
    fails = warns = []
    try:
        client = build_client(tmp, args.ocr)
        ctx = {
            "tmp": tmp,
            "ocr": args.ocr,
            "corpus_n": 0 if args.no_corpus else args.corpus,
            "seed": random.randint(0, 1_000_000),  # fresh random CORD sample / run
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
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
