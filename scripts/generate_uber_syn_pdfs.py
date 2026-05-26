"""Generate PDF receipts for all uber_syn DB rows using WeasyPrint."""
from __future__ import annotations
import json, sqlite3, sys, ctypes.util, platform, os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

def _install_homebrew_shim():
    if platform.system() != "Darwin":
        return
    _orig = ctypes.util.find_library
    def _patched(name):
        hit = _orig(name)
        if hit: return hit
        for d in ("/opt/homebrew/lib", "/usr/local/lib"):
            for cand in (f"{d}/{name}", f"{d}/{name}.dylib", f"{d}/lib{name}.dylib",
                         f"{d}/lib{name}.0.dylib", f"{d}/lib{name}.1.dylib"):
                if os.path.exists(cand): return cand
        return None
    ctypes.util.find_library = _patched

_install_homebrew_shim()

try:
    import weasyprint
    print(f"WeasyPrint {weasyprint.__version__} loaded")
except Exception as e:
    print(f"WeasyPrint not available: {e}"); sys.exit(1)

DB_PATH      = PROJECT_ROOT / "data" / "receipt_tracker.db"
RECEIPTS_DIR = PROJECT_ROOT / "data" / "receipts"
MAPPING_FILE = PROJECT_ROOT / "data" / "db_uuid_mapping.json"
RECEIPTS_DIR.mkdir(exist_ok=True)

CURRENCY_SYMBOLS = {"BRL": "R$", "CAD": "CA$", "USD": "$", "EUR": "€", "GBP": "£"}

CSS = """
  body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 13px; color: #1a1a1a; background: #fff; padding: 40px; }
  .header { border-bottom: 2px solid #000; padding-bottom: 20px; margin-bottom: 28px; overflow: hidden; }
  .logo { font-size: 28px; font-weight: 900; letter-spacing: -1px; float: left; margin-right: 16px; }
  .header-title { font-size: 18px; font-weight: 700; color: #000; }
  .header-subtitle { font-size: 12px; color: #666; margin-top: 2px; }
  .amount-box { float: right; text-align: right; }
  .amount-big { font-size: 30px; font-weight: 800; color: #000; }
  .currency-label { font-size: 14px; color: #666; margin-top: 2px; }
  .section { margin-bottom: 24px; clear: both; }
  .section-title { font-size: 10px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase; color: #888; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 6px; }
  .field { margin-bottom: 7px; overflow: hidden; }
  .field-label { float: left; width: 130px; color: #666; font-size: 12px; }
  .field-value { margin-left: 130px; font-weight: 500; }
  .route { background: #f7f7f7; border: 1px solid #e8e8e8; padding: 14px 16px; margin-bottom: 24px; }
  .route-point { margin-bottom: 12px; overflow: hidden; }
  .route-point:last-child { margin-bottom: 0; }
  .route-dot { float: left; width: 10px; height: 10px; border-radius: 5px; margin-right: 12px; margin-top: 3px; }
  .dot-pickup { background: #000; }
  .dot-dropoff { background: #fff; border: 2px solid #000; }
  .route-content { margin-left: 22px; }
  .route-label { font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
  .route-address { font-weight: 500; font-size: 13px; }
  .footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid #eee; font-size: 11px; color: #aaa; overflow: hidden; }
  .footer-left { float: left; }
  .footer-right { float: right; }
  .badge { display: inline; background: #f0f0f0; padding: 2px 8px; font-size: 11px; color: #555; font-weight: 600; }
"""

def make_html(meta: dict, uuid: str) -> str:
    from datetime import datetime
    date_raw = meta.get("date") or ""
    try:
        dt = datetime.strptime(date_raw, "%Y-%m-%d")
        date_formatted = dt.strftime("%B %d, %Y")
    except Exception:
        date_formatted = date_raw

    currency = (meta.get("currency") or "").upper()
    currency_symbol = CURRENCY_SYMBOLS.get(currency, "")
    amount = meta.get("total_amount") or 0

    vendor_address = meta.get("vendor_address") or ""
    if " → " in vendor_address:
        pickup, dropoff = vendor_address.split(" → ", 1)
    elif "→" in vendor_address:
        pickup, dropoff = vendor_address.split("→", 1)
    else:
        pickup, dropoff = vendor_address, ""
    pickup = pickup.strip() or "–"
    dropoff = dropoff.strip() or "–"

    service = meta.get("uber_service") or "Uber"
    distance_raw = meta.get("uber_distance") or ""
    try:
        distance = f"{float(distance_raw):.1f} mi"
    except Exception:
        distance = distance_raw or "–"

    payment = meta.get("payment_method") or "–"
    city = meta.get("city") or ""

    # Build HTML without using .format() to avoid conflicts with CSS braces
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<style>", CSS, "</style></head><body>",
        # Header
        "<div class='header'>",
        "<div class='logo'>Uber</div>",
        "<div class='amount-box'>",
        f"<div class='amount-big'>{currency_symbol}{amount:,.2f}</div>",
        f"<div class='currency-label'>{currency}</div>",
        "</div>",
        "<div class='header-info'>",
        "<div class='header-title'>Trip Receipt</div>",
        f"<div class='header-subtitle'>{date_formatted}</div>",
        "</div>",
        "</div>",
        # Trip details
        "<div class='section'>",
        "<div class='section-title'>Trip Details</div>",
        "<div class='field'>",
        "<span class='field-label'>Date</span>",
        f"<span class='field-value'>{date_formatted}</span>",
        "</div>",
        "<div class='field'>",
        "<span class='field-label'>Service</span>",
        f"<span class='field-value'>{service} <span class='badge'>{city}</span></span>",
        "</div>",
        "<div class='field'>",
        "<span class='field-label'>Distance</span>",
        f"<span class='field-value'>{distance}</span>",
        "</div>",
        "<div class='field'>",
        "<span class='field-label'>Payment</span>",
        f"<span class='field-value'>&#8226;&#8226;&#8226; {payment}</span>",
        "</div>",
        "</div>",
        # Route
        "<div class='section'>",
        "<div class='section-title'>Route</div>",
        "<div class='route'>",
        "<div class='route-point'>",
        "<div class='route-dot dot-pickup'></div>",
        "<div class='route-content'>",
        "<div class='route-label'>Pickup</div>",
        f"<div class='route-address'>{pickup}</div>",
        "</div></div>",
        "<div class='route-point'>",
        "<div class='route-dot dot-dropoff'></div>",
        "<div class='route-content'>",
        "<div class='route-label'>Dropoff</div>",
        f"<div class='route-address'>{dropoff}</div>",
        "</div></div>",
        "</div></div>",
        # Fare
        "<div class='section'>",
        "<div class='section-title'>Fare Summary</div>",
        "<div class='field'>",
        "<span class='field-label'>Total Charged</span>",
        f"<span class='field-value' style='font-weight:700'>{currency_symbol}{amount:,.2f} {currency}</span>",
        "</div></div>",
        # Footer
        "<div class='footer'>",
        f"<div class='footer-left'>Trip ID: {uuid}</div>",
        f"<div class='footer-right'>Uber trip record &middot; {city}</div>",
        "</div>",
        "</body></html>",
    ]
    return "".join(parts)


def main():
    mapping = json.loads(MAPPING_FILE.read_text())
    by_db_id = {str(m["db_id"]): m for m in mapping}
    print(f"UUID mapping: {len(by_db_id)} entries")

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, filename, filepath, metadata FROM receipts WHERE filename LIKE 'uber_syn%'"
    ).fetchall()
    print(f"uber_syn rows: {len(rows)}")

    generated = 0
    skipped = 0
    errors = []

    for row_id, fname, fpath, meta_str in rows:
        meta = json.loads(meta_str)
        entry = by_db_id.get(str(row_id))
        if not entry:
            errors.append(f"id={row_id}: no UUID mapping")
            continue

        uuid = entry["uuid"]
        pdf_filename = f"uber_syn-{uuid}.pdf"
        pdf_path = RECEIPTS_DIR / pdf_filename

        if pdf_path.exists() and fpath and str(fpath).endswith(pdf_filename):
            skipped += 1
            continue

        try:
            html = make_html(meta, uuid)
            pdf_bytes = weasyprint.HTML(string=html).write_pdf()
            pdf_path.write_bytes(pdf_bytes)
            conn.execute("UPDATE receipts SET filepath=? WHERE id=?", (str(pdf_path), row_id))
            conn.commit()
            generated += 1
            print(f"  [{generated:3d}] {pdf_filename[:40]} — {meta.get('date')} {meta.get('total_amount')} {meta.get('currency')}")
        except Exception as e:
            errors.append(f"id={row_id}: {e}")
            print(f"  ERROR id={row_id}: {e}")

    conn.close()
    print(f"\nDone. generated={generated} skipped={skipped} errors={len(errors)}")
    for e in errors:
        print(f"  {e}")

if __name__ == "__main__":
    main()
