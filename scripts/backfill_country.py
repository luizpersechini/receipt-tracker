#!/usr/bin/env python3
"""
One-shot migration: normalise `country` to ISO-2 and re-refine `currency` on
every row in the receipts table.

Background: the OCR prompt asks the vision model for an ISO-2 country code
but it often returns the full name ("Canada", "Brasil", "U.S.A."). Older
rows that predate src/services/llm_processor._normalize_country end up with
non-ISO-2 country strings, which makes the country-aware currency fallback
miss.

Idempotent: rows already in ISO-2 form pass through unchanged.

Run from the project root:

    python3 scripts/backfill_country.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import database as db                                       # noqa: E402
from src.services.llm_processor import _normalize_country, _refine_currency  # noqa: E402


def main() -> int:
    receipts = db.get_all_receipts()
    if not receipts:
        print("No receipts in database. Nothing to do.")
        return 0

    touched = 0
    for r in receipts:
        meta = json.loads(r.get("metadata") or "{}")
        old_country = meta.get("country")
        old_currency = meta.get("currency")

        new_country = _normalize_country(old_country)
        new_currency = _refine_currency(old_currency, new_country)

        if new_country == old_country and new_currency == old_currency:
            continue

        patch = {}
        if new_country != old_country:
            patch["country"] = new_country
        if new_currency != old_currency:
            patch["currency"] = new_currency

        db.update_receipt(r["id"], {}, patch)
        touched += 1
        print(
            f"#{r['id']:>3} {r['filename'][:60]:60}  "
            f"country: {old_country!r} → {new_country!r}  "
            f"currency: {old_currency!r} → {new_currency!r}"
        )

    print(f"\nDone. {touched}/{len(receipts)} receipts updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
