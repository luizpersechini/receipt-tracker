"""Expense categorization via TypeSafe's Jev (System One) API.

Opt-in (`settings.category_backend == "typesafe"`). Jev isn't a chat model:
we send a `state` JSON plus typed questions and get typed answers back. One
`choice` question per item asks which expense category fits, using exactly
the app's VALID_CATEGORIES as the option set. Every question in a request
runs in parallel server-side, so a whole statement (or receipt batch) is one
HTTP call.

Each answer carries calibrated `probabilities` over the options and a
`confidence` (distribution concentration). Callers treat confidence below
`settings.typesafe_min_confidence` as "don't trust it" and fall back to the
keyword heuristic — that policy lives in the callers, not here, so this
module stays a pure client.

Contract: https://docs.typesafe.ai/api  (POST /v1/systemone)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterable, Optional

import requests

from ..config import settings
from .statement_categories import VALID_CATEGORIES

logger = logging.getLogger(__name__)

API_URL = "https://api.typesafe.ai/v1/systemone"
BATCH_SIZE = 50
TIMEOUT_S = 60

# One-line criterion per category. These are what Jev reads to decide;
# keep them contrastive (what belongs, and what it's NOT) so adjacent
# categories don't bleed into each other.
CATEGORY_CRITERIA: dict[str, str] = {
    "meals": (
        "Food or drink consumed: restaurants, cafés, bars, bakeries, food "
        "delivery (iFood, Uber Eats), snacks, groceries bought to eat"
    ),
    "transportation": (
        "Getting around: ride-hailing (Uber, 99), taxi, bus, metro, train, "
        "tolls, parking of a ride — NOT fuel purchases, NOT airfare"
    ),
    "fuel": (
        "Buying fuel for a vehicle (gasolina, etanol, diesel) at a gas "
        "station, or vehicle-related station charges like car wash"
    ),
    "travel": (
        "Airfare, airline fees, travel agencies, long-distance bus/train "
        "tickets, travel insurance, airport services — NOT the hotel itself"
    ),
    "lodging": (
        "Hotels, hostels, Airbnb and other overnight accommodation, including "
        "hotel-billed extras like minibar or room service"
    ),
    "office_supplies": (
        "Stationery, printing, software/SaaS subscriptions, electronics or "
        "computer accessories, telecom/internet, office services"
    ),
    "other": (
        "Nothing above fits, or the merchant is ambiguous or unknown "
        "(e.g. a generic 'PAGAMENTO' line, a bank fee, an unidentified charge)"
    ),
}
assert set(CATEGORY_CRITERIA) == VALID_CATEGORIES, "criteria must cover VALID_CATEGORIES"


class TypeSafeError(RuntimeError):
    """Raised on config or transport failures — callers fall back on it."""


def _api_key() -> str:
    key = (settings.typesafe_api_key or os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not key:
        raise TypeSafeError("TYPESAFE_API_KEY is not set (category_backend=typesafe needs it)")
    return key


def _post(payload: dict) -> dict:
    """Single seam for the HTTP call so tests can stub it. Returns the parsed
    JSON body. Raises TypeSafeError on transport/HTTP failure."""
    try:
        resp = requests.post(
            API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise TypeSafeError(f"TypeSafe request failed: {e}") from e
    if resp.status_code != 200:
        # 401 bad key, 422 bad request, 429/529 back off — all surfaced
        # uniformly; the caller falls back to keywords either way.
        raise TypeSafeError(f"TypeSafe HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _item_state(item: dict) -> dict:
    """The per-item facts Jev sees. Only what's useful for categorization —
    no ids, no internal fields."""
    st = {
        "merchant_or_vendor": item.get("merchant_or_vendor") or "",
        "amount": item.get("amount"),
        "currency": item.get("currency") or "",
        "date": item.get("date") or "",
    }
    lines = item.get("items")
    if lines:
        # Receipt line items are the strongest signal (a 'CERVEJA' line on an
        # otherwise generic merchant name). Keep them short.
        st["items"] = [
            (ln.get("description") if isinstance(ln, dict) else str(ln)) or "" for ln in lines
        ][:20]
    return st


def _build_payload(batch: list[dict]) -> dict:
    """State = the batch as `items[]`; one choice question per item whose
    instructions point at that item by path, so all questions share one
    state and run in parallel."""
    state = {"items": [_item_state(it) for it in batch]}
    questions = {}
    for i, it in enumerate(batch):
        qid = f"q{i}"  # question ids are for code only, never seen by the model
        has_lines = bool(state["items"][i].get("items"))
        instructions = (
            f"Which expense category does the purchase at `items[{i}].merchant_or_vendor` "
            f"belong to? Consider the merchant name, the amount "
            f"`items[{i}].amount` `items[{i}].currency` and the date `items[{i}].date`"
            + (f", and the receipt line items in `items[{i}].items`" if has_lines else "")
            + ". Merchant names are Brazilian card-statement descriptors "
            "(often truncated, uppercase, sometimes with a city suffix)."
        )
        questions[qid] = {
            "type": "choice",
            "instructions": instructions,
            "criteria": CATEGORY_CRITERIA,
        }
    return {"state": state, "model": settings.typesafe_model, "questions": questions}


def categorize_batch(batch: list[dict]) -> dict:
    """Categorize up to BATCH_SIZE items in ONE request.

    Returns {"results": [...], "latency_s": float, "usage": {...}} where each
    result is {id, category, confidence, probabilities}. Raises TypeSafeError
    on failure (callers decide how to fall back)."""
    if len(batch) > BATCH_SIZE:
        raise ValueError(f"batch of {len(batch)} exceeds BATCH_SIZE={BATCH_SIZE}")
    payload = _build_payload(batch)
    t0 = time.perf_counter()
    body = _post(payload)
    latency = time.perf_counter() - t0

    answers = body.get("answers") or {}
    results = []
    for i, it in enumerate(batch):
        ans = answers.get(f"q{i}") or {}
        cat = ans.get("choice")
        if cat not in VALID_CATEGORIES:
            # Defensive: the option set IS VALID_CATEGORIES, so anything else
            # means a malformed answer. Treat as an untrusted "other".
            logger.warning(
                "TypeSafe returned unexpected category %r for item %r", cat, it.get("id")
            )
            cat, conf = "other", 0.0
        else:
            conf = float(ans.get("confidence") or 0.0)
        results.append(
            {
                "id": it.get("id"),
                "category": cat,
                "confidence": conf,
                "probabilities": ans.get("probabilities") or {},
            }
        )
    return {
        "results": results,
        "latency_s": latency,
        "usage": body.get("usage") or {},
    }


def categorize(items: list[dict]) -> list[dict]:
    """Categorize any number of items, chunking into BATCH_SIZE requests.

    Each item: {id, merchant_or_vendor, amount, currency, date, items?}.
    Returns one {id, category, confidence, probabilities} per input, in
    order. Raises TypeSafeError if any chunk fails."""
    out: list[dict] = []
    for chunk in _chunks(items, BATCH_SIZE):
        out.extend(categorize_batch(chunk)["results"])
    return out


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def is_enabled() -> bool:
    return (settings.category_backend or "keyword").strip().lower() == "typesafe"


def pick_or_fallback(result: Optional[dict], fallback: str, *, label: str = "") -> str:
    """Apply the confidence policy: use TypeSafe's category when confidence
    clears `settings.typesafe_min_confidence`, else the keyword `fallback`.
    Logs the fallback so low-confidence cases are visible."""
    if not result:
        return fallback
    thr = settings.typesafe_min_confidence
    if result["confidence"] >= thr:
        return result["category"]
    logger.info(
        "typesafe low confidence %.2f < %.2f for %s → keyword fallback %r (typesafe said %r)",
        result["confidence"],
        thr,
        label or result.get("id"),
        fallback,
        result["category"],
    )
    return fallback
