"""
Gmail-based ingestion of Uber ride receipts (Phase B of the Uber integration).

Every Uber ride sends a confirmation email. This module:
  1. Authenticates to Gmail via stored OAuth token (see scripts/gmail_auth.py
     for the one-time setup).
  2. Lists messages matching `settings.gmail_query` that don't yet have the
     `settings.gmail_processed_label` applied.
  3. Parses each email's HTML body into the same metadata shape the OCR
     pipeline produces — so the rest of the app (renderer, reports, FX) just
     works without changes.
  4. Saves each parsed message as a receipt, then applies the processed
     label so we don't reimport it.

The parsing is best-effort and tolerant; Uber's email layout shifts.
We extract what we can and leave fields as None if we can't.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..config import settings

logger = logging.getLogger(__name__)


# ─── OAuth / Gmail client ────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _load_credentials():
    """Load stored token, refreshing it if needed. Returns None if not set up."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = settings.gmail_token_path
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to refresh Gmail token: %s", e)
            return None
    return creds if creds and creds.valid else None


def get_service():
    """Return a googleapiclient Gmail service, or None if not authenticated."""
    creds = _load_credentials()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def is_configured() -> bool:
    """True when both credentials.json and token.json exist."""
    return settings.gmail_credentials_path.exists() and settings.gmail_token_path.exists()


def _categorise_gmail_error(exc: Exception) -> dict:
    """Translate a raw HttpError / generic exception from the Gmail API into
    a frontend-friendly error dict with a `kind` discriminator the UI can
    branch on (and a short human-readable message)."""
    text = str(exc)
    lower = text.lower()
    # OAuth client project was deleted in Google Cloud
    if "project" in lower and "deleted" in lower:
        kind = "project_deleted"
        msg = (
            "The Google Cloud project that owns your OAuth client was deleted. "
            "Create or restore a project, download a fresh credentials.json, "
            "and upload it via the Gmail card."
        )
    elif "invalid_client" in lower or "client not found" in lower:
        kind = "invalid_client"
        msg = "Your OAuth client is invalid — upload a new credentials.json."
    elif "invalid_grant" in lower or "token has been expired" in lower or "token expired" in lower:
        kind = "token_expired"
        msg = "OAuth token expired. Click Reconnect Gmail to re-authenticate."
    elif "gmail api has not been used" in lower or "api is not enabled" in lower:
        kind = "api_disabled"
        msg = "Gmail API is not enabled on this Google Cloud project — enable it in the console."
    elif "not authenticated" in lower:
        kind = "token_missing"
        msg = "Gmail not authenticated. Click Reconnect Gmail."
    else:
        kind = "unknown"
        msg = f"Gmail API call failed: {text}"
    logger.warning("Gmail error categorised as %s: %s", kind, text)
    return {
        "success": False,
        "error": msg,
        "error_kind": kind,
        "imported": 0, "skipped": 0, "errors": [],
    }


def run_oauth_flow(timeout: int = 300) -> dict:
    """
    Run Google's installed-app OAuth flow and persist the resulting token.

    Blocks until the user finishes the consent screen in their browser (or
    `timeout` seconds elapse). Intended to be called from a route handler
    via `asyncio.to_thread` so it doesn't block the event loop.

    Returns a dict shaped {"success": bool, "error": str | None}.
    """
    if not settings.gmail_credentials_path.exists():
        return {
            "success": False,
            "error": (
                f"credentials.json not found at {settings.gmail_credentials_path}. "
                "Download a Desktop OAuth client from Google Cloud Console and "
                "drop the file at that path before connecting."
            ),
        }

    settings.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(settings.gmail_credentials_path), SCOPES,
        )
        # port=0 picks a free port; the flow spins up a tiny local server,
        # opens the user's browser, and waits for the redirect.
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=timeout,
            success_message=(
                "Gmail connected to Receipt Tracker. You can close this tab."
            ),
        )
        settings.gmail_token_path.write_text(creds.to_json(), encoding="utf-8")
        return {"success": True, "token_path": str(settings.gmail_token_path)}
    except Exception as e:
        logger.exception("OAuth flow failed")
        return {"success": False, "error": str(e)}


# ─── Label management ────────────────────────────────────────────────────────

def _get_or_create_label_id(service, name: str) -> str:
    """Look up a Gmail label by name; create it if missing."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl.get("name") == name:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


# ─── Message fetching ────────────────────────────────────────────────────────

def list_message_ids(service, query: str, max_results: int) -> list:
    """Return a list of Gmail message IDs matching `query`."""
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return [m["id"] for m in result.get("messages", [])]


def fetch_message(service, msg_id: str) -> dict:
    """Fetch one message with full payload."""
    return service.users().messages().get(
        userId="me",
        id=msg_id,
        format="full",
    ).execute()


def extract_html_body(message: dict) -> Optional[str]:
    """Walk the MIME tree and return the first text/html part decoded as UTF-8."""
    def walk(part):
        if part.get("mimeType") == "text/html" and "data" in part.get("body", {}):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            found = walk(sub)
            if found:
                return found
        return None

    payload = message.get("payload", {})
    return walk(payload)


def extract_plain_body(message: dict) -> Optional[str]:
    def walk(part):
        if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            found = walk(sub)
            if found:
                return found
        return None
    return walk(message.get("payload", {}))


def header(message: dict, name: str) -> Optional[str]:
    headers = (message.get("payload", {}) or {}).get("headers", []) or []
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value")
    return None


# ─── HTML parsing ────────────────────────────────────────────────────────────

@dataclass
class ParsedUberEmail:
    msg_id: str                    # Gmail message id — always present, used as dedup fallback
    trip_id: Optional[str]
    date: Optional[str]            # YYYY-MM-DD
    total_amount: Optional[float]
    currency: Optional[str]
    pickup: Optional[str]
    dropoff: Optional[str]
    payment_method: Optional[str]
    is_business: bool
    raw_subject: Optional[str] = None
    raw_snippet: Optional[str] = None
    debug: dict = field(default_factory=dict)

    def synthetic_filename(self, ext: str = ".eml") -> str:
        # Prefer the Uber trip-uuid (stable across resends), fall back to the
        # Gmail message id when Uber's templates wrap trip links in tracker
        # URLs that obscure the uuid.
        base = self.trip_id or f"gm-{self.msg_id}"
        if not ext.startswith("."):
            ext = "." + ext
        return f"uber_{base}{ext}"

    def to_metadata(self) -> dict:
        addr = None
        if self.pickup and self.dropoff:
            addr = f"{self.pickup} → {self.dropoff}"
        elif self.pickup or self.dropoff:
            addr = self.pickup or self.dropoff
        return {
            "date": self.date,
            "vendor_name": "Uber",
            "vendor_address": addr,
            "total_amount": self.total_amount,
            "currency": self.currency,
            "category": "transportation",
            "city": None,
            "country": None,
            "payment_method": self.payment_method,
            "items": [],
            "language": None,
            "tax_amount": None,
            "tip_amount": None,
            "service_charge": None,
            "uber_trip_id": self.trip_id,
            "uber_source": "gmail",
        }


_AMOUNT_PATTERNS = [
    # "Total $32.29", "Total: R$ 41,78", "Total CA$ 12.34"
    re.compile(r"(?:total|amount)\s*[:\-]?\s*([A-Z]{0,3}\$|R\$|€|£|¥|CA\$|US\$)?\s*([0-9][0-9.,]*)", re.IGNORECASE),
]

_CURRENCY_SYMBOL_MAP = {
    "$":   "USD",
    "us$": "USD",
    "ca$": "CAD",
    "r$":  "BRL",
    "€":   "EUR",
    "£":   "GBP",
    "¥":   "JPY",
}

_BUSINESS_HINTS = re.compile(r"\bbusiness\s+(profile|trip|ride)\b", re.IGNORECASE)


def _parse_amount_str(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    # Detect comma decimal vs thousands separator
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _extract_total(text: str) -> tuple[Optional[float], Optional[str]]:
    """Return (amount, currency) — currency may be None."""
    for pat in _AMOUNT_PATTERNS:
        for m in pat.finditer(text):
            sym = (m.group(1) or "").lower().strip()
            amt = _parse_amount_str(m.group(2))
            if amt is None:
                continue
            currency = _CURRENCY_SYMBOL_MAP.get(sym)
            # Bare "$" is ambiguous — HST (Harmonized Sales Tax) is Canada-only,
            # so its presence is a reliable signal the ride was in CAD.
            if currency == "USD" and re.search(r"\bHST\b", text):
                currency = "CAD"
            return amt, currency
    return None, None


_TRIP_URL = re.compile(r"trips/([0-9a-fA-F-]{8,})")


def _extract_trip_id(html: str, text: str) -> Optional[str]:
    # Trip URLs in body usually contain the UUID
    for blob in (html, text):
        if not blob:
            continue
        m = _TRIP_URL.search(blob)
        if m:
            return m.group(1)
    return None


_MONTHS = {
    # English
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Portuguese
    "fev": 2, "abr": 4, "mai": 5, "ago": 8, "set": 9, "out": 10, "dez": 12,
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


def _extract_date_from_body(text: Optional[str]) -> Optional[str]:
    """
    Look for a ride-date in the receipt body — Uber prints it near the top,
    e.g. "Apr 17, 2026" or "17 de abril de 2026". Preferred over the Date:
    header because the email can be resent later for the same ride.
    """
    if not text:
        return None

    # "Apr 17, 2026" / "April 17 2026"
    m = re.search(
        r"\b([A-Za-zÀ-ÿ]{3,12})\.?\s+(\d{1,2}),?\s+(\d{4})\b",
        text,
    )
    if m:
        mon = m.group(1).lower().strip(".")
        mn = _MONTHS.get(mon)
        if mn:
            try:
                return datetime(int(m.group(3)), mn, int(m.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # "17 de abril de 2026" / "17 abr 2026"
    m = re.search(
        r"\b(\d{1,2})\s+(?:de\s+)?([A-Za-zÀ-ÿ]{3,12})\.?\s+(?:de\s+)?(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        mon = m.group(2).lower().strip(".")
        mn = _MONTHS.get(mon)
        if mn:
            try:
                return datetime(int(m.group(3)), mn, int(m.group(1))).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # ISO date
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    return None


def _extract_date_from_subject(subject: Optional[str]) -> Optional[str]:
    if not subject:
        return None
    return _extract_date_from_body(subject)


def _extract_date_from_header(date_header: Optional[str]) -> Optional[str]:
    """Parse RFC 2822 Date header into YYYY-MM-DD."""
    if not date_header:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_header)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _extract_addresses_from_soup(soup) -> tuple[Optional[str], Optional[str]]:
    """
    Uber emails typically have a 'From' / 'To' pair near the route map. We
    look for ordered <td> or <div> elements that contain the pickup time and
    address. This is best-effort across template versions.
    """
    text = soup.get_text("\n", strip=True)
    pickup = dropoff = None

    # Pattern: time line like "8:23 PM" followed by address line
    time_addr = re.findall(
        r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)\s*\n([^\n]{5,120})",
        text,
    )
    if len(time_addr) >= 2:
        pickup = time_addr[0][1].strip()
        dropoff = time_addr[1][1].strip()
        return pickup, dropoff

    # Fallback: explicit "From X To Y" phrasing
    m = re.search(r"From\s+(.+?)\s+To\s+(.+?)(?:\n|$)", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return None, None


def _extract_payment_method(text: str) -> Optional[str]:
    # "Visa ••••4321" or "Mastercard ending in 1234"
    m = re.search(
        r"((?:Visa|Mastercard|Amex|American Express|Discover|UnionPay|Cash|PayPal)[^,\n]{0,40})",
        text,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def parse_uber_email(*, msg_id: str = "unknown",
                     subject: Optional[str], date_header: Optional[str],
                     html: Optional[str], plain: Optional[str]) -> ParsedUberEmail:
    """
    Best-effort extraction of the receipt fields from one Uber email.
    `html` is preferred; `plain` is used as a fallback for text-only scrapes.
    `msg_id` (Gmail message id) is required for stable dedup when Uber's
    template doesn't expose the trip uuid.
    """
    soup = BeautifulSoup(html or "", "html.parser") if html else None
    visible_text = soup.get_text("\n", strip=True) if soup else (plain or "")

    total, currency = _extract_total(visible_text)
    trip_id = _extract_trip_id(html or "", visible_text)
    pickup, dropoff = _extract_addresses_from_soup(soup) if soup else (None, None)
    payment = _extract_payment_method(visible_text)
    # Body date wins — the email Date: header is the send time, which can be
    # weeks after the actual ride if Uber re-sends the receipt.
    date = (
        _extract_date_from_body(visible_text)
        or _extract_date_from_subject(subject)
        or _extract_date_from_header(date_header)
    )
    subj_l = (subject or "").lower()
    # `[Business]` prefix is Uber's unambiguous business-profile marker
    business = (
        "[business]" in subj_l
        or "business" in subj_l
        or bool(_BUSINESS_HINTS.search(visible_text))
    )

    return ParsedUberEmail(
        msg_id=msg_id,
        trip_id=trip_id,
        date=date,
        total_amount=total,
        currency=currency,
        pickup=pickup,
        dropoff=dropoff,
        payment_method=payment,
        is_business=business,
        raw_subject=subject,
        raw_snippet=visible_text[:200] if visible_text else None,
    )


# ─── Top-level orchestrator ──────────────────────────────────────────────────

def _gmail_date(s: Optional[str]) -> Optional[str]:
    """Convert YYYY-MM-DD → YYYY/MM/DD (Gmail's date format). Returns None
    if the input doesn't match a sane ISO date so we never inject garbage
    into the query."""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s.replace("-", "/")
    return None


def build_query(base: str, *, after: Optional[str] = None, before: Optional[str] = None,
                exclude_label: Optional[str] = None) -> str:
    """Compose a Gmail search query from the base + optional date / label clauses."""
    parts = [base]
    if exclude_label:
        parts.append(f"-label:{exclude_label}")
    a = _gmail_date(after)
    b = _gmail_date(before)
    if a:
        parts.append(f"after:{a}")
    if b:
        # Gmail's `before:` is exclusive of the date — bump by one day so
        # callers can pass an inclusive end-date without surprises.
        try:
            d = datetime.strptime(b, "%Y/%m/%d") + timedelta(days=1)
            parts.append(f"before:{d.strftime('%Y/%m/%d')}")
        except ValueError:
            parts.append(f"before:{b}")
    return " ".join(p for p in parts if p)


def sync_new_receipts(*, business_only: bool = False,
                      max_messages: Optional[int] = None,
                      after: Optional[str] = None,
                      before: Optional[str] = None,
                      attach_only: bool = False) -> dict:
    """
    Fetch unprocessed Uber emails, parse them, save as receipts, mark each as
    processed.

    `after` / `before` (YYYY-MM-DD) optionally narrow the Gmail search by
    ride/email date.

    `attach_only=True` makes the CSV the source of truth: emails that match
    an existing receipt (by date + amount + currency) get their PDF attached
    to that row; emails with no match are reported back in `unmatched` and
    NOT inserted as new rows. Use this when you've already imported your
    full ride list from the Uber data-download CSV.
    """
    from ..db import database as db

    service = get_service()
    if service is None:
        return {
            "success": False,
            "error": "Gmail not authenticated. Run scripts/gmail_auth.py.",
            "imported": 0, "skipped": 0, "errors": [],
        }

    try:
        label_id = _get_or_create_label_id(service, settings.gmail_processed_label)
    except Exception as e:
        # Common cases: Gmail API not enabled, OAuth client's project has
        # been deleted, or the token is for a now-orphaned client. Surface
        # a clean error dict instead of bubbling a raw HttpError.
        return _categorise_gmail_error(e)
    # Narrow at the Gmail layer when caller wants only Business-profile rides
    # — Uber's actual subject prefix is `[Business]`. Pushes the filter to
    # the server so a 100-msg cap actually returns up to 100 business rides
    # instead of being diluted by promos / Eats / account notifications.
    query_base = settings.gmail_query
    if business_only:
        query_base = f'{query_base} subject:"[Business]"'
    # In attach-only mode, the rt-processed label is irrelevant: we don't
    # care if a message was previously examined, only whether its matching
    # row already has a PDF. The inner loop's dedup makes re-scans idempotent
    # (no-op when the row's filename is already a .pdf).
    exclude_label = None if attach_only else settings.gmail_processed_label
    query = build_query(
        query_base,
        after=after, before=before,
        exclude_label=exclude_label,
    )
    limit = max_messages or settings.gmail_max_messages_per_sync

    try:
        msg_ids = list_message_ids(service, query, limit)
    except Exception as e:
        logger.exception("Gmail list failed")
        return _categorise_gmail_error(e)

    imported = 0
    skipped = 0
    unmatched: list = []      # only populated when attach_only=True
    upgraded = 0              # count of existing rows that gained a PDF
    errors: list[str] = []

    for msg_id in msg_ids:
        try:
            msg = fetch_message(service, msg_id)
        except Exception as e:
            errors.append(f"fetch {msg_id}: {e}")
            continue

        subject = header(msg, "Subject")
        date_hdr = header(msg, "Date")
        html = extract_html_body(msg)
        plain = extract_plain_body(msg)

        parsed = parse_uber_email(
            msg_id=msg_id,
            subject=subject, date_header=date_hdr,
            html=html, plain=plain,
        )

        if business_only and not parsed.is_business:
            # The Gmail query already narrowed to subject:"[Business]", so
            # this is a safety net for Gmail's full-text quirks. Don't label
            # — if the user disables business_only later, these messages
            # should still be re-scannable.
            skipped += 1
            continue

        if parsed.total_amount is None:
            # Either a non-receipt (promo / account / summary) OR a real
            # receipt whose template our parser didn't match. We can't tell
            # which from here, so don't label — leave it for a future sync
            # once the parser is improved. Use POST /api/gmail/reset to
            # un-label anything we've already mislabeled.
            skipped += 1
            continue

        # Dedup against both possible extensions (existing rows may be .eml
        # from pre-PDF syncs).
        eml_filename = parsed.synthetic_filename(".eml")
        pdf_filename = parsed.synthetic_filename(".pdf")
        if db.get_receipt_by_filename(pdf_filename) or db.get_receipt_by_filename(eml_filename):
            skipped += 1
            _apply_label(service, msg_id, label_id)
            continue

        # Cross-source dedup: the same ride may already be in the DB from a
        # CSV import. Match by (date, amount, currency). If found, attach our
        # PDF to that row instead of creating a duplicate.
        existing = db.find_uber_receipt_by_date_amount(
            parsed.date, parsed.total_amount, parsed.currency,
        )

        if not existing and attach_only:
            # CSV is the source of truth — don't insert a new row.
            unmatched.append({
                "msg_id": msg_id,
                "subject": subject,
                "date": parsed.date,
                "amount": parsed.total_amount,
                "currency": parsed.currency,
            })
            skipped += 1
            # Deliberately NOT labelling — user may want to retry after
            # uploading a wider CSV that covers these rides.
            continue

        # Try to render the email body as a PDF. If WeasyPrint isn't
        # installed (or fails on this specific email), fall back to the
        # gmail:// pointer so the receipt still gets recorded.
        filename, filepath = _persist_receipt_pdf(parsed, html, msg_id)

        if existing:
            # Upgrade the existing CSV-imported row with the PDF we just
            # rendered, if it doesn't already have one.
            if filename.endswith(".pdf") and not (existing.get("filename") or "").endswith(".pdf"):
                try:
                    db.update_receipt_file(existing["id"], filename, filepath)
                    upgraded += 1
                except Exception as e:
                    errors.append(f"upgrade {existing['id']}: {e}")
            else:
                skipped += 1
            _apply_label(service, msg_id, label_id)
            continue

        try:
            db.save_receipt(
                filename=filename,
                filepath=filepath,
                metadata=parsed.to_metadata(),
                description=(f"Uber · {parsed.pickup or ''} → {parsed.dropoff or ''}"
                             .strip(" ·→") or "Uber ride via Gmail"),
            )
            _apply_label(service, msg_id, label_id)
            imported += 1
        except Exception as e:
            errors.append(f"save {msg_id} ({parsed.trip_id}): {e}")

    return {
        "success": True,
        "imported": imported,
        "upgraded": upgraded,
        "skipped": skipped,
        "scanned": len(msg_ids),
        "errors": errors,
        "business_only": business_only,
        "attach_only": attach_only,
        "unmatched": unmatched,
    }


def _persist_receipt_pdf(parsed: "ParsedUberEmail", html: Optional[str],
                         msg_id: str) -> tuple:
    """
    Try to render the email's HTML as a PDF and save it under
    settings.receipts_dir. On success returns (.pdf filename, absolute path).
    On any failure falls back to (.eml filename, gmail:// pointer) so the
    receipt is still recorded — without the PDF.
    """
    eml_filename = parsed.synthetic_filename(".eml")
    if not html:
        return eml_filename, f"gmail://{msg_id}"

    try:
        from . import receipt_pdf  # local import keeps weasyprint optional
        if not receipt_pdf.is_available():
            return eml_filename, f"gmail://{msg_id}"
        pdf_bytes = receipt_pdf.render_html_to_pdf(html)
    except Exception as e:
        logger.warning("PDF render failed for %s: %s", msg_id, e)
        return eml_filename, f"gmail://{msg_id}"

    settings.receipts_dir.mkdir(parents=True, exist_ok=True)
    pdf_filename = parsed.synthetic_filename(".pdf")
    pdf_path = settings.receipts_dir / pdf_filename
    try:
        pdf_path.write_bytes(pdf_bytes)
    except OSError as e:
        logger.warning("PDF write failed for %s: %s", msg_id, e)
        return eml_filename, f"gmail://{msg_id}"
    return pdf_filename, str(pdf_path)


def render_pdfs_for_existing(max_messages: int = 200) -> dict:
    """
    Backfill PDFs for receipts that were imported before PDF-rendering was
    available. Finds receipts whose filepath starts with `gmail://`,
    re-fetches the email, renders the PDF, saves it, and updates the row's
    filename/filepath to point at the saved PDF.
    """
    from ..db import database as db

    service = get_service()
    if service is None:
        return {"success": False, "error": "Gmail not authenticated."}

    # Pull every Gmail-sourced receipt that doesn't yet have a real file.
    all_receipts = db.get_all_receipts()
    targets = [
        r for r in all_receipts
        if (r.get("filepath") or "").startswith("gmail://")
    ][:max_messages]

    if not targets:
        return {"success": True, "rendered": 0, "skipped": 0, "scanned": 0}

    rendered = 0
    skipped = 0
    errors: list = []

    for r in targets:
        msg_id = (r["filepath"] or "").removeprefix("gmail://")
        if not msg_id:
            skipped += 1
            continue
        try:
            msg = fetch_message(service, msg_id)
        except Exception as e:
            errors.append(f"fetch {msg_id}: {e}")
            continue

        html = extract_html_body(msg)
        if not html:
            skipped += 1
            continue

        parsed = parse_uber_email(
            msg_id=msg_id,
            subject=header(msg, "Subject"),
            date_header=header(msg, "Date"),
            html=html, plain=extract_plain_body(msg),
        )

        new_filename, new_path = _persist_receipt_pdf(parsed, html, msg_id)
        if not new_filename.endswith(".pdf"):
            skipped += 1  # render didn't succeed
            continue

        # Update the existing row to point at the rendered PDF
        try:
            db.update_receipt_file(r["id"], new_filename, new_path)
            rendered += 1
        except Exception as e:
            errors.append(f"db update {r['id']}: {e}")

    return {
        "success": True,
        "rendered": rendered,
        "skipped": skipped,
        "scanned": len(targets),
        "errors": errors,
    }


def _apply_label(service, msg_id: str, label_id: str) -> None:
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except Exception as e:
        logger.warning("Could not label message %s: %s", msg_id, e)


def debug_preview(max_messages: int = 5, include_html: bool = False) -> dict:
    """
    Non-destructive parse preview. Fetches the most recent matching messages
    (including ones that already have the rt-processed label), runs them
    through the parser, and returns the parsed result. Does NOT save to DB
    and does NOT modify any labels.

    Use this to figure out why messages aren't importing.
    """
    service = get_service()
    if service is None:
        return {"success": False, "error": "Gmail not authenticated."}

    try:
        # Drop the label-exclusion from the query so we can also preview
        # messages we previously mislabeled.
        msg_ids = list_message_ids(service, settings.gmail_query, max_messages)
    except Exception as e:
        return {"success": False, "error": f"Gmail list failed: {e}"}

    out: list = []
    for msg_id in msg_ids:
        try:
            msg = fetch_message(service, msg_id)
        except Exception as e:
            out.append({"msg_id": msg_id, "error": str(e)})
            continue

        subject = header(msg, "Subject")
        date_hdr = header(msg, "Date")
        html = extract_html_body(msg)
        plain = extract_plain_body(msg)
        parsed = parse_uber_email(
            msg_id=msg_id,
            subject=subject, date_header=date_hdr, html=html, plain=plain,
        )
        entry = {
            "msg_id": msg_id,
            "subject": subject,
            "date_header": date_hdr,
            "parsed": {
                "trip_id": parsed.trip_id,
                "date": parsed.date,
                "total_amount": parsed.total_amount,
                "currency": parsed.currency,
                "pickup": parsed.pickup,
                "dropoff": parsed.dropoff,
                "payment_method": parsed.payment_method,
                "is_business": parsed.is_business,
            },
            # Snippet helps spot what the parser is seeing without leaking
            # huge HTML payloads back to the client.
            "snippet": parsed.raw_snippet,
            "has_html": bool(html),
            "has_plain": bool(plain),
        }
        if include_html and html:
            entry["html"] = html[:8000]
        out.append(entry)

    return {"success": True, "previews": out, "scanned": len(msg_ids)}


def reset_processed_label(only_unimported: bool = True) -> dict:
    """
    Remove the rt-processed label from messages so they get re-scanned on the
    next sync. With `only_unimported=True`, skips messages whose synthetic
    filename is already in the receipts table (safe default — don't churn
    over things we successfully imported).
    """
    from ..db import database as db

    service = get_service()
    if service is None:
        return {"success": False, "error": "Gmail not authenticated."}

    label_id = _get_or_create_label_id(service, settings.gmail_processed_label)
    query = f"label:{settings.gmail_processed_label}"
    try:
        msg_ids = list_message_ids(service, query, 500)
    except Exception as e:
        return {"success": False, "error": f"Gmail list failed: {e}"}

    cleared = 0
    kept = 0
    for msg_id in msg_ids:
        if only_unimported:
            try:
                msg = fetch_message(service, msg_id)
            except Exception:
                continue
            html = extract_html_body(msg)
            plain = extract_plain_body(msg)
            parsed = parse_uber_email(
                msg_id=msg_id,
                subject=header(msg, "Subject"),
                date_header=header(msg, "Date"),
                html=html, plain=plain,
            )
            if parsed.trip_id and db.get_receipt_by_filename(parsed.synthetic_filename()):
                kept += 1
                continue
        try:
            service.users().messages().modify(
                userId="me", id=msg_id, body={"removeLabelIds": [label_id]},
            ).execute()
            cleared += 1
        except Exception as e:
            logger.warning("Could not unlabel %s: %s", msg_id, e)

    return {"success": True, "cleared": cleared, "kept_imported": kept, "scanned": len(msg_ids)}


def status() -> dict:
    """Lightweight, called from the status endpoint and the UI."""
    creds_present = settings.gmail_credentials_path.exists()
    token_present = settings.gmail_token_path.exists()
    return {
        "credentials_file_present": creds_present,
        "token_file_present": token_present,
        "configured": creds_present and token_present,
        "credentials_path": str(settings.gmail_credentials_path),
        "token_path": str(settings.gmail_token_path),
        "query": settings.gmail_query,
        "processed_label": settings.gmail_processed_label,
    }
