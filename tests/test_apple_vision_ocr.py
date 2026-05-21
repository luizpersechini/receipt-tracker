"""Tests for the Apple Vision OCR adapter.

These tests only exercise the *adapter* layer — the wrapping logic around
the Vision framework call, not the framework itself. The actual Vision
calls are skipped on non-Darwin systems and live-tested manually on Mac.
"""

import sys
import importlib

import pytest
from PIL import Image

from src.services import apple_vision_ocr as av


def test_module_imports_without_vision_framework():
    """The module must import even on systems where pyobjc-Vision isn't
    available — only the call sites should fail, not the import."""
    assert callable(av.is_available)
    assert callable(av.extract_text_from_file)
    assert callable(av.extract_text_from_pil)


def test_unavailability_reason_is_none_initially_then_set_after_check():
    """is_available() caches the result. unavailability_reason() reflects
    the last probe."""
    # Force a re-probe by resetting the module-level cache
    av._AVAILABILITY_CHECKED = False
    av._AVAILABLE = False
    av._AVAILABILITY_ERROR = None
    available = av.is_available()
    if available:
        assert av.unavailability_reason() is None
    else:
        assert av.unavailability_reason() is not None


def test_extract_text_raises_when_unavailable(monkeypatch):
    """When the binding isn't loadable, the extractor must raise RuntimeError
    with a useful message instead of crashing with NameError downstream."""
    monkeypatch.setattr(av, "_AVAILABILITY_CHECKED", True)
    monkeypatch.setattr(av, "_AVAILABLE", False)
    monkeypatch.setattr(av, "_AVAILABILITY_ERROR", "ModuleNotFoundError: No module named 'Vision'")
    with pytest.raises(RuntimeError, match="Apple Vision OCR not available"):
        av.extract_text_from_file("/tmp/nope.png")
    with pytest.raises(RuntimeError, match="Apple Vision OCR not available"):
        av.extract_text_from_pil(Image.new("RGB", (10, 10)))


@pytest.mark.skipif(sys.platform != "darwin", reason="Vision framework is macOS-only")
def test_extracts_text_from_a_synthetic_text_image(tmp_path):
    """End-to-end test that runs on actual macOS: render a known string,
    extract it, expect the string back. Tolerates exact spacing differences."""
    if not av.is_available():
        pytest.skip(f"Vision unavailable: {av.unavailability_reason()}")
    # Render "HELLO RECEIPT TRACKER 12.34" as bold black on white
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (800, 200), color="white")
    drw = ImageDraw.Draw(img)
    target = "HELLO RECEIPT TRACKER 12.34"
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
    except OSError:
        font = ImageFont.load_default()
    drw.text((20, 60), target, fill="black", font=font)
    p = tmp_path / "txt.png"
    img.save(p)
    text = av.extract_text_from_file(str(p))
    # Some characters may differ on OCR; do a lenient substring check
    assert "RECEIPT" in text.upper()
    assert "12.34" in text or "12 34" in text
