"""Parser for Itaú corporate-card PDF statements (PT-BR).

The PDF uses a two-column layout per page, but PyMuPDF's `get_text()`
flattens the content in a way that preserves transaction triplets
intact — every transaction renders as 3 sequential lines:

    DD/MM
    MERCHANT NAME
    AMOUNT,XX

International transactions add 5 more lines after the BRL amount:

    CITY
    NATIVE_AMOUNT
    CURRENCY (USD/CAD/EUR/...)
    USD_AMOUNT
    Dólar de Conversão R$ X,XX

We walk the flattened lines with a small state machine, matching the
strict international pattern first (8 lines, ends with the FX marker)
then falling back to the 3-line domestic pattern. The "Pagamentos
efetuados" section is skipped — those rows aren't merchant purchases.

Transaction shape matches what `parse_statement_csv` produces so the
matching engine and DB layer don't change:

    {
      "date":        "2026-03-21",   # ISO; year inferred from Postagem
      "description": "DL *UberRides",
      "amount":      24.20,          # always BRL (statement currency)
      "category":    "transportation",
      "raw_data": {
        "section":           "domestic" | "international",
        "original_currency": "BRL" | "CAD" | "USD" | ...,
        "original_amount":   21.88,  # native amount (int'l only)
        "fx_to_brl":         5.62,   # BRL per USD (int'l only)
        "city":              "TORONTO" | None,
      }
    }
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Patterns ───────────────────────────────────────────────────────────────

# Section headers (sticky once matched, until the next header).
_HEADER_PAYMENTS = "Pagamentos efetuados"
_HEADER_DOMESTIC = "Lançamentos: compras e saques"
_HEADER_INTERNATIONAL = "Lançamentos internacionais"

# DD/MM on its own line — transaction date marker.
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})$")

# BRL amount: comma decimal, optional thousands dots ("4.662,00"), optional
# leading "-" ("- 123,45") — estornos/refunds print as negatives inside the
# purchases sections and are captured as credit rows. The previous-bill
# payment is ALSO negative, but lives in the "Pagamentos efetuados" section
# which the walker skips wholesale.
_BRL_AMOUNT_RE = re.compile(r"^-?\s?\d{1,3}(?:\.\d{3})*,\d{2}$")

# Native-currency amount on its own line ("21,88" / "- 21,88").
_NATIVE_AMOUNT_RE = re.compile(r"^-?\s?\d{1,3}(?:\.\d{3})*,\d{2}$")

# ISO 4217 codes that show up in this statement series.
_CCY_RE = re.compile(r"^(USD|CAD|EUR|GBP|JPY|AUD|CHF|MXN|ARS|ARP|CLP)$")

# "Dólar de Conversão R$ 5,62" — the FX rate line that closes an int'l txn.
_FX_RE = re.compile(r"^Dólar de Conversão R\$\s*([\d.,]+)$")

# "Postagem: 05/04/2026" on the cover page — used to infer the year.
_POSTAGEM_RE = re.compile(r"Postagem:\s*(\d{2})/(\d{2})/(\d{4})")


# ─── Column-aware text extraction ──────────────────────────────────────────


def _column_ordered_lines(page) -> Optional[list[str]]:
    """Return one page's text as lines, but column-first (left column top
    to bottom, then right column top to bottom).

    Itaú statements lay out two visual columns of transactions per page.
    PyMuPDF's default flat `get_text()` reads them in row-by-row order,
    which interleaves a transaction's int'l footer (city/native/ccy/USD/FX)
    with the start of the NEXT row's transaction in the other column. By
    sorting line bboxes into two column buckets (split at page midpoint)
    and walking each in y-order, every transaction's lines stay together.

    Returns None on any extraction error so the caller can fall back to
    the flat-text reading order.
    """
    try:
        d = page.get_text("dict")
    except Exception:
        return None
    page_width = page.rect.width
    # The visual column gap in Itaú statements sits well to the right of
    # geometric centre — the LEFT column's right-aligned amounts can reach
    # x≈335 while the RIGHT column's date starts at x≈365. A 50/50 split
    # would mis-bucket the LEFT amount lines. Empirically the boundary
    # sits around 58% of page width; tune here if other Itaú templates
    # render differently.
    mid_x = page_width * 0.58

    lines: list[tuple[int, float, str]] = []  # (column, y0, text)
    for block in d.get("blocks", []):
        if block.get("type", 0) != 0:  # type 0 = text; skip images
            continue
        for ln in block.get("lines", []):
            spans = ln.get("spans", [])
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            bbox = ln.get("bbox") or [0, 0, 0, 0]
            x0, y0 = bbox[0], bbox[1]
            # Lines that span (nearly) the whole page width are headers /
            # totals / fine print — keep them on the left so section
            # detection still fires at the right place.
            line_width = bbox[2] - bbox[0]
            full_width = line_width > page_width * 0.55
            col = 0 if (full_width or x0 < mid_x) else 1
            lines.append((col, y0, text))

    # Stable sort: column first, then y. The result is left col top→bottom,
    # right col top→bottom — preserving each transaction's line cluster.
    lines.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in lines]


# ─── Public entry point ────────────────────────────────────────────────────


def _unlock(doc) -> bool:
    """True when `doc` is readable: not encrypted, or the configured
    STATEMENT_PDF_PASSWORD opened it. The password is tried as typed and
    digits-only — banks quote it formatted ("74.127.010") but usually
    expect bare digits."""
    if not doc.needs_pass:
        return True
    from ..config import settings

    pw = (settings.statement_pdf_password or "").strip()
    for cand in dict.fromkeys([pw, re.sub(r"\D", "", pw)]):
        if cand and doc.authenticate(cand):
            return True
    return False


def parse_statement_pdf(filepath: str) -> list[dict]:
    """Parse an Itaú PDF statement into a list of transaction dicts."""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover — checked at runtime
        raise RuntimeError("PyMuPDF (fitz) is required to parse PDF statements") from e

    doc = fitz.open(filepath)
    if not _unlock(doc):
        doc.close()
        raise ValueError(
            "This statement PDF is password-protected and couldn't be "
            "unlocked — set STATEMENT_PDF_PASSWORD in .env to the bank's "
            "PDF password (e.g. the CNPJ prefix Itaú uses)."
        )
    try:
        year: Optional[int] = None
        post_md: Optional[tuple] = None  # (month, day) of the Postagem date
        all_lines: list[str] = []
        for page in doc:
            text = page.get_text()
            if year is None:
                m = _POSTAGEM_RE.search(text)
                if m:
                    year = int(m.group(3))
                    post_md = (int(m.group(2)), int(m.group(1)))
            # Itaú renders two visual columns per page. `get_text()` reads
            # them in row-by-row "newspaper" order which interleaves a
            # transaction's footer (city / native / ccy / USD / FX) with
            # the NEXT transaction in the other column. Pre-sort by
            # column-then-y so each transaction's 8 lines stay contiguous.
            page_lines = _column_ordered_lines(page)
            if page_lines is not None:
                all_lines.extend(page_lines)
            else:
                # Fallback if the dict extraction fails for any reason.
                all_lines.extend(text.splitlines())
    finally:
        doc.close()

    if year is None:
        # Fall back to current year if Postagem wasn't found.
        year = datetime.now().year
        logger.warning("PDF statement: Postagem header not found, using current year %d", year)

    txns = _walk_lines(all_lines, year, post_md)
    logger.info("PDF statement: parsed %d transaction(s)", len(txns))
    return txns


def zero_parse_warning(filepath: str) -> str:
    """Explain a zero-transaction parse to the user.

    Scanned statements (image-only pages, no text layer) are the common
    cause and used to fail silently — the parser reads embedded text, so a
    scan yields an empty list without any error. A PDF that does have text
    but still parsed to nothing is more likely a non-Itaú layout.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(filepath)
    try:
        # Defensive — parse_statement_pdf raises on locked docs before this
        # helper is ever reached, but keep it safe standalone.
        if not _unlock(doc):
            return (
                "this PDF is password-protected — set STATEMENT_PDF_PASSWORD "
                "in .env so statements can be unlocked automatically."
            )
        chars = sum(len(page.get_text().strip()) for page in doc)
    finally:
        doc.close()
    # A native statement carries thousands of chars; tolerate stray
    # watermark/footer text before calling it a scan.
    if chars < 50:
        return (
            "this PDF has no text layer — it looks like a scan. The parser "
            "reads native digital statements; download the original PDF from "
            "the bank and re-upload."
        )
    return (
        "no transactions were recognized — the file has text but doesn't "
        "match the expected Itaú corporate-card layout."
    )


# ─── State-machine walk over flattened text ────────────────────────────────


def _walk_lines(lines: list[str], year: int, post_md: Optional[tuple] = None) -> list[dict]:
    """Walk the line list, emitting one dict per detected transaction."""
    txns: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()

        # The payments section lists the previous-bill payment (money in,
        # not a purchase OR a refund). Skip EVERY such block — multi-
        # cardholder statements repeat it, and now that negative amounts
        # parse as credit rows an unskipped block would emit phantom rows.
        if _HEADER_PAYMENTS in line:
            i += 1
            while i < n and not (_HEADER_DOMESTIC in lines[i] or _HEADER_INTERNATIONAL in lines[i]):
                i += 1
            continue

        # Try the strict international pattern first (8 lines, ends on FX).
        txn, used = _try_international(lines, i, year, post_md)
        if txn:
            txns.append(txn)
            i += used
            continue

        # Then the simpler domestic 3-line pattern.
        txn, used = _try_domestic(lines, i, year, post_md)
        if txn:
            txns.append(txn)
            i += used
            continue

        i += 1
    return txns


def _try_domestic(lines: list[str], i: int, year: int, post_md: Optional[tuple] = None):
    """Match a 3-line BRL transaction at position i. Returns (txn, n_used)."""
    if i + 2 >= len(lines):
        return None, 0
    date_m = _DATE_RE.match(lines[i].strip())
    if not date_m:
        return None, 0
    amt_line = lines[i + 2].strip()
    if not _BRL_AMOUNT_RE.match(amt_line):
        return None, 0
    amount = _parse_brl(amt_line)
    if amount is None or amount == 0:
        return None, 0
    merchant = lines[i + 1].strip()
    if not merchant:
        return None, 0
    raw_data: dict = {"section": "domestic", "original_currency": "BRL"}
    if amount < 0:
        # Estorno/refund — keep it (it nets against the purchase on the
        # report) and flag it so consumers don't treat it as an expense
        # waiting for a receipt.
        raw_data["credit"] = True
    return {
        "date": _iso_date(date_m, year, post_md),
        "description": merchant,
        "amount": amount,
        "category": _guess_category(merchant),
        "raw_data": raw_data,
    }, 3


def _try_international(lines: list[str], i: int, year: int, post_md: Optional[tuple] = None):
    """Match the 8-line int'l pattern at position i. Returns (txn, n_used).

    Strict: the last line MUST match "Dólar de Conversão R$ X,XX" — that's
    what tells us we found a real int'l transaction (and not a domestic
    triplet whose 4th line happens to look like a city).
    """
    if i + 7 >= len(lines):
        return None, 0
    date_m = _DATE_RE.match(lines[i].strip())
    if not date_m:
        return None, 0
    brl_line = lines[i + 2].strip()
    if not _BRL_AMOUNT_RE.match(brl_line):
        return None, 0
    fx_m = _FX_RE.match(lines[i + 7].strip())
    if not fx_m:
        return None, 0  # not an int'l txn — let domestic try
    native_m = _NATIVE_AMOUNT_RE.match(lines[i + 4].strip())
    ccy_m = _CCY_RE.match(lines[i + 5].strip())
    if not (native_m and ccy_m):
        return None, 0

    merchant = lines[i + 1].strip()
    if not merchant:
        return None, 0
    brl_amount = _parse_brl(brl_line)
    if brl_amount is None or brl_amount == 0:
        return None, 0
    native_amount = _parse_brl(native_m.group(0))
    fx_rate = _parse_brl(fx_m.group(1))
    city = lines[i + 3].strip()
    # Newer templates print a shortened merchant echo in the city slot for
    # online purchases ("METALPRICEAPI.COM" → "METALPRICEAPI"). A city that's
    # just the merchant again is noise — drop it rather than surface it.
    if city and _is_merchant_echo(city, merchant):
        city = None

    raw_data: dict = {
        "section": "international",
        "original_currency": ccy_m.group(1),
        "original_amount": native_amount,
        "fx_to_brl": fx_rate,
        "city": city,
    }
    if brl_amount < 0:
        raw_data["credit"] = True
    return {
        "date": _iso_date(date_m, year, post_md),
        "description": merchant,
        "amount": brl_amount,
        "category": _guess_category(merchant),
        "raw_data": raw_data,
    }, 8


# ─── Helpers ───────────────────────────────────────────────────────────────


def _is_merchant_echo(city: str, merchant: str) -> bool:
    """True when the "city" line is really the merchant name again.

    Compares alphanumeric-only, case-insensitive forms. Echo if either is a
    prefix of the other ("METALPRICEAPI" ~ "METALPRICEAPI.COM") or they share
    a ≥8-char prefix ("ANTHROPIC.COM" ~ "ANTHROPIC*CLAUDESUB"). Min 6 chars
    so a real short city sharing a few letters can't be swallowed.
    """

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    c, m = norm(city), norm(merchant)
    if len(c) < 6 or not m:
        return False
    if c.startswith(m) or m.startswith(c):
        return True
    common = 0
    for a, b in zip(c, m):
        if a != b:
            break
        common += 1
    return common >= 8


def _parse_brl(s: str) -> Optional[float]:
    """Parse '4.662,00' / '21,88' / '- 123,45' to float, sign preserved.
    Returns None on junk."""
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    negative = s.startswith("-")
    s = s.lstrip("-")
    # Remove thousands separators, swap decimal comma to dot.
    s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v


def _iso_date(date_m: re.Match, year: int, post_md: Optional[tuple] = None) -> str:
    day, month = int(date_m.group(1)), int(date_m.group(2))
    # Year boundary: a January statement (Postagem 05/01/2026) lists December
    # purchases as "15/12" — those belong to the PREVIOUS year. Anything dated
    # after the posting date can't be this billing year.
    if post_md and (month, day) > post_md:
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


# Lightweight category inference from merchant text. Same intent as the
# receipt-side `_infer_category` — keep the buckets aligned so receipt /
# statement comparisons surface in the same Excel category.
_CATEGORY_KEYWORDS = {
    "transportation": (
        "uber",
        "lyft",
        "taxi",
        "rideshare",
        "didi",
        "cabify",
        "metro",
        "subway",
        "transit",
        "trip",
    ),
    "lodging": (
        "hotel",
        "hostel",
        "inn ",
        "marriott",
        "hyatt",
        "sutton place",
        "double tree",
        "doubletree",
        "hilton",
        "airbnb",
        "ibis",
    ),
    "travel": (
        "latam",
        "azul",
        "gol ",
        "delta",
        "united",
        "american airlines",
        "air canada",
        "westjet",
        "lufthansa",
        "klm",
        "swiss",
    ),
    "meals": (
        "restaurant",
        "ristorante",
        "starbucks",
        "coffee",
        "cafe",
        "café",
        "pizza",
        "burger",
        "kitchen",
        "grill",
        "diner",
        "barolio",
        "ramen",
        "sushi",
        "ipanema",
        "panin",
        "tutti matti",
        "kingyo",
        "chaplin",
        "ikkousha",
        "olly fresco",
        "second cup",
        "trees coffee",
        "perfecto",
        "ciclo espresso",
        "beertown",
    ),
    "office_supplies": (
        "office depot",
        "staples",
        "kalunga",
        "shoppers drug",
    ),
}


def _guess_category(merchant: str) -> str:
    m = (merchant or "").lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(kw in m for kw in kws):
            return cat
    return "other"
