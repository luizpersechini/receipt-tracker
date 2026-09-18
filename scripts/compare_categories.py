#!/usr/bin/env python
"""Compare expense-category sources over the live DB — READ-ONLY.

For every receipt and every statement line, print:
  stored    — what the app has today (receipts: structuring-model output;
              statement lines: the keyword guess, since that's the only
              source statements have)
  keyword   — the keyword heuristic recomputed now
  typesafe  — TypeSafe/Jev's choice + confidence (skipped if no API key)
  truth     — hard ground truth where it exists: a user override in
              data/statement_categories.json. Receipts carry NO reviewed
              marker in this DB, so for receipts the stored category is
              reported as "current/accepted" and used as the reference —
              treat receipt agreement as "agrees with what the app has",
              not "agrees with a human".

Ends with agreement rates, the 20 most interesting disagreements (confident
TypeSafe answers that differ from the heuristic, truth-backed ones first),
and per-call latency + token usage.

Usage:  .venv/bin/python scripts/compare_categories.py [--limit N] [--no-typesafe]
Never writes to the DB (opened with mode=ro) or to any JSON store.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services import typesafe_categorizer as ts  # noqa: E402
from src.services.llm_processor import _infer_category  # noqa: E402
from src.services.statement_pdf_parser import _guess_category  # noqa: E402


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_overrides(data_dir: Path) -> dict:
    p = data_dir / "statement_categories.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def collect_rows(conn: sqlite3.Connection, overrides: dict, limit: int | None) -> list[dict]:
    rows: list[dict] = []

    for r in conn.execute("SELECT id, metadata FROM receipts ORDER BY id"):
        try:
            m = json.loads(r["metadata"] or "{}")
        except json.JSONDecodeError:
            m = {}
        vendor = m.get("vendor_name") or ""
        items = m.get("items") or []
        rows.append(
            {
                "kind": "receipt",
                "key": f"r{r['id']}",
                "label": vendor[:34] or f"receipt #{r['id']}",
                "stored": (m.get("category") or "other").lower(),
                "keyword": _infer_category(vendor, items),
                # No reviewed marker exists → stored is the reference, flagged.
                "truth": (m.get("category") or "other").lower(),
                "truth_kind": "current",
                "ts_item": {
                    "id": f"r{r['id']}",
                    "merchant_or_vendor": vendor,
                    "amount": m.get("total_amount"),
                    "currency": m.get("currency") or "",
                    "date": m.get("date") or "",
                    "items": items,
                },
            }
        )

    for s in conn.execute("SELECT id, transactions FROM statements ORDER BY id"):
        try:
            txns = json.loads(s["transactions"] or "[]")
        except json.JSONDecodeError:
            txns = []
        ov = overrides.get(str(s["id"])) or {}
        for i, t in enumerate(txns):
            desc = t.get("description") or ""
            truth = ov.get(str(i))
            rows.append(
                {
                    "kind": "stmt",
                    "key": f"s{s['id']}:{i}",
                    "label": desc[:34],
                    "stored": (t.get("category") or "other").lower(),
                    "keyword": _guess_category(desc),
                    "truth": truth.lower() if truth else None,
                    "truth_kind": "override" if truth else None,
                    "ts_item": {
                        "id": f"s{s['id']}:{i}",
                        "merchant_or_vendor": desc,
                        "amount": t.get("amount"),
                        "currency": (t.get("raw_data") or {}).get("original_currency", "BRL"),
                        "date": t.get("date") or "",
                    },
                }
            )

    return rows[:limit] if limit else rows


def run_typesafe(rows: list[dict]) -> list[dict]:
    """One batched call per 50 rows; returns per-call metrics."""
    calls = []
    for start in range(0, len(rows), ts.BATCH_SIZE):
        chunk = rows[start : start + ts.BATCH_SIZE]
        res = ts.categorize_batch([r["ts_item"] for r in chunk])
        for r, out in zip(chunk, res["results"]):
            r["typesafe"] = out["category"]
            r["ts_conf"] = out["confidence"]
        calls.append(
            {
                "n": len(chunk),
                "latency_s": res["latency_s"],
                "input_tokens": (res["usage"] or {}).get("input_tokens"),
                "output_tokens": (res["usage"] or {}).get("output_tokens"),
            }
        )
    return calls


def _rate(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return "n/a (no rows)"
    ok = sum(1 for a, b in pairs if a == b)
    return f"{ok}/{len(pairs)} = {100 * ok / len(pairs):.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-typesafe", action="store_true", help="heuristic/truth only")
    args = ap.parse_args()

    conn = _ro_connect(settings.db_path)
    rows = collect_rows(conn, _load_overrides(settings.data_dir), args.limit)
    conn.close()

    have_key = bool((settings.typesafe_api_key or os.environ.get("TYPESAFE_API_KEY") or "").strip())
    calls: list[dict] = []
    if args.no_typesafe or not have_key:
        print(
            "TypeSafe column SKIPPED —",
            "flag --no-typesafe" if args.no_typesafe else "TYPESAFE_API_KEY unset",
        )
    else:
        calls = run_typesafe(rows)

    # ── per-row table ────────────────────────────────────────────────────
    print(
        f"\n{'key':<9} {'kind':<7} {'label':<34} {'stored':<16} {'keyword':<16} {'typesafe':<22} truth"
    )
    for r in rows:
        tsv = f"{r['typesafe']} ({r['ts_conf']:.2f})" if "typesafe" in r else "—"
        truth = f"{r['truth']} [{r['truth_kind']}]" if r["truth"] else "—"
        print(
            f"{r['key']:<9} {r['kind']:<7} {r['label']:<34} {r['stored']:<16} {r['keyword']:<16} {tsv:<22} {truth}"
        )

    # ── agreement ────────────────────────────────────────────────────────
    hard = [r for r in rows if r["truth_kind"] == "override"]
    cur = [r for r in rows if r["truth_kind"] == "current"]
    print("\n== Agreement vs HARD truth (statement overrides) ==")
    print("  keyword  :", _rate([(r["keyword"], r["truth"]) for r in hard]))
    if calls:
        print("  typesafe :", _rate([(r["typesafe"], r["truth"]) for r in hard]))
    print(
        "== Agreement vs CURRENT category (receipts; structuring-model output, not human-verified) =="
    )
    print("  keyword  :", _rate([(r["keyword"], r["truth"]) for r in cur]))
    if calls:
        print("  typesafe :", _rate([(r["typesafe"], r["truth"]) for r in cur]))
        print(
            "  typesafe rows above confidence floor "
            f"({settings.typesafe_min_confidence}): "
            f"{sum(1 for r in rows if r['ts_conf'] >= settings.typesafe_min_confidence)}/{len(rows)}"
        )

    # ── interesting disagreements ────────────────────────────────────────
    if calls:
        dis = [r for r in rows if r["typesafe"] != r["keyword"]]
        # truth-backed first, then most confident TypeSafe answers
        dis.sort(key=lambda r: (r["truth_kind"] != "override", -r["ts_conf"]))
        print(f"\n== Top 20 interesting disagreements (typesafe ≠ keyword; {len(dis)} total) ==")
        print(f"{'key':<9} {'label':<34} {'keyword':<16} {'typesafe':<22} truth")
        for r in dis[:20]:
            truth = f"{r['truth']} [{r['truth_kind']}]" if r["truth"] else "—"
            print(
                f"{r['key']:<9} {r['label']:<34} {r['keyword']:<16} {r['typesafe'] + f' ({r['ts_conf']:.2f})':<22} {truth}"
            )

        # where typesafe and keyword disagree AND hard truth exists: who wins?
        judged = [r for r in dis if r["truth_kind"] == "override"]
        if judged:
            tw = sum(1 for r in judged if r["typesafe"] == r["truth"])
            kw = sum(1 for r in judged if r["keyword"] == r["truth"])
            print(
                f"\n  on truth-backed disagreements: typesafe right {tw}, keyword right {kw}, of {len(judged)}"
            )

        # ── metrics ──────────────────────────────────────────────────────
        print("\n== Per-call metrics ==")
        for i, c in enumerate(calls, 1):
            print(
                f"  call {i}: {c['n']} items  {c['latency_s'] * 1000:.0f} ms  "
                f"in={c['input_tokens']} out={c['output_tokens']} tokens"
            )
        tot_in = sum(c["input_tokens"] or 0 for c in calls)
        tot_out = sum(c["output_tokens"] or 0 for c in calls)
        tot_lat = sum(c["latency_s"] for c in calls)
        print(
            f"  total: {len(rows)} items in {len(calls)} calls, {tot_lat:.1f}s, "
            f"{tot_in} in / {tot_out} out tokens "
            f"(~{tot_in / max(len(rows), 1):.0f} in-tokens per item)"
        )

    dist = Counter(r["keyword"] for r in rows)
    print("\n== keyword category distribution ==", dict(dist))
    return 0


if __name__ == "__main__":
    sys.exit(main())
