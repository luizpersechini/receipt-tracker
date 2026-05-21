"""
Structure raw OCR text into receipt-metadata JSON using a small text-only
LLM via Ollama.

Pairs with `apple_vision_ocr.py` to provide the OCR-then-structure pipeline:
the vision step is handled by Apple Vision (fast + accurate at printed
text), and this module takes the resulting text blob and maps it to the
same metadata schema the vision-LLM path emits.

Why text-only is the right choice here:
- The hard part (OCR) is already done — we just need NLP, not a vision
  encoder. That eliminates 95% of the per-call latency and the RAM
  pressure that comes with vision-LMs (4–5 GB peak for llava/moondream).
- Text LLMs at the 2-3B parameter range (llama3.2:3b ~2 GB, qwen2.5:3b
  ~3 GB) are very capable at JSON structuring tasks — they don't need
  to also figure out what the receipt SAYS, just how to lay it out.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

from ..config import settings

logger = logging.getLogger(__name__)


OLLAMA_HOST_DEFAULT = "http://localhost:11434"


PROMPT = """You are a receipt-data structurer. The user pasted the raw text of ONE
receipt that was already OCR'd by an external tool. Extract the structured
fields and return ONLY a valid JSON object — no markdown, no commentary.

Return exactly this shape:
{
  "language": "<ISO 639-1, e.g. pt|en|es|fr>",
  "date": "<date exactly as printed, e.g. 30/04/2026 or 2026-04-30, or null if not present>",
  "total_amount": "<the FINAL AMOUNT THE CUSTOMER PAID — see rules below>",
  "currency": "<ISO 4217, e.g. BRL|USD|CAD|EUR>",
  "vendor_name": "<business name as printed, or null if not legible>",
  "vendor_address": "<string or null>",
  "city": "<city/town name only, e.g. Vancouver — NOT a person name, table, or month>",
  "state": "<state/province abbreviation, e.g. BC, SP — or null>",
  "country": "<ISO 3166-1 alpha-2, e.g. CA|BR|US — derive from city/address if absent>",
  "items": [{"description": "<item>", "amount": "<as printed>"}],
  "tax_amount": "<as printed or null>",
  "tip_amount": "<as printed or null>",
  "service_charge": "<as printed or null>",
  "payment_method": "<string or null>",
  "category": "<meals|travel|transportation|lodging|office_supplies|other>"
}

Rules:
- total_amount IS THE FINAL AMOUNT CHARGED TO THE CARD (or paid in cash).
  Priority order when reading the text:
    1. Lines tagged with the card transaction: "VENTA", "CHARGED", "AMOUNT",
       "Total CAD$", "TOTAL CAD", "Card", or the largest amount adjacent to
       a masked card number (xxxxxxxx2928 etc.). This is the truth source.
    2. Otherwise a line literally labeled "Total" / "Grand Total" / "Valor
       Total" — ONLY IF there is no separate Tip/Propina/Service charge/
       Credit card surcharge line below it that adds value.
    3. If a "Total" line is followed by a Tip/Propina/Service charge/
       Surcharge line, the REAL total is "Total + those extras". If the
       receipt prints the sum on a later line, return that sum. Otherwise
       return the printed Total but also fill tip_amount and service_charge
       so downstream code can reconcile.
  total_amount is NEVER the subtotal. If the only numbers visible are
  Subtotal and Total, pick the larger.

- date: return ONLY the transaction date physically printed on the receipt.
  NEVER use today's date. NEVER fabricate. If unclear, return null.

- vendor_name: the business name (e.g. "Trees Coffee Gastown",
  "Ciclo Espresso"). NOT a server name, table number, transaction id,
  or "Unknown". If you cannot identify the business, return null.

- city / state / country: ONLY real geographic place names. Server names
  ("Karamtan", "Cafe Tech P"), table numbers, month abbreviations
  (Mar/Apr/etc.), and employee IDs are NOT cities — return null. If unsure,
  null rather than guess.

- Amounts EXACTLY as printed — preserve commas/periods ("41,78" not 4178)

- currency: ISO code only (BRL not R$, CAD not CA$, USD not $).
  Derive from the country/city when the symbol is ambiguous: a bare "$"
  on a Vancouver receipt is CAD, on a New York receipt is USD.

- country: prefer the country implied by city/address over the currency
  symbol. Vancouver/Toronto/Montreal → CA; São Paulo/Rio → BR.

- items: ONLY purchased products/services, NOT totals/discounts/taxes/
  service charge rows.

- If a field cannot be read with confidence, return null. NEVER fabricate
  a value just to fill a slot.

- The OCR text may have noise (extra characters, broken lines); recover
  the real values where possible, but if you can't, prefer null.

Here is the receipt text:
---
{text}
---
Return ONLY the JSON object."""


def _ollama_chat(model: str, prompt: str) -> dict:
    """Call Ollama's /api/chat with a text-only prompt. Hard timeout."""
    host = (settings.ollama_host or OLLAMA_HOST_DEFAULT).rstrip("/")
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": "10m",
        "messages": [{"role": "user", "content": prompt}],
        # Encourage JSON output via Ollama's format hint when available.
        "format": "json",
    }
    resp = requests.post(
        f"{host}/api/chat",
        json=payload,
        timeout=settings.ocr_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


def structure_text_as_receipt(text: str, *, model: Optional[str] = None) -> dict:
    """
    Run the structuring step. `model` defaults to settings.ocr_text_model.
    Returns a metadata dict. Includes an "error" field if the LLM call or
    JSON parse failed — caller can decide to keep or discard.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "no OCR text to structure"}

    model = model or settings.ocr_text_model
    prompt = PROMPT.replace("{text}", text)
    try:
        response = _ollama_chat(model, prompt)
    except requests.Timeout:
        return {"error": f"Ollama text-model timed out after {settings.ocr_timeout_seconds}s"}
    except requests.RequestException as e:
        return {"error": f"Ollama text-model request failed: {e}"}

    raw = response.get("message", {}).get("content", "").strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    # Most models honour format=json, but be defensive about trailing text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Find the outermost {...} block in the response
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    return {"error": f"JSON parse failed. Raw: {raw[:300]}"}
