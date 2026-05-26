#!/usr/bin/env python3
"""
Repair script: merge uber_gm-* standalone Gmail imports into their matching
CSV (syn-*) entries where a date+amount match exists.

Run from the receipt-tracker directory with the venv activated:
    python scripts/repair_merge_gmail.py [--dry-run]

What it does:
  1. Finds uber_gm-* rows that have a matching CSV row (by date + amount,
     within 0.01 tolerance, currency flexible — catches CAD/USD confusion).
  2. If the CSV row doesn't already have a PDF, copies the PDF filepath from
     the gmail row to the CSV row.
  3. Deletes the redundant uber_gm-* row.
  4. Rows with no CSV match (e.g. May trips not in the CSV) are left untouched.
"""

import json
import sqlite3
import sys
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv
DB_PATH = Path(__file__).parent.parent / "data" / "receipt_tracker.db"


def load_rows(conn, pattern: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, filepath, metadata FROM receipts WHERE filename LIKE ?",
        (pattern,),
    )
    rows = []
    for row in cur.fetchall():
        meta = {}
        try:
            meta = json.loads(row[2] or "{}")  # metadata is col index 3; fix below
        except Exception:
            pass
        rows.append({"id": row[0], "filename": row[1], "filepath": row[2], "metadata": row[3]})
    return rows


def load_all_uber(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, filepath, metadata FROM receipts WHERE filename LIKE 'uber_%'"
    )
    result = []
    for row in cur.fetchall():
        meta = {}
        try:
            raw = row[3]
            # metadata may be double-encoded
            if isinstance(raw, str):
                parsed = json.loads(raw)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                meta = parsed
        except Exception:
            pass
        result.append({
            "id": row[0],
            "filename": row[1],
            "filepath": row[2],
            "meta": meta,
        })
    return result


def find_csv_match(all_rows, date, amount, tolerance=0.01):
    """Find a non-gmail CSV row matching date+amount (currency-agnostic)."""
    for row in all_rows:
        if row["filename"].startswith("uber_gm-"):
            continue  # skip other gmail rows
        meta = row["meta"]
        if meta.get("date") != date:
            continue
        amt = meta.get("total_amount")
        if amt is None:
            continue
        try:
            if abs(round(float(amt), 2) - round(float(amount), 2)) <= tolerance + 1e-9:
                return row
        except (TypeError, ValueError):
            continue
    return None


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    all_rows = load_all_uber(conn)
    gmail_rows = [r for r in all_rows if r["filename"].startswith("uber_gm-")]

    print(f"Found {len(gmail_rows)} uber_gm-* rows, {len(all_rows) - len(gmail_rows)} CSV rows")
    print(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n")

    merged = 0
    skipped_no_match = 0
    skipped_csv_has_pdf = 0

    cur = conn.cursor()
    for gm in gmail_rows:
        meta = gm["meta"]
        date = meta.get("date")
        amount = meta.get("total_amount")
        currency = meta.get("currency", "?")

        if not date or amount is None:
            print(f"  SKIP {gm['filename']}: missing date or amount")
            continue

        csv_row = find_csv_match(all_rows, date, amount)
        if csv_row is None:
            print(f"  NO MATCH  {gm['filename']}  {date}  {amount} {currency}")
            skipped_no_match += 1
            continue

        # Check if the CSV row already has a real PDF
        csv_filepath = csv_row.get("filepath") or ""
        gm_filepath = gm.get("filepath") or ""

        if csv_filepath.endswith(".pdf"):
            print(f"  ALREADY OK  CSV id={csv_row['id']} already has PDF — deleting gm row")
            if not DRY_RUN:
                cur.execute("DELETE FROM receipts WHERE id = ?", (gm["id"],))
                conn.commit()
            merged += 1
            continue

        # Update CSV row with gmail PDF filepath+filename
        new_filepath = gm_filepath
        new_filename = gm["filename"]
        print(
            f"  MERGE  gm id={gm['id']} ({date}, {amount} {currency})"
            f"  →  CSV id={csv_row['id']} ({csv_row['filename']})"
        )
        if not DRY_RUN:
            cur.execute(
                "UPDATE receipts SET filepath = ?, filename = ? WHERE id = ?",
                (new_filepath, new_filename, csv_row["id"]),
            )
            cur.execute("DELETE FROM receipts WHERE id = ?", (gm["id"],))
            conn.commit()
        merged += 1

    conn.close()

    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}Done:")
    print(f"  Merged / cleaned: {merged}")
    print(f"  No CSV match (kept): {skipped_no_match}")
    print(f"  CSV already had PDF: {skipped_csv_has_pdf}")


if __name__ == "__main__":
    main()
