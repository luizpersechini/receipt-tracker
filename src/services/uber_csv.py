"""
Uber rider trip-history CSV importer.

This is Phase A of the Uber integration: bulk-import rides from the CSV that
Uber emails you when you request your data at uber.com/privacy/downloadyourdata.

Every row becomes a `receipt` entry directly — no OCR. Vendor is hard-coded
to "Uber", category to "transportation", expense_type to "corp_card" (the
caller's stated setup: corp card attached to the Uber Business Profile).

The CSV format has shifted over time, so column lookup is tolerant: we try
several known header aliases before giving up on a row.
"""

from __future__ import annotations

import csv
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Header alias tables ─────────────────────────────────────────────────────
# Order within each list matters: earlier entries are preferred. Aliases cover
# both Uber's older "Trip or Order ID" CSV and the modern data-download
# format (trips_data-0.csv) which has snake_case columns and no trip_id.

_HEADER_ALIASES = {
    "trip_id":        ["trip or order id", "trip id", "request id", "uuid"],
    "begin_time":     [
        "begintrip_timestamp_local", "begintrip_timestamp_utc",
        "request_timestamp_local", "request_timestamp_utc",
        "begin trip time", "request time", "trip start time", "begin time",
    ],
    "end_time":       [
        "dropoff_timestamp_local", "dropoff_timestamp_utc",
        "dropoff time", "end trip time", "trip end time", "end time",
    ],
    "begin_address":  [
        "begintrip_address",
        "begin trip address", "pickup address", "begin address", "pickup",
    ],
    "end_address":    [
        "dropoff_address",
        "dropoff address", "drop off address", "end trip address", "end address", "dropoff",
    ],
    "fare":           [
        # Most accurate "what the rider paid" comes first.
        "client_upfront_fare_local", "original_fare_local",
        "fare amount", "total fare", "fare", "amount", "total charged", "fare_amount",
    ],
    "currency":       ["currency_code", "fare currency", "currency", "currency code"],
    "city":           ["city_name", "city"],
    "payment_method": ["card_number", "payment method", "payment_method", "payment"],
    "expense_code":   ["expense code", "expense_code", "request type", "profile", "profile_type"],
    "service_type":   [
        "global_product_name", "product_type_name",
        "product type", "service",
    ],
    "distance":       ["trip_distance_miles", "distance (mi)", "distance (km)", "distance"],
    "duration":       ["trip_duration_seconds", "duration (min)", "duration"],
    "status":         ["status", "trip status"],
    "begin_lat":      ["begintrip_lat"],
    "begin_lng":      ["begintrip_lng"],
}

# Trip statuses we should not import.
_SKIP_STATUSES = {
    "canceled", "cancelled",
    "rider canceled", "driver canceled",
    "rider_canceled", "driver_canceled",
    "unfulfilled", "failed",
}


@dataclass
class UberRow:
    """Normalised representation of one ride for downstream insertion."""
    trip_id: str
    date: Optional[str]              # YYYY-MM-DD
    begin_time_raw: Optional[str]
    end_time_raw: Optional[str]
    begin_address: Optional[str]
    end_address: Optional[str]
    city: Optional[str]
    fare: Optional[float]
    currency: Optional[str]
    payment_method: Optional[str]
    expense_code: Optional[str]      # the column where Uber tags Business profile
    service_type: Optional[str]
    distance: Optional[str]
    is_business: bool

    def synthetic_filename(self) -> str:
        return f"uber_{self.trip_id}.csv"

    def to_metadata(self) -> dict:
        """Shape this row like the metadata blob the OCR pipeline produces."""
        addr = None
        if self.begin_address and self.end_address:
            addr = f"{self.begin_address} → {self.end_address}"
        elif self.begin_address or self.end_address:
            addr = self.begin_address or self.end_address
        return {
            "date": self.date,
            "vendor_name": "Uber",
            "vendor_address": addr,
            "total_amount": self.fare,
            "currency": self.currency,
            "category": "transportation",
            "city": self.city,
            "country": None,
            "payment_method": self.payment_method,
            "items": [],
            "language": None,
            "tax_amount": None,
            "tip_amount": None,
            "service_charge": None,
            # Uber-specific extras (kept for transparency / debugging)
            "uber_trip_id": self.trip_id,
            "uber_service": self.service_type,
            "uber_distance": self.distance,
            "uber_expense_code": self.expense_code,
        }


# ─── Header detection ────────────────────────────────────────────────────────

def _normalise(h: str) -> str:
    return (h or "").strip().lower()


def _resolve_columns(headers: list[str]) -> dict[str, str]:
    """Return {logical_name: actual_header} for the columns we can identify."""
    norm = {_normalise(h): h for h in headers}
    mapping: dict[str, str] = {}
    for logical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                mapping[logical] = norm[alias]
                break
    return mapping


def is_uber_csv(filepath: str) -> bool:
    """Cheap header sniff: file looks like one of Uber's rider CSV formats.

    Accepts both the legacy "Trip or Order ID" format and the modern data-
    download format (no trip_id, snake_case columns).
    """
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, [])
    except OSError:
        return False
    cols = _resolve_columns(headers)
    # Either: explicit trip_id + (begin_time or fare) — old format
    # Or:     begin_time + (fare or begin_address) + currency — new format
    has_old = "trip_id" in cols and ("begin_time" in cols or "fare" in cols)
    has_new = (
        "begin_time" in cols
        and "currency" in cols
        and ("fare" in cols or "begin_address" in cols)
    )
    return has_old or has_new


# ─── Per-field parsing ───────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
]


def _parse_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: take the first 10 chars if they look like a date
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _parse_amount(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip currency symbols, group separators
    for ch in ("$", "R$", "€", "£", "¥"):
        s = s.replace(ch, "")
    s = s.strip()
    # If both '.' and ',' present, assume '.' is thousands sep
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        # comma is decimal separator (locale-dependent)
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _is_business(payment_method: Optional[str], expense_code: Optional[str]) -> bool:
    """Heuristic: the row is a business-profile ride."""
    haystack = " ".join([payment_method or "", expense_code or ""]).lower()
    return "business" in haystack or "corp" in haystack or "work" in haystack


# ─── Main parse ──────────────────────────────────────────────────────────────

def _synthesize_trip_id(*, begin_time: Optional[str], begin_lat: Optional[str],
                        begin_lng: Optional[str], fare: Optional[float]) -> str:
    """Stable hash for de-duplicating CSV rows that have no native trip id."""
    parts = [
        (begin_time or "").strip(),
        (begin_lat or "").strip(),
        (begin_lng or "").strip(),
        f"{fare:.2f}" if fare is not None else "",
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"syn-{digest}"


def parse_uber_csv(filepath: str, *, business_only: bool = False,
                   after: Optional[str] = None,
                   before: Optional[str] = None) -> list:
    """
    Parse an Uber rider CSV into a list of UberRow.

    `business_only=True` keeps only rows that look like Business Profile rides.
    Note: the modern data-download CSV doesn't expose the profile choice, so
    this filter is best-effort and may match nothing on those files.

    `after` and `before` (YYYY-MM-DD) optionally clip the date range. Both
    bounds are inclusive.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(filepath)

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        cols = _resolve_columns(headers)

        # New format has no trip_id but does have begin_time + currency.
        if "begin_time" not in cols:
            raise ValueError(
                "CSV doesn't look like an Uber trip history (no recognised "
                f"begin-time column). Headers seen: {headers}"
            )

        rows: list = []
        skipped_cancelled = 0
        skipped_non_business = 0
        skipped_no_fare = 0
        skipped_out_of_range = 0

        def get(r, key):
            col = cols.get(key)
            return r.get(col) if col else None

        for raw in reader:
            status = (get(raw, "status") or "").strip().lower()
            if status in _SKIP_STATUSES:
                skipped_cancelled += 1
                continue

            begin_time_raw = get(raw, "begin_time")
            end_time_raw = get(raw, "end_time")
            fare = _parse_amount(get(raw, "fare"))

            if fare is None:
                skipped_no_fare += 1
                continue

            date = _parse_date(begin_time_raw) or _parse_date(end_time_raw)
            if after and date and date < after:
                skipped_out_of_range += 1
                continue
            if before and date and date > before:
                skipped_out_of_range += 1
                continue

            trip_id = (get(raw, "trip_id") or "").strip()
            if not trip_id:
                trip_id = _synthesize_trip_id(
                    begin_time=begin_time_raw,
                    begin_lat=get(raw, "begin_lat"),
                    begin_lng=get(raw, "begin_lng"),
                    fare=fare,
                )

            payment_method = (get(raw, "payment_method") or "").strip() or None
            expense_code = (get(raw, "expense_code") or "").strip() or None
            row = UberRow(
                trip_id=trip_id,
                date=date,
                begin_time_raw=(begin_time_raw or "").strip() or None,
                end_time_raw=(end_time_raw or "").strip() or None,
                begin_address=(get(raw, "begin_address") or "").strip() or None,
                end_address=(get(raw, "end_address") or "").strip() or None,
                city=(get(raw, "city") or "").strip() or None,
                fare=fare,
                currency=((get(raw, "currency") or "").strip().upper() or None),
                payment_method=payment_method,
                expense_code=expense_code,
                service_type=(get(raw, "service_type") or "").strip() or None,
                distance=(get(raw, "distance") or "").strip() or None,
                is_business=_is_business(payment_method, expense_code),
            )

            if business_only and not row.is_business:
                skipped_non_business += 1
                continue

            rows.append(row)

    logger.info(
        "uber_csv: parsed=%d skipped(cancelled=%d, no_fare=%d, "
        "non_business=%d, out_of_range=%d) headers_count=%d",
        len(rows), skipped_cancelled, skipped_no_fare,
        skipped_non_business, skipped_out_of_range, len(headers),
    )
    return rows
