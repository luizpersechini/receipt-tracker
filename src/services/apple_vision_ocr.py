"""
On-device OCR using macOS Apple Vision framework (VNRecognizeTextRequest).

Why this exists:
- glm-ocr is fast but lossy on receipts (mis-reads currencies, vendor names).
- llava/moondream are accurate but the vision encoder thrashes RAM on
  Apple-Silicon laptops — peak memory + per-call latency are both painful.
- macOS already ships a class-leading printed-text OCR engine that runs on
  the Neural Engine. It's the same component behind Live Text.

On a 2024 M-series Mac this typically returns hundreds-of-ms for a 1500px
image, regardless of how many distinct text regions the page contains.
The output is raw lines of text; the *structuring* step (mapping text →
{vendor, date, total, currency, ...}) is handled separately by a small
text LLM, which is a much easier job than vision+OCR+structuring combined.

Pure-Python via pyobjc:
- pyobjc-framework-Vision   (the Vision.framework bindings)
- pyobjc-framework-Quartz   (CIImage helpers, transitively pulled by Vision)

Module imports are lazy so non-macOS dev environments and tests that mock
this layer don't need the binding at import time.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import List, Optional

from PIL import Image

logger = logging.getLogger(__name__)


# Recognition levels exposed by VNRequestTextRecognitionLevel:
#   0 = Accurate (slower, better for receipts / multi-line text)
#   1 = Fast     (real-time, less accurate)
RECOGNITION_LEVEL_ACCURATE = 0
RECOGNITION_LEVEL_FAST = 1


# ─── Availability check ────────────────────────────────────────────────────

_AVAILABILITY_CHECKED = False
_AVAILABLE = False
_AVAILABILITY_ERROR: Optional[str] = None


def is_available() -> bool:
    """Lazy probe — True iff pyobjc Vision bindings load and the Vision
    framework is reachable. Caches the result so we don't repeatedly try
    a doomed import."""
    global _AVAILABILITY_CHECKED, _AVAILABLE, _AVAILABILITY_ERROR
    if _AVAILABILITY_CHECKED:
        return _AVAILABLE
    _AVAILABILITY_CHECKED = True
    try:
        from Vision import VNRecognizeTextRequest, VNImageRequestHandler  # noqa: F401
        _AVAILABLE = True
    except Exception as e:
        _AVAILABILITY_ERROR = f"{type(e).__name__}: {e}"
        logger.warning("Apple Vision OCR not available: %s", _AVAILABILITY_ERROR)
    return _AVAILABLE


def unavailability_reason() -> Optional[str]:
    """If `is_available()` returned False, returns the diagnostic message."""
    if not _AVAILABILITY_CHECKED:
        is_available()
    return _AVAILABILITY_ERROR


# ─── Core OCR ──────────────────────────────────────────────────────────────

def extract_text_from_file(image_path: str, *, level: int = RECOGNITION_LEVEL_ACCURATE,
                           languages: Optional[List[str]] = None) -> str:
    """
    Run Apple Vision OCR on `image_path` and return a single newline-joined
    text blob with one detected line per row. Returns "" if no text is
    found. Raises RuntimeError if the binding isn't available.

    `languages` is an optional list of BCP-47 language tags to prefer
    (e.g. `["pt-BR", "en-US"]`). Apple Vision auto-detects in most cases;
    setting this helps when the receipt has unusual diacritics.
    """
    if not is_available():
        raise RuntimeError(
            f"Apple Vision OCR not available: {_AVAILABILITY_ERROR or 'unknown reason'}"
        )

    from Foundation import NSURL                          # noqa: E402
    from Vision import (                                  # noqa: E402
        VNRecognizeTextRequest, VNImageRequestHandler,
    )

    url = NSURL.fileURLWithPath_(image_path)
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(level)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(languages)

    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision request failed: {err}")

    results = request.results() or []
    lines: List[str] = []
    for obs in results:
        top = obs.topCandidates_(1)
        if top.count() > 0:
            lines.append(str(top.objectAtIndex_(0).string()))
    return "\n".join(lines)


def extract_text_from_pil(img: Image.Image, *, level: int = RECOGNITION_LEVEL_ACCURATE,
                          languages: Optional[List[str]] = None) -> str:
    """Same as extract_text_from_file but takes a PIL image. We write to a
    temp PNG because VNImageRequestHandler's CIImage initialiser is tricky
    to feed from raw Python bytes; a 1.5 MB temp PNG is cheap."""
    if not is_available():
        raise RuntimeError(
            f"Apple Vision OCR not available: {_AVAILABILITY_ERROR or 'unknown reason'}"
        )

    if img.mode != "RGB":
        img = img.convert("RGB")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img.save(tmp_path, format="PNG", optimize=False)
        return extract_text_from_file(tmp_path, level=level, languages=languages)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_text_boxes_from_file(image_path: str, image_size: tuple,
                                 *, level: int = RECOGNITION_LEVEL_ACCURATE,
                                 languages: Optional[List[str]] = None) -> List[dict]:
    """
    Run Apple Vision OCR and return one dict per recognised text line with
    both the text AND its bounding box in IMAGE PIXEL coordinates:

        [{"text": "...", "bbox": (x, y, w, h), "confidence": 0.97}, ...]

    `image_size` is required so we can convert from Vision's normalised
    (0..1, origin bottom-left) coords to pixel coords (origin top-left).

    Used by the segmenter to cluster text into receipt regions — way more
    reliable than image-only thresholding when receipts share a similar
    brightness to the background, because each text region has a precise
    geometric footprint.
    """
    if not is_available():
        raise RuntimeError(
            f"Apple Vision OCR not available: {_AVAILABILITY_ERROR or 'unknown reason'}"
        )

    from Foundation import NSURL                          # noqa: E402
    from Vision import (                                  # noqa: E402
        VNRecognizeTextRequest, VNImageRequestHandler,
    )

    img_w, img_h = image_size
    url = NSURL.fileURLWithPath_(image_path)
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(level)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(languages)

    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision request failed: {err}")

    items: List[dict] = []
    for obs in (request.results() or []):
        top = obs.topCandidates_(1)
        if top.count() == 0:
            continue
        text = str(top.objectAtIndex_(0).string())
        confidence = float(top.objectAtIndex_(0).confidence())
        # boundingBox is normalised (0..1) with origin at BOTTOM-LEFT.
        # Convert to pixel coords with origin at TOP-LEFT.
        bb = obs.boundingBox()
        nx = float(bb.origin.x)
        ny_bl = float(bb.origin.y)        # y measured from bottom
        nw = float(bb.size.width)
        nh = float(bb.size.height)
        px = int(nx * img_w)
        py = int((1.0 - ny_bl - nh) * img_h)
        pw = int(nw * img_w)
        ph = int(nh * img_h)
        items.append({
            "text": text,
            "bbox": (px, py, pw, ph),
            "confidence": confidence,
        })
    return items
