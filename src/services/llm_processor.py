"""
LLM Processor for receipt analysis using Ollama.
Uses the configured vision model with fallback to other available vision models.
Handles HEIC images via pillow-heif.
"""

import base64
import io
import json
import logging
import re
from typing import Optional

import ollama
import requests
from PIL import Image

from ..config import settings

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

logger = logging.getLogger(__name__)


class OCRConfigError(RuntimeError):
    """Raised when no safe vision model is available."""


# Curated allowlist of small vision-capable models. A model is "safe" if its
# name (or family prefix) appears here. The configured `OLLAMA_MODEL` is
# always allowed regardless of this list, but the size cap still applies.
KNOWN_VISION_MODELS = {
    "glm-ocr",
    "llava",
    "llava-llama3",
    "llava-phi3",
    "bakllava",
    "moondream",
    "minicpm-v",
    "qwen2-vl",
    "qwen2.5-vl",
    "llama3.2-vision",
    "gemma3",  # Gemma 3 multimodal variants
    "gemma4",  # explicitly opt-in (still subject to size cap)
}

GB = 1024**3


def _model_family(model_name: str) -> str:
    """`glm-ocr:latest` → `glm-ocr`; `llava:7b` → `llava`."""
    return model_name.split(":", 1)[0].lower()


def _is_vision_allowlisted(model_name: str) -> bool:
    family = _model_family(model_name)
    return family in KNOWN_VISION_MODELS


def _list_available_models() -> dict:
    """Return {name: size_bytes} for installed models. Empty dict on failure."""
    try:
        resp = ollama.list()
        models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
        out: dict = {}
        for m in models:
            if isinstance(m, dict):
                name = m.get("name") or m.get("model")
                size = m.get("size", 0)
            else:
                name = getattr(m, "name", None) or getattr(m, "model", None)
                size = getattr(m, "size", 0)
            if name:
                out[name] = int(size or 0)
        return out
    except Exception as e:
        logger.warning("Could not list Ollama models: %s", e)
        return {}


def _pick_model(available: Optional[dict] = None) -> str:
    """
    Return a safe vision model name, or raise OCRConfigError.

    A model is acceptable iff it is installed, its size is ≤ ocr_max_model_gb,
    AND either (a) it equals the user-configured `OLLAMA_MODEL`, or
    (b) its family is in KNOWN_VISION_MODELS.
    """
    if available is None:
        available = _list_available_models()
    if not available:
        raise OCRConfigError(
            "Ollama is unreachable or has no installed models. "
            "Start `ollama serve` and `ollama pull glm-ocr`."
        )

    max_bytes = int(settings.ocr_max_model_gb * GB)
    configured = settings.ollama_model

    # 1) explicit configured model wins if installed and within size cap
    if configured in available:
        size = available[configured]
        if size <= max_bytes:
            return configured
        raise OCRConfigError(
            f"Configured model {configured} is {size / GB:.1f} GB, "
            f"over the {settings.ocr_max_model_gb} GB cap. "
            f"Pull a smaller vision model or raise OCR_MAX_MODEL_GB."
        )

    # 2) fall back to allowlisted families, smallest-first
    candidates = [
        (name, size)
        for name, size in available.items()
        if _is_vision_allowlisted(name) and size <= max_bytes
    ]
    if candidates:
        candidates.sort(key=lambda x: x[1])
        chosen = candidates[0][0]
        logger.warning(
            "Configured model %s not installed; falling back to %s",
            configured,
            chosen,
        )
        return chosen

    raise OCRConfigError(
        f"No safe vision model available. Configured `{configured}` is not "
        f"installed, and no allowlisted vision model under "
        f"{settings.ocr_max_model_gb} GB is available. Installed: "
        f"{', '.join(available) or 'none'}."
    )


# Image loading


def _resize_for_ocr(img: Image.Image, max_dim: Optional[int] = None) -> Image.Image:
    """Downsample so the longest edge is ≤ max_dim. No-op if already small."""
    if max_dim is None:
        max_dim = settings.ocr_max_image_dim
    w, h = img.size
    longest = max(w, h)
    if longest <= max_dim:
        return img
    scale = max_dim / longest
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return img.resize(new_size, Image.LANCZOS)


def _load_image_bytes(image_path: str) -> bytes:
    """
    Decode any supported format, resize to OCR_MAX_IMAGE_DIM on the longest
    edge, and return PNG bytes. PNG (lossless) is used instead of JPEG
    because Ollama's vision input parser intermittently rejects valid
    JPEG outputs from Pillow with "invalid JPEG format: missing 0xff00
    sequence" depending on Huffman-table state. PNG round-trips cleanly.

    Keeping the image dimensions small is still the main safeguard against
    OCR thrashing — the encoding choice doesn't affect that.
    """
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return _image_to_ocr_bytes(img)


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Lift image quality before the OCR call. Three cheap operations that
    measurably improve any vision model's accuracy on phone-photo receipts:

      1. Respect the EXIF orientation tag — phone photos are often stored
         in their sensor orientation with a "rotate 90°" hint. Without
         this, a portrait receipt arrives sideways at the model.
      2. CLAHE on the luminance channel — local contrast normalisation
         that handles unevenly-lit photos (one side bright, other dim).
      3. Mild unsharp mask — sharpens printed text edges that get soft
         after our resize step.

    All three are universal wins; we still wrap them in a try/except so a
    weird input format doesn't break the whole OCR call.
    """
    if not settings.ocr_preprocess:
        return img
    try:
        from PIL import ImageOps, ImageFilter
        import cv2  # already a dep via the segmenter
        import numpy as np

        # 1. EXIF orientation
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # 2. CLAHE on the L channel of LAB colour space
        arr = np.array(img)
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        l2 = clahe.apply(l)
        arr2 = cv2.cvtColor(cv2.merge((l2, a, b)), cv2.COLOR_LAB2RGB)
        img = Image.fromarray(arr2)

        # 3. Unsharp mask — radius 1.5, percent 80, threshold 3.
        # Gentle settings: enough to crisp 10pt receipt text without
        # introducing ringing artefacts.
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=3))
        return img
    except Exception as e:
        logger.warning("OCR preprocess failed, falling back to original: %s", e)
        return img


def _image_to_ocr_bytes(img: Image.Image) -> bytes:
    """Resize + encode a PIL image for Ollama. Called for both the whole-image
    path and each segmented receipt crop. Pre-processing (rotation, contrast,
    sharpening) runs first when settings.ocr_preprocess is True."""
    img = _preprocess_for_ocr(img)
    img = _resize_for_ocr(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# JSON repair


def _repair_json(text: str) -> str:
    """Best-effort repair for malformed JSON: bare keys, trailing commas, single quotes."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return text
    text = text[start:end]
    text = re.sub(r'(?<!["\w])(\w[\w\s]*)(?=\s*:)', lambda m: f'"{m.group(1).strip()}"', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = text.replace("'", '"')
    return text


# Amount / locale handling

_COMMA_DECIMAL_LANGUAGES = {
    "portuguese",
    "pt",
    "pt-br",
    "pt-pt",
    "spanish",
    "es",
    "french",
    "fr",
    "german",
    "de",
    "italian",
    "it",
    "dutch",
    "nl",
    "norwegian",
    "no",
    "swedish",
    "sv",
    "danish",
    "da",
    "finnish",
    "fi",
    "polish",
    "pl",
    "russian",
    "ru",
}
_COMMA_DECIMAL_CURRENCIES = {"BRL", "EUR", "ARS", "CLP", "COP", "PEN", "UYU"}


def _uses_comma_decimal(language: Optional[str], currency: Optional[str]) -> bool:
    lang = (language or "").lower().strip()
    cur = (currency or "").upper().strip()
    return lang in _COMMA_DECIMAL_LANGUAGES or cur in _COMMA_DECIMAL_CURRENCIES


def _parse_amount(val, comma_decimal: bool) -> Optional[float]:
    if val is None:
        return None

    s = str(val).strip()
    s = re.sub(r"[R$€£¥\s]", "", s)
    if not s:
        return None

    if comma_decimal:
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
    else:
        if "," in s and "." in s:
            s = s.replace(",", "")
        elif "," in s and "." not in s:
            s = s.replace(",", ".")

    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


# Currency normalisation

_CURRENCY_MAP = {
    "r$": "BRL",
    "brl": "BRL",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "a$": "AUD",
    "aud": "AUD",
    "c$": "CAD",
    "cad": "CAD",
    "mxn": "MXN",
    "ars": "ARS",
    "clp": "CLP",
    "cop": "COP",
}


def _normalize_currency(raw) -> Optional[str]:
    if not raw:
        return None
    key = str(raw).strip().lower()
    return _CURRENCY_MAP.get(key, str(raw).upper().strip())


# ISO 3166-1 alpha-2 → ISO 4217 currency code. Used when the receipt text
# itself doesn't disambiguate (e.g. a bare "$" on a Canadian receipt).
_COUNTRY_TO_CURRENCY = {
    "BR": "BRL",
    "US": "USD",
    "CA": "CAD",
    "MX": "MXN",
    "AR": "ARS",
    "CL": "CLP",
    "CO": "COP",
    "PE": "PEN",
    "UY": "UYU",
    "GB": "GBP",
    "JP": "JPY",
    "CN": "CNY",
    "HK": "HKD",
    "TW": "TWD",
    "AU": "AUD",
    "NZ": "NZD",
    "SG": "SGD",
    "KR": "KRW",
    "CH": "CHF",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
    "IS": "ISK",
    "PL": "PLN",
    "CZ": "CZK",
    "HU": "HUF",
    "RO": "RON",
    "TR": "TRY",
    "RU": "RUB",
    "IN": "INR",
    "TH": "THB",
    "ID": "IDR",
    "PH": "PHP",
    "MY": "MYR",
    "VN": "VND",
    "ZA": "ZAR",
    "EG": "EGP",
    "AE": "AED",
    "SA": "SAR",
    "IL": "ILS",
    # Eurozone
    "AT": "EUR",
    "BE": "EUR",
    "CY": "EUR",
    "EE": "EUR",
    "FI": "EUR",
    "FR": "EUR",
    "DE": "EUR",
    "GR": "EUR",
    "IE": "EUR",
    "IT": "EUR",
    "LV": "EUR",
    "LT": "EUR",
    "LU": "EUR",
    "MT": "EUR",
    "NL": "EUR",
    "PT": "EUR",
    "SK": "EUR",
    "SI": "EUR",
    "ES": "EUR",
    "HR": "EUR",
}


# Common full-name → ISO-2 aliases. The model is asked for ISO-2 in the
# prompt but doesn't always comply (it sometimes returns "Canada", "Brasil",
# "U.S.A.", etc.), so we normalise before currency refinement.
_COUNTRY_ALIASES = {
    "BRAZIL": "BR",
    "BRASIL": "BR",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "USA": "US",
    "U.S.A.": "US",
    "U.S.": "US",
    "AMERICA": "US",
    "CANADA": "CA",
    "MEXICO": "MX",
    "MÉXICO": "MX",
    "UNITED KINGDOM": "GB",
    "UK": "GB",
    "GREAT BRITAIN": "GB",
    "ENGLAND": "GB",
    "SCOTLAND": "GB",
    "WALES": "GB",
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "FRANCE": "FR",
    "SPAIN": "ES",
    "ESPAÑA": "ES",
    "ESPANA": "ES",
    "PORTUGAL": "PT",
    "ITALY": "IT",
    "ITALIA": "IT",
    "NETHERLANDS": "NL",
    "HOLLAND": "NL",
    "BELGIUM": "BE",
    "AUSTRIA": "AT",
    "SWITZERLAND": "CH",
    "SWEDEN": "SE",
    "NORWAY": "NO",
    "DENMARK": "DK",
    "FINLAND": "FI",
    "IRELAND": "IE",
    "POLAND": "PL",
    "CZECHIA": "CZ",
    "CZECH REPUBLIC": "CZ",
    "GREECE": "GR",
    "HUNGARY": "HU",
    "ROMANIA": "RO",
    "CROATIA": "HR",
    "TURKEY": "TR",
    "RUSSIA": "RU",
    "JAPAN": "JP",
    "CHINA": "CN",
    "HONG KONG": "HK",
    "TAIWAN": "TW",
    "KOREA": "KR",
    "SOUTH KOREA": "KR",
    "INDIA": "IN",
    "THAILAND": "TH",
    "INDONESIA": "ID",
    "PHILIPPINES": "PH",
    "MALAYSIA": "MY",
    "VIETNAM": "VN",
    "SINGAPORE": "SG",
    "AUSTRALIA": "AU",
    "NEW ZEALAND": "NZ",
    "ARGENTINA": "AR",
    "CHILE": "CL",
    "COLOMBIA": "CO",
    "PERU": "PE",
    "PERÚ": "PE",
    "URUGUAY": "UY",
    "SOUTH AFRICA": "ZA",
    "EGYPT": "EG",
    "UNITED ARAB EMIRATES": "AE",
    "UAE": "AE",
    "SAUDI ARABIA": "SA",
    "ISRAEL": "IL",
}


# When the model returns no `country` AND no usable `state`/subdivision,
# we look at the `city` field as a last resort. Keys are lowercase, accent-
# stripped. Only well-known unambiguous city names — skip "Springfield" etc.
_CITY_TO_COUNTRY = {
    # Canada
    "vancouver": "CA",
    "toronto": "CA",
    "montreal": "CA",
    "calgary": "CA",
    "edmonton": "CA",
    "ottawa": "CA",
    "winnipeg": "CA",
    "halifax": "CA",
    "victoria": "CA",
    "quebec city": "CA",
    "saskatoon": "CA",
    "regina": "CA",
    "whistler": "CA",
    "banff": "CA",
    "kelowna": "CA",
    # Brazil
    "sao paulo": "BR",
    "rio de janeiro": "BR",
    "belo horizonte": "BR",
    "brasilia": "BR",
    "salvador": "BR",
    "fortaleza": "BR",
    "curitiba": "BR",
    "porto alegre": "BR",
    "manaus": "BR",
    "recife": "BR",
    "goiania": "BR",
    "campinas": "BR",
    "florianopolis": "BR",
    "vitoria": "BR",
    "natal": "BR",
    "macae": "BR",
    "santos": "BR",
    "niteroi": "BR",
    # United States
    "new york": "US",
    "los angeles": "US",
    "san francisco": "US",
    "chicago": "US",
    "miami": "US",
    "seattle": "US",
    "boston": "US",
    "austin": "US",
    "denver": "US",
    "atlanta": "US",
    "washington": "US",
    "portland": "US",
    "dallas": "US",
    "houston": "US",
    "philadelphia": "US",
    # UK
    "london": "GB",
    "edinburgh": "GB",
    "manchester": "GB",
    "birmingham": "GB",
    "glasgow": "GB",
    "liverpool": "GB",
    "leeds": "GB",
    "oxford": "GB",
    "cambridge": "GB",
    # Eurozone
    "paris": "FR",
    "lyon": "FR",
    "marseille": "FR",
    "nice": "FR",
    "berlin": "DE",
    "munich": "DE",
    "hamburg": "DE",
    "frankfurt": "DE",
    "madrid": "ES",
    "barcelona": "ES",
    "seville": "ES",
    "rome": "IT",
    "milan": "IT",
    "florence": "IT",
    "venice": "IT",
    "amsterdam": "NL",
    "rotterdam": "NL",
    "lisbon": "PT",
    "porto": "PT",
    "dublin": "IE",
    "brussels": "BE",
    "vienna": "AT",
    "athens": "GR",
    # APAC
    "tokyo": "JP",
    "osaka": "JP",
    "kyoto": "JP",
    "singapore": "SG",
    "hong kong": "HK",
    "sydney": "AU",
    "melbourne": "AU",
    "brisbane": "AU",
    "perth": "AU",
    "auckland": "NZ",
    "wellington": "NZ",
    # LatAm
    "buenos aires": "AR",
    "mexico city": "MX",
    "ciudad de mexico": "MX",
    "santiago": "CL",
    "lima": "PE",
    "bogota": "CO",
    "montevideo": "UY",
}


def _strip_accents(s: str) -> str:
    """Lowercase + remove combining marks so 'São Paulo' matches 'sao paulo'."""
    import unicodedata

    nfd = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _country_from_city(city) -> Optional[str]:
    if not city:
        return None
    key = _strip_accents(str(city).strip())
    if key in _CITY_TO_COUNTRY:
        return _CITY_TO_COUNTRY[key]
    # Try the first token (e.g. "Vancouver, BC" → "vancouver")
    head = key.split(",", 1)[0].strip()
    return _CITY_TO_COUNTRY.get(head)


_SUBDIVISION_TO_COUNTRY = {
    # Canadian provinces / territories — when the model returns the
    # province code in the country slot ("Vancouver BC" → it picks "BC"),
    # walk back to "CA". Excluded: codes that collide with ISO-2 country
    # codes like CA (California vs Canada — too easy to misfire).
    "BC": "CA",
    "ON": "CA",
    "QC": "CA",
    "AB": "CA",
    "MB": "CA",
    "SK": "CA",
    "NB": "CA",
    "NS": "CA",
    "NL": "CA",
    "YT": "CA",
    "NU": "CA",
    "NT": "CA",
    # Brazilian states — same pattern (the user has lots of BR receipts
    # and the model sometimes returns "SP" instead of "BR"). Skipped the
    # codes that conflict with country codes (PE/Peru, ES/Spain) or US
    # states (AL/Alabama, MT/Montana, etc.).
    "SP": "BR",
    "RJ": "BR",
    "MG": "BR",
    "PR": "BR",
    "RS": "BR",
    "BA": "BR",
    "CE": "BR",
    "SC": "BR",
    "GO": "BR",
    "AM": "BR",
    "DF": "BR",
    "MA": "BR",
    "PB": "BR",
    "RN": "BR",
    "PI": "BR",
    "TO": "BR",
    "RO": "BR",
    "AC": "BR",
    "RR": "BR",
    "AP": "BR",
    "SE": "BR",
}


def _normalize_country(raw) -> Optional[str]:
    """
    Coerce whatever the model returned for `country` into an ISO 3166-1
    alpha-2 code. Accepts existing ISO-2 codes verbatim, falls back to a
    name alias table for the common cases ("Canada", "Brasil", …), and
    finally to a subdivision map for when the model returns a province /
    state code instead of the country code ("BC" → "CA").
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    # Already ISO-2? Honour it verbatim (Canada beats California for "CA")
    if len(s) == 2 and s.isalpha():
        # …unless it's a known subdivision code that's NOT a country code,
        # in which case promote it to its parent country.
        if s in _SUBDIVISION_TO_COUNTRY:
            return _SUBDIVISION_TO_COUNTRY[s]
        return s
    # Strip common decorations the model adds: ", USA", "(BR)", trailing punct
    cleaned = s.replace("(", " ").replace(")", " ").replace(",", " ").strip()
    cleaned = " ".join(cleaned.split())
    if cleaned in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[cleaned]
    # Try just the last word in case the model returned "Vancouver, Canada"
    last = cleaned.rsplit(" ", 1)[-1]
    if last in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[last]
    # Sub-tokens may include a province code embedded in a longer string
    for tok in cleaned.split():
        if tok in _SUBDIVISION_TO_COUNTRY:
            return _SUBDIVISION_TO_COUNTRY[tok]
    return None


def _refine_currency(currency: Optional[str], country: Optional[str]) -> Optional[str]:
    """
    When a confident country is available, trust it over the model-extracted
    currency. The vision model reads country names ("Vancouver", "Brazil",
    "Berlin") far more reliably than currency symbols — a receipt that says
    "Trees Coffee Gastown, Vancouver" should always be CAD, even if the
    model hallucinated BRL or USD.

    Rules:
    - No country known → keep whatever currency was extracted (may be None).
    - Country in our map → use the country's currency, overriding any
      mismatched model output. This deliberately treats `country` as the
      higher-confidence signal.
    - Country known but unmapped (rare/edge ISO codes) → keep currency.
    """
    country = (country or "").upper().strip() or None
    if not country:
        return currency

    country_currency = _COUNTRY_TO_CURRENCY.get(country)
    if not country_currency:
        return currency

    return country_currency


# Category inference

_CATEGORY_KEYWORDS = {
    "meals": [
        "restaurant",
        "gastronomia",
        "bar",
        "cafe",
        "café",
        "coffee",
        "espresso",
        "cappuccino",
        "latte",
        "tea",
        "food",
        "tapioca",
        "pizza",
        "burger",
        "sushi",
        "grill",
        "kitchen",
        "lunch",
        "dinner",
        "breakfast",
        "snack",
        "cantina",
        "bistro",
        "lanchonete",
        "padaria",
        "bakery",
        "pastry",
        "deli",
        "diner",
        "trattoria",
        "steakhouse",
    ],
    "travel": [
        "airline",
        "aerobar",
        "airport",
        "flight",
        "hotel",
        "airbnb",
        "hostel",
        "inn",
        "motel",
        "resort",
        "booking",
        "aeroporto",
    ],
    "transportation": [
        "uber",
        "lyft",
        "taxi",
        "cab",
        "metro",
        "bus",
        "train",
        "parking",
        "toll",
        "posto",
        "gas station",
        "fuel",
        "estacionamento",
        "gasolina",
    ],
    "lodging": ["hotel", "airbnb", "hostel", "inn", "motel", "resort", "pousada"],
    "office_supplies": [
        "amazon",
        "office",
        "staples",
        "print",
        "paper",
        "ink",
        "papelaria",
        "livraria",
    ],
}


def _infer_category(vendor: str, items: list) -> str:
    """Pick the first category whose keyword list hits the vendor/items text.

    We use word-boundary matching (regex \\b) so short keywords like 'gas'
    don't match 'Gastown' or 'bar' don't match 'barber'. Falls back to
    plain substring for multi-word keywords like 'gas station' because the
    space already gives an implicit boundary.
    """
    text = (
        (vendor or "").lower()
        + " "
        + " ".join((i.get("description") or "") for i in (items or [])).lower()
    )
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if " " in kw:
                if kw in text:
                    return cat
            else:
                # word-boundary so 'gas' doesn't match 'gastown', 'bar' doesn't
                # match 'barbershop', etc. Locale-aware: \w matches accented
                # chars when re.UNICODE is on (default in Py3).
                if re.search(rf"\b{re.escape(kw)}\b", text):
                    return cat
    return "other"


# Item cleaning

_SUMMARY_KEYWORDS = [
    "total",
    "valor a pagar",
    "subtotal",
    "desconto",
    "acréscimo",
    "qtde",
    "quantidade",
    "troco",
    "change",
    "tax total",
    "grand total",
    "amount due",
    "balance",
    "taxa de serviço",
    "service charge",
    "service fee",
    "taxa serviço",
]


def _is_summary_row(desc: str) -> bool:
    d = (desc or "").lower()
    return any(kw in d for kw in _SUMMARY_KEYWORDS)


def _clean_items(items, comma_decimal: bool) -> list:
    if not isinstance(items, list):
        return []
    cleaned: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or "").strip()
        if not desc or _is_summary_row(desc):
            continue
        amount = _parse_amount(item.get("amount"), comma_decimal) or 0.0
        cleaned.append({"description": desc, "amount": amount})
    return cleaned


# Deprecated: a shorter prompt for the single-receipt path. Kept here as a
# reference but no longer used — empirically, the per-field examples in the
# verbose PROMPT below give noticeably better accuracy and the speed
# difference was negligible (~2s per call).
_SINGLE_RECEIPT_PROMPT_UNUSED = """You are an expert receipt OCR system. The image shows exactly ONE receipt.
Extract its data and return ONLY a valid JSON object — no markdown, no explanation.

Return exactly this structure:
{
  "language": "<ISO 639-1, e.g. pt|en|es|fr>",
  "date": "<date exactly as printed, e.g. 30/04/2026 or 2026-04-30>",
  "total_amount": "<total exactly as printed, e.g. 41,78 or 99.99>",
  "currency": "<ISO 4217, e.g. BRL|USD|CAD|EUR>",
  "vendor_name": "<string>",
  "vendor_address": "<string or null>",
  "city": "<city or null>",
  "state": "<state/province code or name, or null>",
  "country": "<ISO 3166-1 alpha-2, e.g. CA|BR|US — derive from city/address if unsure>",
  "items": [{"description": "<item>", "amount": "<as printed>"}],
  "tax_amount": "<as printed or null>",
  "tip_amount": "<as printed or null>",
  "service_charge": "<as printed or null>",
  "payment_method": "<string or null>",
  "category": "<meals|travel|transportation|lodging|office_supplies|other>"
}

Rules:
- Amounts EXACTLY as printed (preserve "41,78" not 4178)
- date EXACTLY as printed; don't reformat
- currency: ISO code only (BRL not R$)
- country: prefer the country implied by city/address over the currency symbol
  (Vancouver/Toronto → CA; São Paulo/Rio → BR; "$" alone doesn't mean USD)
- items: ONLY products/services, NOT totals/discounts/taxes
- absent fields: null"""


PROMPT = """You are an expert receipt OCR system. The image may contain ONE OR MORE
distinct receipts (a phone photo of a single receipt, a scan of multiple receipts
placed side-by-side, or a multi-page scan). Identify every separate receipt and
return ONLY a valid JSON object — no markdown, no explanation.

Return exactly this structure:
{
  "receipts": [
    {
      "language": "<ISO 639-1 language code of the receipt, e.g. pt, en, es, fr>",
      "date": "<date exactly as printed on the receipt, e.g. 30/04/2026 or 2026-04-30, or null if not legible>",
      "total_amount": "<the FINAL AMOUNT THE CUSTOMER PAID — see rules below>",
      "currency": "<ISO 4217 code, e.g. BRL, USD, EUR>",
      "vendor_name": "<business name as printed, or null if not legible>",
      "vendor_address": "<string or null>",
      "city": "<city/town name only, e.g. Vancouver — NOT a person name, table, or code>",
      "state": "<state/province abbreviation, e.g. BC, SP, CA — or null>",
      "country": "<ISO 3166-1 alpha-2 country code, e.g. BR, US, CA, or null>",
      "items": [{"description": "<item name>", "amount": "<amount exactly as printed>"}],
      "tax_amount": "<amount as printed or null>",
      "tip_amount": "<amount as printed or null>",
      "service_charge": "<amount as printed or null>",
      "payment_method": "<string or null>",
      "category": "<one of: meals, travel, transportation, lodging, office_supplies, other>"
    }
  ]
}

Critical rules:
- Wrap the result in {"receipts": [...]}, even for a single receipt — one element in the array.
- Each receipt is from a DIFFERENT vendor or has a DIFFERENT total. Don't split a multi-page receipt of the same purchase into multiple entries.

- total_amount IS THE FINAL AMOUNT CHARGED TO THE CUSTOMER'S CARD (or paid in cash). Look for, in priority order:
    1. The line tagged with the actual card transaction: "VENTA", "CHARGED", "AMOUNT", "Total CAD$", "TOTAL CAD", "Card", or the largest amount adjacent to the masked card number.
    2. Otherwise the line literally labeled "Total" or "Grand Total" or "Valor Total" — BUT ONLY IF there is no separate tip / surcharge / propina line below it adding extra value.
    3. If a "Total" line is followed by a "Tip"/"Propina"/"Service charge"/"Credit card surcharge" line, the REAL total is "Total + those extras" — return the sum as printed if a sum line exists, otherwise return the printed Total but also fill tip_amount and service_charge so downstream can reconcile.
  total_amount is NEVER the subtotal (the line before tax). If the only numbers you can see are "Subtotal" and "Total", pick the larger.

- date: return ONLY the date that is physically printed on the receipt as the transaction date. NEVER use today's date. NEVER fabricate. If the receipt has no clearly readable date, return null.

- vendor_name: the business name as printed (e.g. "Trees Coffee Gastown", "Ciclo Espresso"). NOT a server name, a table number, a transaction id, or "Unknown". If you cannot read the business name, return null.

- city / state / country: ONLY real geographic place names found in the receipt's address block. Server names, table numbers, month abbreviations (Mar/Apr/etc.), employee names, and transaction codes are NOT cities — they go to null. If unsure, return null rather than guess.

- Return amounts EXACTLY as printed — preserve commas and periods (e.g. "41,78" not 4178). Do NOT reformat.

- currency: ISO code only (BRL not R$, USD not $, CAD not C$)

- items: ONLY purchased products/services, NOT totals/discounts/taxes/service charge rows

- If a field cannot be read with confidence, return null. NEVER fabricate a value to fill a field."""


OLLAMA_HOST_DEFAULT = "http://localhost:11434"


def warmup() -> Optional[str]:
    """
    Preload the model the active OCR backend will use, so the first user
    upload doesn't pay the cold-start cost. Returns the model name that got
    warmed, or None if warmup was skipped or failed.

    - backend=ollama_vision → warms the chosen vision model (glm-ocr/llava)
    - backend=apple_vision  → warms the text-only structuring model
      (settings.ocr_text_model). Apple Vision itself has no warmup since
      it's a system framework, not an Ollama model.
    """
    if settings.ocr_backend == "apple_vision":
        model = settings.ocr_text_model
    else:
        try:
            model = _pick_model()
        except OCRConfigError as e:
            logger.warning("OCR warmup skipped: %s", e)
            return None

    host = (settings.ollama_host or OLLAMA_HOST_DEFAULT).rstrip("/")
    payload = {
        "model": model,
        "prompt": "",  # empty prompt = pure model load, no generation
        "stream": False,
        "keep_alive": "10m",
    }
    try:
        # Short timeout for the warmup call itself — if Ollama isn't reachable
        # or loading is pathologically slow, don't block server start.
        resp = requests.post(f"{host}/api/generate", json=payload, timeout=60)
        resp.raise_for_status()
        logger.info("OCR warmup complete: %s (backend=%s)", model, settings.ocr_backend)
        return model
    except requests.RequestException as e:
        logger.warning("OCR warmup failed for %s: %s", model, e)
        return None


def _ollama_chat(model: str, prompt: str, image_bytes: bytes) -> dict:
    """Call Ollama's chat API with a hard timeout. Raises on transport errors."""
    host = (settings.ollama_host or OLLAMA_HOST_DEFAULT).rstrip("/")
    payload = {
        "model": model,
        "stream": False,
        # Keep the model resident for 10 minutes so the next upload in the
        # same session doesn't have to reload the weights.
        "keep_alive": "10m",
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image_bytes).decode("ascii")],
            }
        ],
    }
    resp = requests.post(
        f"{host}/api/chat",
        json=payload,
        timeout=settings.ocr_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


def _empty_receipt() -> dict:
    return {
        "language": None,
        "date": None,
        "total_amount": None,
        "currency": None,
        "vendor_name": "Unknown",
        "vendor_address": None,
        "items": [],
        "tax_amount": None,
        "tip_amount": None,
        "service_charge": None,
        "payment_method": None,
        "category": "other",
    }


def _normalize_one_receipt(data: dict) -> dict:
    """Clean and normalise a single receipt dict produced by the LLM.

    Same pipeline that the single-receipt code used: currency normalisation,
    country → ISO-2, locale-aware amount parsing, item cleaning, category
    fallback, vendor scrub, payment-method sanitation, and tip/service
    sanity checks. Extracted as its own function so it can be applied per
    element of a multi-receipt response.
    """
    data["currency"] = _normalize_currency(data.get("currency"))
    # Coerce country to ISO-2 — the model often returns the full name
    # ("Canada", "Brasil", …) despite the prompt asking for ISO-2.
    data["country"] = _normalize_country(data.get("country"))
    # Fallback: if country is still missing, try inferring it from the
    # `state`/subdivision the model returned (e.g. "BC" → "CA"), then
    # finally from the city name ("Vancouver" → "CA"). This is what saves
    # us when the model leaves country empty but clearly knows the city.
    if not data["country"]:
        data["country"] = _normalize_country(data.get("state"))
    if not data["country"]:
        data["country"] = _country_from_city(data.get("city"))
    # Disambiguate currency with country once we have both — fixes the
    # "Canadian receipt shows USD because of bare $" case.
    data["currency"] = _refine_currency(data["currency"], data.get("country"))
    comma_dec = _uses_comma_decimal(data.get("language"), data.get("currency"))

    # Strip clock time from the date field — receipts often print
    # "10-Mar-2026 1:44:53p.m." or "2026-03-10 10:29 a.m." and we only
    # want the calendar date downstream.
    _date_clean = _strip_time_from_date(data.get("date"))
    # Then normalise to ISO YYYY-MM-DD using the receipt's language/country
    # to disambiguate D/M vs M/D. Falls back to the cleaned-but-not-ISO
    # string if parsing fails, so we never silently drop the printed date.
    data["date"] = (
        _normalize_date(_date_clean, language=data.get("language"), country=data.get("country"))
        or _date_clean
    )

    for field in ("total_amount", "tax_amount", "tip_amount", "service_charge"):
        data[field] = _parse_amount(data.get(field), comma_dec)

    data["items"] = _clean_items(data.get("items"), comma_dec)

    cat = str(data.get("category") or "")
    if "/" in cat or cat not in (
        "meals",
        "travel",
        "transportation",
        "lodging",
        "office_supplies",
        "other",
    ):
        data["category"] = _infer_category(data.get("vendor_name", ""), data.get("items", []))

    vendor = data.get("vendor_name") or ""
    vendor = re.sub(
        r"(?i)^(cnpj|cpf|vat|ein|tax id|nif|rfc)\s*[:\-#]?\s*[\d.\-/]+\s*",
        "",
        vendor,
    ).strip()
    data["vendor_name"] = vendor or "Unknown"

    _PM_LABELS = ("forma de pagamento", "valor pago", "payment method", "método de pago")
    pm = data.get("payment_method") or ""
    if any(lbl in pm.lower() for lbl in _PM_LABELS):
        data["payment_method"] = None

    # Sanity-check tip/service_charge: discard if >= total, sub-cent, or a
    # small integer that looks like a misread item-count row.
    total = data.get("total_amount") or 0
    for field in ("tip_amount", "service_charge"):
        val = data.get(field)
        if val is not None and val > 0:
            if val < 0.5 or (total > 0 and val >= total):
                data[field] = None
            elif val == int(val) and val < 10 and total > 100:
                data[field] = None

    return data


def _ocr_one_crop(model: str, image_bytes: bytes, prompt: Optional[str] = None) -> list:
    """Run OCR on a single image-bytes payload. Returns a list of normalised
    receipt dicts (usually 1 for a single-receipt crop, but the model may
    still return >1 if it sees multiple receipts inside one segment).
    Returns `[empty_with_error]` on failure.

    `prompt` defaults to the multi-receipt-aware PROMPT. Callers that know
    the image contains exactly one receipt (e.g. the bbox-editor commit
    path) should pass SINGLE_RECEIPT_PROMPT — fewer prompt tokens means
    faster per-call latency."""

    def _fail(msg: str) -> list:
        empty = _empty_receipt()
        empty["error"] = msg
        return [empty]

    try:
        response = _ollama_chat(model, prompt or PROMPT, image_bytes)
    except requests.Timeout:
        return _fail(f"Ollama timed out after {settings.ocr_timeout_seconds}s (model={model})")
    except requests.RequestException as e:
        return _fail(f"Ollama request failed: {e}")

    raw = response["message"]["content"].strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    parsed = None
    for attempt in [raw, _repair_json(raw)]:
        try:
            parsed = json.loads(attempt)
            break
        except json.JSONDecodeError:
            continue
    if parsed is None:
        return _fail(f"JSON parse failed. Raw: {raw[:300]}")

    if isinstance(parsed, dict) and isinstance(parsed.get("receipts"), list):
        raw_receipts = parsed["receipts"]
    elif isinstance(parsed, list):
        raw_receipts = parsed
    elif isinstance(parsed, dict):
        raw_receipts = [parsed]
    else:
        return _fail(f"Unexpected JSON shape: {type(parsed).__name__}")

    if not raw_receipts:
        return _fail("Model returned an empty receipts array")

    cleaned = [_normalize_one_receipt(r) for r in raw_receipts if isinstance(r, dict)]
    return cleaned or _fail("Model returned no usable receipt objects")


def process_with_segments(image_path: str, *, skip_segmentation: bool = False) -> list:
    """
    Process a receipt image and return a list of dicts:
        [{"metadata": <receipt_dict>, "crop": <PIL.Image | None>}, ...]

    `crop` is the PIL image of the SEGMENT this receipt came from, OR None
    if there's only one segment (the whole image — caller should use the
    original file). The upload router uses this to save per-receipt crops
    to disk so each row's thumbnail shows the right portion of the page.

    Backend is chosen by `settings.ocr_backend`:
    - "ollama_vision" (default): single Ollama vision call per segment.
    - "apple_vision":             Apple Vision OCR (text) + small text LLM
                                  (structuring). Fast on Apple Silicon and
                                  far lower RAM pressure than vision-LMs.

    `skip_segmentation=True` bypasses the multi-receipt detector and treats
    the image as a single full-page receipt. Used for PDF pages, which are
    almost always digital documents with one receipt per page — running the
    segmenter on them tends to shred a single receipt into many fragments
    because of whitespace between text blocks.
    """

    def _fail(msg: str) -> list:
        empty = _empty_receipt()
        empty["error"] = msg
        return [{"metadata": empty, "crop": None}]

    try:
        if skip_segmentation:
            from .receipt_segmenter import _open_as_rgb

            segments = [_open_as_rgb(image_path)]
        else:
            from .receipt_segmenter import segment_receipts

            segments = segment_receipts(image_path)
        is_multi = len(segments) > 1
        backend = settings.ocr_backend
        logger.info(
            "OCR: backend=%s segments=%d skip_seg=%s path=%s",
            backend,
            len(segments),
            skip_segmentation,
            image_path,
        )

        results: list = []
        if backend == "apple_vision":
            results = _process_via_apple_vision(segments, is_multi)
        else:
            results = _process_via_ollama_vision(segments, is_multi)

        # Drop errored entries when at least one segment succeeded so the
        # user gets the receipts we DID parse without a phantom error row.
        non_error = [r for r in results if not r["metadata"].get("error")]
        if non_error:
            return non_error
        return results or _fail("No receipts extracted")

    except Exception as e:
        logger.error("OCR pipeline failed for %s: %s", image_path, e)
        return _fail(str(e))


def _process_via_ollama_vision(segments: list, is_multi: bool) -> list:
    """Original path: each segment becomes one Ollama vision-model call."""

    def _fail(msg: str) -> list:
        empty = _empty_receipt()
        empty["error"] = msg
        return [{"metadata": empty, "crop": None}]

    try:
        model = _pick_model()
    except OCRConfigError as e:
        logger.error("OCR config error: %s", e)
        return _fail(str(e))

    results: list = []
    for i, img in enumerate(segments, 1):
        image_bytes = _image_to_ocr_bytes(img)
        logger.info(
            "OCR (ollama_vision): segment %d/%d image_bytes=%d", i, len(segments), len(image_bytes)
        )
        for r in _ocr_one_crop(model, image_bytes):
            results.append({"metadata": r, "crop": img if is_multi else None})
    return results


def _process_via_apple_vision(segments: list, is_multi: bool) -> list:
    """Apple Vision text extraction + small text-LLM structuring."""
    from . import apple_vision_ocr
    from .text_to_receipt import structure_text_as_receipt

    def _fail(msg: str) -> list:
        empty = _empty_receipt()
        empty["error"] = msg
        return [{"metadata": empty, "crop": None}]

    if not apple_vision_ocr.is_available():
        return _fail(f"Apple Vision OCR not available: {apple_vision_ocr.unavailability_reason()}")

    results: list = []
    for i, img in enumerate(segments, 1):
        try:
            text = apple_vision_ocr.extract_text_from_pil(img)
        except Exception as e:
            logger.warning("Apple Vision OCR failed on segment %d: %s", i, e)
            empty = _empty_receipt()
            empty["error"] = f"Apple Vision OCR failed: {e}"
            results.append({"metadata": empty, "crop": img if is_multi else None})
            continue

        logger.info("OCR (apple_vision): segment %d/%d text_chars=%d", i, len(segments), len(text))
        if not text.strip():
            empty = _empty_receipt()
            empty["error"] = "No text detected"
            results.append({"metadata": empty, "crop": img if is_multi else None})
            continue

        raw = structure_text_as_receipt(text)
        if not raw.get("error"):
            # Stash the raw OCR so downstream code (and humans) can debug
            # mismatches between what was read and what was extracted.
            raw["ocr_text"] = text
            # Post-processing fallbacks recover information the LLM missed
            # (null date, wrong currency, generic "other" category, …).
            _post_process_with_ocr_text(raw, text)
        normalised = _normalize_one_receipt(raw) if not raw.get("error") else raw
        results.append({"metadata": normalised, "crop": img if is_multi else None})

    return results


# ─── Post-processing fallbacks that read the raw OCR text ───────────────────

# Explicit currency tokens with NO scope for ambiguity. Order matters —
# longer / more-specific tokens first so "R$" doesn't accidentally trigger
# on "$" alone.
_CURRENCY_TOKENS = [
    ("CAD$", "CAD"),
    ("CA$", "CAD"),
    ("US$", "USD"),
    ("USD$", "USD"),
    ("AU$", "AUD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("R$ ", "BRL"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("CHF", "CHF"),
    ("CAD", "CAD"),
    ("USD", "USD"),
    ("BRL", "BRL"),
    ("EUR", "EUR"),
    ("GBP", "GBP"),
    ("JPY", "JPY"),
    ("AUD", "AUD"),
    ("MXN", "MXN"),
]

# Date patterns we recognise in OCR text — generous coverage so a missed
# `date` field can still get filled. We return the first match as-is and
# let _parse_amount-equivalent normalisation downstream do the formatting.
_DATE_PATTERNS_FALLBACK = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),  # ISO
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"),  # 10/03/2026 etc.
    re.compile(
        r"\b(\d{1,2}[\s\-]?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        r"\.?[\s\-]?\d{2,4})\b",
        re.IGNORECASE,
    ),  # 10-Mar-2026
    re.compile(
        r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        r"\.?\s+\d{1,2},?\s+\d{2,4})\b",
        re.IGNORECASE,
    ),  # Mar 10, 2026
]


def _scan_currency_in_text(text: str) -> Optional[str]:
    """Find the first unambiguous currency token in the OCR text. Case-
    sensitive on the symbol prefixes (CAD$ vs cad$) but checks both."""
    if not text:
        return None
    for token, code in _CURRENCY_TOKENS:
        if token in text or token.lower() in text.lower():
            return code
    return None


def _scan_date_in_text(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in _DATE_PATTERNS_FALLBACK:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


# Matches a clock time, with optional am/pm marker. Used to chop everything
# from a date string starting at the time portion.
_TIME_TAIL_RE = re.compile(
    r"\s+(?:at\s+)?\d{1,2}[:hH]\d{2}(?:[:hH]?\d{2})?\s*(?:[ap]\.?\s*m\.?)?",
    re.IGNORECASE,
)
# Bare am/pm without an explicit hour (rare but seen on some receipts after
# we already removed the digits via a sloppy match upstream).
_BARE_AMPM_RE = re.compile(r"\s+[ap]\.?\s*m\.?$", re.IGNORECASE)


# Portuguese / Spanish month abbreviations → English (dateutil parses English
# natively but doesn't know "abr" = "Apr" or "ago" = "Aug"). Keep this in sync
# with the locales we see most often: BR (pt), MX/ES (es). Lowercased keys.
_PT_TO_EN_MONTH = {
    "jan": "Jan",
    "fev": "Feb",
    "mar": "Mar",
    "abr": "Apr",
    "mai": "May",
    "jun": "Jun",
    "jul": "Jul",
    "ago": "Aug",
    "set": "Sep",
    "out": "Oct",
    "nov": "Nov",
    "dez": "Dec",
}
_ES_TO_EN_MONTH = {
    "ene": "Jan",
    "feb": "Feb",
    "mar": "Mar",
    "abr": "Apr",
    "may": "May",
    "jun": "Jun",
    "jul": "Jul",
    "ago": "Aug",
    "sep": "Sep",
    "oct": "Oct",
    "nov": "Nov",
    "dic": "Dec",
}


def _normalize_date(
    date_str, *, language: Optional[str] = None, country: Optional[str] = None
) -> Optional[str]:
    """Parse a receipt date into ISO ``YYYY-MM-DD``.

    Handles the formats receipts actually print:
      "25/03/2026"   → "2026-03-25"  (D/M/Y default for non-US)
      "01/abr"       → "2026-04-01"  (PT month, year filled from today)
      "17,03,26"     → "2026-03-17"  (comma separators)
      "3/6/26"       → "2026-06-03"  (D/M/Y default; M/D/Y only for US)
      "2026-03-10"   → "2026-03-10"  (ISO pass-through)
      "10-Mar-2026"  → "2026-03-10"  (English text month)
      "1 abril 2026" → "2026-04-01"  (full PT month)

    Returns None for None/empty/unparseable input — callers should keep the
    original string in that case so we don't silently drop data.

    Disambiguation rule for ambiguous numeric formats: ``country="US"`` →
    month-first; everything else (BR, CA, EU, LATAM, …) → day-first, which
    matches what receipts in those locales actually print.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None

    # Fast path: already ISO and plausible.
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = map(int, m.groups())
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Replace common alternative separators with slashes. Commas and bare dots
    # between digits show up in BR receipts ("17,03,26", "17.03.26").
    s2 = re.sub(r",", "/", s)
    s2 = re.sub(r"(\d)\.(\d)", r"\1/\2", s2)

    # Map non-English month tokens to English so dateutil understands them.
    def _swap_months(text: str, mapping: dict) -> str:
        for src, dst in mapping.items():
            text = re.sub(rf"\b{src}\.?\b", dst, text, flags=re.IGNORECASE)
        return text

    s2 = _swap_months(s2, _PT_TO_EN_MONTH)
    s2 = _swap_months(s2, _ES_TO_EN_MONTH)

    from datetime import datetime as _dt

    today = _dt.now()
    dayfirst = (country or "").upper() != "US"

    try:
        from dateutil import parser as _dp

        dt = _dp.parse(
            s2,
            dayfirst=dayfirst,
            default=_dt(today.year, 1, 1),  # year-fill when missing
            fuzzy=True,
        )
    except Exception:
        return None

    # Guard against wild parses ("forma de pagamento" → 1970-01-01 etc.)
    if not (2000 <= dt.year <= today.year + 1):
        return None
    return dt.strftime("%Y-%m-%d")


def _strip_time_from_date(date_str) -> Optional[str]:
    """Return just the calendar date portion of a date string.

    Receipts commonly print transactional timestamps like:
      "10-Mar-2026 1:44:53p.m."  →  "10-Mar-2026"
      "2026-03-10 10:29 a.m."    →  "2026-03-10"
      "Mar 10, 2026 at 1:44pm"   →  "Mar 10, 2026"

    We don't need the clock time anywhere downstream; the UI displays only
    the date and dedup compares only the date. Time noise also breaks
    sorting in the receipts table.

    Returns the input unchanged if it doesn't look like a date+time
    combination. Returns None for None / empty / whitespace input.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    s = _TIME_TAIL_RE.sub("", s)
    s = _BARE_AMPM_RE.sub("", s)
    # Trailing "at" left over from "Mar 10, 2026 at 1:44pm" once the time bit
    # is removed → also trim it.
    s = re.sub(r"\s+at\s*$", "", s, flags=re.IGNORECASE)
    return s.strip(" ,;-") or None


# Keywords that signal a *final-charged* amount line. Matched case-insensitively
# against the lowercased line.
_TOTAL_FINAL_KEYWORDS = (
    "venta",
    "amount due",
    "amount charged",
    "grand total",
    "total charged",
    "balance due",
    "total cad",
    "total usd",
    "total brl",
    "total eur",
    "valor a pagar",
    "valor total",
    "valor pago",
    "valor a receber",
    "card total",
    "credito",
    "crédito",
)
# Weaker keywords — a plain "total" line. Only used if no FINAL keyword matched.
_TOTAL_HEADLINE_KEYWORDS = ("total",)
# Lines containing any of these are excluded — we never want subtotal/tax/tip
# rows masquerading as the final total.
_TOTAL_NEGATIVE_KEYWORDS = (
    "subtotal",
    "sub total",
    "sub-total",
    "sous total",
    "sub tot",
    "gst",
    "pst",
    "hst",
    "vat",
    "tax",
    "tip",
    "propina",
    "service charge",
    "surcharge",
    "service fee",
    "gratuit",
    "discount",
    "desconto",
)

# Matches a money-like token: 1234,56 or 1234.56, optionally with thousands
# separators. The leading char must not be alnum so "id2928" doesn't match.
_MONEY_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2})(?!\d)")


def _parse_money_token(token: str) -> Optional[float]:
    """Convert a captured money token to float. Handles 1.234,56 (EU)
    vs 1,234.56 (US) by looking at the last separator."""
    if not token:
        return None
    s = token.replace(" ", "")
    if "," in s and "." in s:
        # The rightmost separator is the decimal mark
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Pure comma → EU decimal
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _scan_total_in_text(text: str) -> Optional[float]:
    """Find the most likely final-charged total amount in raw OCR text.

    Receipts commonly print BOTH a pre-tip "Total" line and a later final-
    charged line (VENTA, TOTAL CAD$, Amount Due, …). We scan line-by-line,
    collect every numeric token whose line is tagged with a total keyword
    and is NOT a subtotal/tax/tip/surcharge line, then return the LARGEST.
    Final-charged is always ≥ pre-tip total, so max() is the right reducer.

    Returns None if nothing reliable was found.
    """
    if not text:
        return None
    final_amounts: list[float] = []
    headline_amounts: list[float] = []
    for line in text.splitlines():
        low = line.lower()
        if any(neg in low for neg in _TOTAL_NEGATIVE_KEYWORDS):
            continue
        money = _MONEY_RE.findall(line)
        if not money:
            continue
        # Take the rightmost amount on the line — receipts print labels left,
        # values right.
        amt = _parse_money_token(money[-1])
        if amt is None or amt <= 0:
            continue
        if any(k in low for k in _TOTAL_FINAL_KEYWORDS):
            final_amounts.append(amt)
        elif any(k in low for k in _TOTAL_HEADLINE_KEYWORDS):
            headline_amounts.append(amt)
    if final_amounts:
        return max(final_amounts)
    if headline_amounts:
        return max(headline_amounts)
    return None


def _post_process_with_ocr_text(data: dict, text: str) -> None:
    """In-place: fill gaps that the LLM left using the raw OCR text.

    Four patches:
    - Date: if missing, scan the OCR text with date regexes.
    - Currency: if the OCR text contains an unambiguous symbol/code that
      contradicts the LLM's choice, the explicit text wins.
    - Total amount: if the OCR text shows a larger amount on a TOTAL/VENTA
      line, trust that over the LLM's pick. Pre-tip totals and item prices
      sneak in to total_amount otherwise.
    - Category: when LLM says "other" but vendor name / items have strong
      keyword signals, prefer the keyword inference.
    """
    # Date fallback
    if not (data.get("date") or "").strip():
        d = _scan_date_in_text(text)
        if d:
            data["date"] = d

    # Currency override — only when we find an unambiguous token
    explicit = _scan_currency_in_text(text)
    if explicit and explicit != (data.get("currency") or "").upper():
        # Don't override an unambiguous code with a different unambiguous code
        # unless the OCR text has the explicit token — which it does, by
        # construction here.
        data["currency"] = explicit

    # Total-amount override — pick the largest TOTAL/VENTA-tagged amount.
    # Only override if it's meaningfully larger than what the LLM picked
    # (>= 1% bigger, to avoid flipping on OCR-noise round-off).
    scanned = _scan_total_in_text(text)
    if scanned is not None:
        existing = data.get("total_amount")
        try:
            existing_f = float(existing) if existing not in (None, "") else None
        except (TypeError, ValueError):
            existing_f = None
        if existing_f is None or scanned > existing_f * 1.01:
            data["total_amount"] = scanned

    # Category rescue: only when LLM defaulted to "other"
    if (data.get("category") or "").lower() == "other":
        inferred = _infer_category(data.get("vendor_name", ""), data.get("items") or [])
        # Use the inferred category if it found something specific. Also
        # try the raw OCR text — vendor name alone misses cases like a
        # cafe receipt where the items list is empty after cleaning.
        if inferred == "other":
            inferred = _infer_category(text, [])
        if inferred and inferred != "other":
            data["category"] = inferred


def process_receipt_image(image_path: str) -> list:
    """Back-compat wrapper. Returns metadata dicts only; crop info dropped."""
    return [item["metadata"] for item in process_with_segments(image_path)]


def ocr_crops(crops: list) -> list:
    """
    OCR a caller-provided list of PIL crops, in order. Returns a list of
    `{"metadata": <dict>, "crop": <PIL.Image>}` items — one per receipt
    extracted. Used by the bbox-editor commit endpoint where the user has
    already split the image into segments and we just need to OCR each.

    Falls back to a single `_empty_receipt()` row on model/pick failure so
    the caller never gets an empty list.
    """
    if not crops:
        empty = _empty_receipt()
        empty["error"] = "no crops"
        return [{"metadata": empty, "crop": None}]
    try:
        model = _pick_model()
    except OCRConfigError as e:
        logger.error("OCR config error: %s", e)
        empty = _empty_receipt()
        empty["error"] = str(e)
        # Attach the first crop so the caller still has something to save
        return [{"metadata": empty, "crop": crops[0]}]

    results: list = []
    for i, img in enumerate(crops, 1):
        image_bytes = _image_to_ocr_bytes(img)
        logger.info("OCR (user-crop): %d/%d image_bytes=%d", i, len(crops), len(image_bytes))
        # Use the verbose PROMPT here too — the per-field examples ("e.g.
        # 30/04/2026", "e.g. 41,78 or 1.234,56") meaningfully anchor the
        # model's parsing. Empirically the shorter SINGLE_RECEIPT_PROMPT
        # saved only a few seconds per call but dropped accuracy on
        # vendor/currency/category. Speed gain wasn't worth the regression.
        for r in _ocr_one_crop(model, image_bytes):
            results.append({"metadata": r, "crop": img})
    return results or [{"metadata": _empty_receipt(), "crop": crops[0]}]
