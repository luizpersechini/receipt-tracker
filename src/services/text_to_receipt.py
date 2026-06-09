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
from . import ocr_settings

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
- total_amount IS THE GRAND TOTAL the customer owed — the final amount after
  tax/service, in ANY language. It is a "total"-type amount, NEVER a line-item
  price, a quantity, a subtotal, or a tax line.
  Priority order when reading the text:
    1. A card-transaction line: "VENTA", "CHARGED", "AMOUNT", "Total CAD$",
       "TOTAL CAD", "Card", or the amount next to a masked card number
       (xxxxxxxx2928 etc.). This is the truth source.
    2. Otherwise a line labeled total / grand total / amount due / amount to
       pay, IN ANY LANGUAGE: "Total", "Grand Total", "Valor Total", "Total a
       Pagar", "Importe", "Totale", "Jumlah", "Total Bayar", "合計". If several
       total-type amounts appear (subtotal, tax, total), take the LARGEST —
       that is the grand total.
    3. If a "Total" line is followed by Tip/Propina/Service charge/Surcharge
       lines, the REAL total is "Total + those extras"; return the printed sum
       if present, otherwise the Total plus fill tip_amount/service_charge.
  NEVER return a "cash"/"tunai"/"paid"/"tendered" amount or a
  "change"/"kembali"/"troco" amount as total_amount — those are the money
  handed over and given back, not the bill. If both are shown, total = cash −
  change. total_amount is NEVER the subtotal; between Subtotal and Total pick
  Total (the larger).

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

Worked example — note how total_amount is the card charge (Total + Tip),
items exclude subtotal/tax/total/tip, and currency/country come from the city:
INPUT:
  TREES COFFEE
  Gastown, Vancouver BC
  Latte           4.50
  Muffin          3.25
  Subtotal        7.75
  GST             0.39
  Total           8.14
  Tip             1.50
  VISA ****2928   9.64
  2026-03-09
OUTPUT:
  {"language":"en","date":"2026-03-09","total_amount":"9.64","currency":"CAD",
   "vendor_name":"Trees Coffee","vendor_address":"Gastown, Vancouver BC",
   "city":"Vancouver","state":"BC","country":"CA",
   "items":[{"description":"Latte","amount":"4.50"},{"description":"Muffin","amount":"3.25"}],
   "tax_amount":"0.39","tip_amount":"1.50","service_charge":null,
   "payment_method":"VISA ****2928","category":"meals"}

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
        # Cap context so Ollama doesn't allocate the model's full default
        # window (128K → 20GB+ KV cache). See settings.ocr_num_ctx.
        "options": {"num_ctx": settings.ocr_num_ctx},
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

    model = model or ocr_settings.effective_text_model()
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
