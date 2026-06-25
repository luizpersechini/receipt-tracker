"""
On-device OCR using PaddleOCR (Baidu PP-OCR) — the cross-platform sibling of
`apple_vision_ocr.py`.

Why this exists:
- `apple_vision_ocr.py` is the fast, low-RAM, on-device text extractor behind
  the `apple_vision` backend — but it only works on macOS (pyobjc Vision
  bindings build for Darwin only). On Windows/Linux that backend reports
  `is_available() == False`, forcing the heavy `ollama_vision` one-shot path.
- PaddleOCR is a small, fully-local, open-source OCR engine (PP-OCR) that runs
  on CPU on any platform. It plays the exact same role Apple Vision does:
  image → raw lines of text, which the existing `text_to_receipt` step then
  structures with a small text LLM. No vision-LM, no external API.

This module mirrors the PUBLIC SURFACE of `apple_vision_ocr.py` so it drops
into the same call sites:
    is_available() / unavailability_reason()
    extract_text_from_file(path) / extract_text_from_pil(img)
    extract_text_boxes_from_file(path, image_size)   # backs the segmenter
    warmup()                                          # preload the model

Imports are lazy: dev machines and tests that mock this layer (or run on a
box without paddleocr installed) don't pay the import cost or need the wheel.

Version tolerance: PaddleOCR's API shifted between the 2.x and 3.x (PP-OCRv5+)
lines — constructor kwargs (`use_angle_cls` → `use_textline_orientation`) and
result shape (nested lists → `rec_texts`/`rec_polys` dicts) both changed. The
helpers below probe both so a fresh `pip install paddleocr` works regardless of
which major version resolves.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import List, Optional

from PIL import Image

from ..config import settings

logger = logging.getLogger(__name__)

# PaddleOCR / PaddlePaddle are chatty at INFO (per-image inference banners).
# Quieten them so a multi-receipt page doesn't flood the server log.
for _noisy in ("ppocr", "paddle", "paddleocr"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ─── Availability check ────────────────────────────────────────────────────

_AVAILABILITY_CHECKED = False
_AVAILABLE = False
_AVAILABILITY_ERROR: Optional[str] = None


def is_available() -> bool:
    """Lazy probe — True iff the `paddleocr` package imports. The deeper
    PaddlePaddle runtime is only exercised when the model is first built
    (see `warmup()` / first OCR call); a failure there surfaces as a per-run
    error rather than flipping availability, mirroring Apple Vision."""
    global _AVAILABILITY_CHECKED, _AVAILABLE, _AVAILABILITY_ERROR
    if _AVAILABILITY_CHECKED:
        return _AVAILABLE
    _AVAILABILITY_CHECKED = True
    try:
        import paddleocr  # noqa: F401

        _AVAILABLE = True
    except Exception as e:  # noqa: BLE001 — any import failure means unavailable
        _AVAILABILITY_ERROR = f"{type(e).__name__}: {e}"
        logger.warning("PaddleOCR not available: %s", _AVAILABILITY_ERROR)
    return _AVAILABLE


def unavailability_reason() -> Optional[str]:
    """If `is_available()` returned False, returns the diagnostic message."""
    if not _AVAILABILITY_CHECKED:
        is_available()
    return _AVAILABILITY_ERROR


# ─── Lazy singleton model ──────────────────────────────────────────────────
#
# Building a PaddleOCR instance loads the detection + recognition + angle
# models (~1-2s, and a one-time model download on first ever run). We build
# once and reuse. The same lock serialises the actual inference call too:
# a single PaddleOCR instance is NOT safe to call from multiple threads, and
# the OCR pipeline can run with ocr_concurrency > 1.

_LOCK = threading.Lock()
_INSTANCE = None
_INSTANCE_LANG: Optional[str] = None


def _build_instance(lang: str):
    """Construct a PaddleOCR across 2.x/3.x constructor differences."""
    from paddleocr import PaddleOCR

    base = {"lang": lang}
    use_gpu = bool(getattr(settings, "paddle_use_gpu", False))
    # GPU is requested differently across versions: 3.x takes device="gpu",
    # 2.x takes use_gpu=True. CPU is the default if neither applies.
    gpu_3x = {"device": "gpu"} if use_gpu else {}
    gpu_2x = {"use_gpu": True} if use_gpu else {}
    # Ordered most-modern first. Unknown kwargs raise TypeError/ValueError,
    # so we fall through to an older-style signature.
    variants = [
        {**base, **gpu_3x, "use_textline_orientation": True},  # 3.x (PP-OCRv5+)
        {**base, **gpu_2x, "use_angle_cls": True, "show_log": False},  # 2.x
        {**base, **gpu_2x, "use_angle_cls": True},  # 2.x without show_log
        base,  # bare minimum (CPU)
    ]
    last_err: Optional[Exception] = None
    for kw in variants:
        try:
            inst = PaddleOCR(**kw)
            logger.info("PaddleOCR initialised (lang=%s, kwargs=%s)", lang, list(kw))
            return inst
        except (TypeError, ValueError) as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not initialise PaddleOCR (lang={lang}): {last_err}")


def _get_instance():
    """Return the cached PaddleOCR, rebuilding if the configured language
    changed. Caller must hold `_LOCK` (or accept the build race on first use)."""
    global _INSTANCE, _INSTANCE_LANG
    lang = (settings.paddle_lang or "en").strip() or "en"
    if _INSTANCE is None or _INSTANCE_LANG != lang:
        _INSTANCE = _build_instance(lang)
        _INSTANCE_LANG = lang
    return _INSTANCE


def warmup() -> bool:
    """Preload the model so the first user upload doesn't pay the build cost
    (and triggers the one-time model download). Returns True on success."""
    if not is_available():
        return False
    try:
        with _LOCK:
            _get_instance()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("PaddleOCR warmup failed: %s", e)
        return False


# ─── Result parsing (version-tolerant) ─────────────────────────────────────


def _invoke(inst, image_path: str):
    """Run inference. `.ocr()` exists on both 2.x and 3.x (3.x deprecates it
    but still returns a materialised list); `.predict()` is the 3.x-native
    entry point and may return a generator — materialise it to a list so the
    downstream `raw[0]` indexing in `_normalize` is always safe."""
    try:
        return inst.ocr(image_path)
    except TypeError:
        pass  # signature mismatch — fall through
    except Exception as e:  # noqa: BLE001
        logger.debug("PaddleOCR.ocr() raised (%s); trying predict()", e)
    if hasattr(inst, "predict"):
        out = inst.predict(image_path)
        return out if isinstance(out, (list, tuple)) else list(out)
    return inst.ocr(image_path, cls=True)


def _poly_to_bbox(poly) -> Optional[tuple]:
    """Convert a recognised region to a pixel (x, y, w, h) bbox.

    Handles a 4-point quad ([[x,y], ...]) and a flat axis-aligned rect
    ([x1, y1, x2, y2]). Coordinates are already in image pixels (origin
    top-left), so — unlike Apple Vision — no normalisation is needed."""
    if poly is None:
        return None
    try:
        pts = [(float(p[0]), float(p[1])) for p in poly]
    except (TypeError, IndexError, ValueError):
        # Flat [x1, y1, x2, y2]
        try:
            vals = [float(v) for v in poly]
        except (TypeError, ValueError):
            return None
        if len(vals) >= 4:
            x1, y1, x2, y2 = vals[:4]
            return (int(min(x1, x2)), int(min(y1, y2)), int(abs(x2 - x1)), int(abs(y2 - y1)))
        return None
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x, y = min(xs), min(ys)
    return (int(x), int(y), int(max(xs) - x), int(max(ys) - y))


def _normalize(raw) -> List[tuple]:
    """Flatten PaddleOCR output into [(text, confidence, poly), ...] across
    the 2.x nested-list and 3.x dict result shapes."""
    items: List[tuple] = []
    if not raw:
        return items

    # Results are per-image; receipt crops are single images → take page 0.
    page = raw[0] if isinstance(raw, (list, tuple)) and len(raw) else raw
    if page is None:
        return items

    # New-style 3.x: a dict (OCRResult) with parallel arrays.
    if hasattr(page, "get") and not isinstance(page, list):
        texts = page.get("rec_texts") or []
        scores = page.get("rec_scores") or []
        polys = page.get("rec_polys")
        if polys is None:
            polys = page.get("dt_polys") or page.get("rec_boxes") or []
        for i, text in enumerate(texts):
            poly = polys[i] if i < len(polys) else None
            score = float(scores[i]) if i < len(scores) else 0.0
            items.append((str(text), score, poly))
        return items

    # Old-style 2.x: page is a list of [box, (text, score)].
    for line in page:
        try:
            box, rec = line[0], line[1]
            text, score = rec[0], rec[1]
        except (IndexError, TypeError):
            continue
        items.append((str(text), float(score), box))
    return items


def _ocr_items(image_path: str) -> List[tuple]:
    """Run OCR and return [(text, confidence, bbox|None), ...] in reading
    order (top-to-bottom, then left-to-right)."""
    if not is_available():
        raise RuntimeError(
            f"PaddleOCR not available: {_AVAILABILITY_ERROR or 'unknown reason'}"
        )
    with _LOCK:
        inst = _get_instance()
        raw = _invoke(inst, image_path)

    parsed = _normalize(raw)
    rows = []
    for text, score, poly in parsed:
        if not str(text).strip():
            continue
        rows.append((text, score, _poly_to_bbox(poly)))
    # Reading order: by top-y then x. Regions without a bbox sort last.
    rows.sort(key=lambda r: (r[2][1] if r[2] else 1 << 30, r[2][0] if r[2] else 0))
    return rows


# ─── Public API (mirrors apple_vision_ocr) ─────────────────────────────────


def _assemble_lines(rows: List[tuple]) -> List[str]:
    """Group recognised boxes into VISUAL LINES (one printed receipt line per
    output string), instead of emitting one line per detected box.

    PaddleOCR detects each text span as its own box, so a printed line like
    ``Total .......... R$ 70,00`` arrives as two separate boxes — and emitting
    them on two lines breaks the label↔value adjacency the structuring LLM
    relies on (it then can't tell which amount is the total). We cluster boxes
    whose vertical extents overlap into a single line and order them
    left-to-right, so the text reads the way a human sees the receipt.

    `rows` is [(text, score, bbox|None), ...]; bbox is (x, y, w, h)."""
    boxed = [(t, b) for (t, _s, b) in rows if b is not None]
    unboxed = [t for (t, _s, b) in rows if b is None]

    # Sort by vertical centre so we can sweep top-to-bottom.
    boxed.sort(key=lambda it: it[1][1] + it[1][3] / 2.0)

    lines: List[list] = []  # each: [items, y0, y1]; items = [(x, text), ...]
    for text, (x, y, w, h) in boxed:
        y0, y1 = y, y + h
        if lines:
            cur_items, cur_y0, cur_y1 = lines[-1]
            overlap = min(cur_y1, y1) - max(cur_y0, y0)
            # Same line if the vertical overlap is a meaningful fraction of the
            # smaller box height (robust to differing font sizes on a row).
            if overlap > 0.3 * min(h, cur_y1 - cur_y0):
                cur_items.append((x, text))
                lines[-1][1] = min(cur_y0, y0)
                lines[-1][2] = max(cur_y1, y1)
                continue
        lines.append([[(x, text)], y0, y1])

    out: List[str] = []
    for items, _y0, _y1 in lines:
        items.sort(key=lambda it: it[0])  # left-to-right within the line
        out.append("  ".join(t for _x, t in items))
    out.extend(unboxed)  # geometry-less fragments last
    return out


def extract_text_from_file(
    image_path: str,
    *,
    languages: Optional[List[str]] = None,
) -> str:
    """Run PaddleOCR on `image_path` and return a newline-joined blob, one
    printed receipt LINE per row (boxes on the same visual line are merged),
    in reading order. Returns "" if no text.

    `languages` is accepted for signature parity with `apple_vision_ocr`;
    PaddleOCR's language is fixed at model-build time (settings.paddle_lang),
    so the argument is ignored here."""
    rows = _ocr_items(image_path)
    return "\n".join(_assemble_lines(rows))


def extract_text_from_pil(
    img: Image.Image,
    *,
    languages: Optional[List[str]] = None,
) -> str:
    """Same as extract_text_from_file but takes a PIL image. Writes a temp PNG
    (PaddleOCR reads a path/array; a path is simplest and matches the Apple
    Vision implementation's temp-file approach)."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img)  # fix raw EXIF orientation; no-op for crops
    if img.mode != "RGB":
        img = img.convert("RGB")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img.save(tmp_path, format="PNG", optimize=False)
        return extract_text_from_file(tmp_path, languages=languages)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_text_boxes_from_file(
    image_path: str,
    image_size: tuple,
    *,
    languages: Optional[List[str]] = None,
) -> List[dict]:
    """Run PaddleOCR and return one dict per recognised line with its bounding
    box in IMAGE PIXEL coordinates (origin top-left):

        [{"text": "...", "bbox": (x, y, w, h), "confidence": 0.97}, ...]

    Used by the segmenter to cluster text into receipt regions. `image_size`
    is accepted for parity with `apple_vision_ocr` (PaddleOCR already returns
    pixel coords, so no normalisation is required)."""
    rows = _ocr_items(image_path)
    items: List[dict] = []
    for text, score, bbox in rows:
        if bbox is None:
            continue
        items.append({"text": text, "bbox": bbox, "confidence": score})
    return items
