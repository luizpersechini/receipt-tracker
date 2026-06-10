"""
FX rate fetcher using AwesomeAPI (economia.awesomeapi.com.br).
Free, no API key required. Rates are cached daily in the DB.

AwesomeAPI returns the bid price for a currency pair, e.g.:
  GET /json/last/USD-BRL,EUR-BRL
  → {"USDBRL": {"bid": "5.1234", ...}, "EURBRL": {"bid": "5.5678", ...}}

The bid is how many units of `to_currency` you get per 1 unit of `from_currency`.
"""

import json
import logging
import re
from datetime import date as date_cls
from typing import Optional

import requests

from ..db.database import cache_fx_rate, get_cached_rate

logger = logging.getLogger(__name__)

AWESOMEAPI_BASE = "https://economia.awesomeapi.com.br/json/last"
REQUEST_TIMEOUT = 8

# Currency codes are OCR output — a junk "code" in the batched URL makes
# AwesomeAPI 404 the WHOLE request, nullifying every rate in the report.
_CODE_RE = re.compile(r"^[A-Z]{3}$")


def fetch_rates(from_currencies: list, to_currency: str = "BRL") -> dict:
    """
    Return {from_currency: rate} where rate is units of `to_currency` per 1
    unit of `from_currency`. Cached daily; same-currency pairs return 1.0.
    """
    today = date_cls.today().isoformat()
    to_currency = to_currency.upper()
    rates: dict = {}

    need_fetch: list[str] = []
    for cur in from_currencies:
        cur = cur.upper()
        if cur == to_currency:
            rates[cur] = 1.0
            continue
        if not _CODE_RE.match(cur):
            logger.warning("Ignoring invalid currency code %r", cur)
            rates[cur] = None
            continue
        cached = get_cached_rate(today, cur, to_currency)
        if cached is not None:
            rates[cur] = cached
        else:
            need_fetch.append(cur)

    if need_fetch:
        fetched = _call_api(need_fetch, to_currency)
        if not fetched and len(need_fetch) > 1:
            # The batched endpoint 404s the whole request when ANY pair is
            # unknown — retry pairs individually so one bad code can't
            # nullify every other currency's rate.
            for cur in need_fetch:
                fetched.update(_call_api([cur], to_currency))
        for cur, rate in fetched.items():
            cache_fx_rate(today, cur, to_currency, rate)
            rates[cur] = rate

        for cur in need_fetch:
            if cur.upper() not in rates:
                logger.warning("Could not fetch rate for %s→%s", cur, to_currency)
                rates[cur.upper()] = None

    return rates


def _call_api(from_currencies: list, to_currency: str) -> dict:
    pairs = ",".join(f"{c}-{to_currency}" for c in from_currencies)
    url = f"{AWESOMEAPI_BASE}/{pairs}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("AwesomeAPI request failed: %s", e)
        return {}
    except (ValueError, KeyError) as e:
        logger.error("AwesomeAPI response parse error: %s", e)
        return {}

    result: dict = {}
    for key, val in data.items():
        try:
            from_cur = key[:3].upper()
            result[from_cur] = float(val["bid"])
        except (KeyError, ValueError, TypeError):
            continue
    return result


def convert(
    amount: Optional[float], from_currency: str, to_currency: str, rates: dict
) -> Optional[float]:
    """Convert amount between currencies. Returns None if amount or rate is missing."""
    if amount is None:
        return None
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return amount
    rate = rates.get(from_currency)
    if rate is None:
        return None
    return round(amount * rate, 2)


def get_unique_currencies(receipts: list) -> list:
    """Return sorted list of unique non-null currency codes found in receipts."""
    seen: set = set()
    for r in receipts:
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        cur = (meta or {}).get("currency")
        if cur:
            seen.add(cur.upper())
    return sorted(seen)
