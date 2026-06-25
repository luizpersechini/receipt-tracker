"""Tests for the PaddleOCR adapter (src/services/paddle_ocr.py).

Like the Apple Vision tests, these exercise the *adapter* layer — the
version-tolerant result parsing, bbox conversion, reading-order sorting and
availability guards — not the PaddlePaddle runtime itself (which is heavy and
platform-specific, and live-tested via the QA corpus on the target machine).
"""

import pytest
from PIL import Image

from src.services import paddle_ocr as po


def test_module_imports_without_paddle_installed():
    """The module must import even where paddleocr isn't installed — only the
    call sites should fail, not the import."""
    assert callable(po.is_available)
    assert callable(po.extract_text_from_file)
    assert callable(po.extract_text_from_pil)
    assert callable(po.extract_text_boxes_from_file)
    assert callable(po.warmup)


# ─── bbox conversion ────────────────────────────────────────────────────────


def test_poly_to_bbox_from_quad():
    quad = [[10, 20], [110, 20], [110, 60], [10, 60]]
    assert po._poly_to_bbox(quad) == (10, 20, 100, 40)


def test_poly_to_bbox_from_flat_rect():
    # Axis-aligned [x1, y1, x2, y2] (PaddleOCR 3.x rec_boxes shape).
    assert po._poly_to_bbox([10, 20, 110, 60]) == (10, 20, 100, 40)


def test_poly_to_bbox_handles_unordered_points():
    # Points not in clockwise order must still yield the enclosing rect.
    quad = [[110, 60], [10, 20], [110, 20], [10, 60]]
    assert po._poly_to_bbox(quad) == (10, 20, 100, 40)


def test_poly_to_bbox_none():
    assert po._poly_to_bbox(None) is None


# ─── result normalisation (version tolerance) ───────────────────────────────


def test_normalize_old_style_2x_nested_list():
    # PaddleOCR 2.x: [[ [box, (text, score)], ... ]]
    raw = [
        [
            [[[0, 0], [50, 0], [50, 10], [0, 10]], ("HELLO", 0.99)],
            [[[0, 20], [80, 20], [80, 30], [0, 30]], ("WORLD", 0.95)],
        ]
    ]
    items = po._normalize(raw)
    assert [t for t, _s, _p in items] == ["HELLO", "WORLD"]
    assert items[0][1] == pytest.approx(0.99)


def test_normalize_new_style_3x_dict():
    # PaddleOCR 3.x: [ {rec_texts, rec_scores, rec_polys} ]
    raw = [
        {
            "rec_texts": ["FOO", "BAR"],
            "rec_scores": [0.88, 0.77],
            "rec_polys": [
                [[0, 0], [10, 0], [10, 5], [0, 5]],
                [[0, 8], [10, 8], [10, 13], [0, 13]],
            ],
        }
    ]
    items = po._normalize(raw)
    assert [t for t, _s, _p in items] == ["FOO", "BAR"]
    assert items[1][1] == pytest.approx(0.77)


def test_normalize_empty():
    assert po._normalize(None) == []
    assert po._normalize([]) == []
    assert po._normalize([None]) == []


# ─── reading order + public surface (stubbed engine) ────────────────────────


@pytest.fixture
def _stub_engine(monkeypatch):
    """Make the adapter think PaddleOCR is available and feed canned output,
    bypassing the real model build / inference."""
    monkeypatch.setattr(po, "_AVAILABILITY_CHECKED", True)
    monkeypatch.setattr(po, "_AVAILABLE", True)
    monkeypatch.setattr(po, "_AVAILABILITY_ERROR", None)
    monkeypatch.setattr(po, "_get_instance", lambda: object())

    def _set(raw):
        monkeypatch.setattr(po, "_invoke", lambda inst, path: raw)

    return _set


def test_extract_text_sorts_into_reading_order(_stub_engine, tmp_path):
    # Lines deliberately supplied bottom-to-top; output must be top-to-bottom.
    raw = [
        [
            [[[0, 200], [50, 200], [50, 220], [0, 220]], ("TOTAL 9.64", 0.9)],
            [[[0, 10], [50, 10], [50, 30], [0, 30]], ("TREES COFFEE", 0.9)],
            [[[0, 100], [50, 100], [50, 120], [0, 120]], ("Latte 4.50", 0.9)],
        ]
    ]
    _stub_engine(raw)
    p = tmp_path / "r.png"
    Image.new("RGB", (60, 240), "white").save(p)
    text = po.extract_text_from_file(str(p))
    assert text.splitlines() == ["TREES COFFEE", "Latte 4.50", "TOTAL 9.64"]


def test_extract_text_skips_blank_lines(_stub_engine, tmp_path):
    raw = [
        [
            [[[0, 10], [50, 10], [50, 30], [0, 30]], ("KEEP", 0.9)],
            [[[0, 40], [50, 40], [50, 60], [0, 60]], ("   ", 0.9)],
        ]
    ]
    _stub_engine(raw)
    p = tmp_path / "r.png"
    Image.new("RGB", (60, 80), "white").save(p)
    assert po.extract_text_from_file(str(p)) == "KEEP"


def test_extract_text_boxes_returns_pixel_bboxes(_stub_engine, tmp_path):
    raw = [
        {
            "rec_texts": ["A"],
            "rec_scores": [0.91],
            "rec_polys": [[[5, 6], [25, 6], [25, 16], [5, 16]]],
        }
    ]
    _stub_engine(raw)
    p = tmp_path / "r.png"
    Image.new("RGB", (40, 40), "white").save(p)
    boxes = po.extract_text_boxes_from_file(str(p), (40, 40))
    assert boxes == [{"text": "A", "bbox": (5, 6, 20, 10), "confidence": pytest.approx(0.91)}]


def test_extract_text_raises_when_unavailable(monkeypatch):
    monkeypatch.setattr(po, "_AVAILABILITY_CHECKED", True)
    monkeypatch.setattr(po, "_AVAILABLE", False)
    monkeypatch.setattr(po, "_AVAILABILITY_ERROR", "ModuleNotFoundError: paddleocr")
    with pytest.raises(RuntimeError, match="PaddleOCR not available"):
        po.extract_text_from_file("/tmp/nope.png")


def test_warmup_returns_false_when_unavailable(monkeypatch):
    monkeypatch.setattr(po, "_AVAILABILITY_CHECKED", True)
    monkeypatch.setattr(po, "_AVAILABLE", False)
    monkeypatch.setattr(po, "_AVAILABILITY_ERROR", "nope")
    assert po.warmup() is False


# ─── dispatch wiring (llm_processor → paddle_ocr → structurer) ──────────────


def test_process_via_text_extraction_routes_paddle_to_structurer(monkeypatch):
    """The shared text-extraction path must feed PaddleOCR's text into the
    structuring step for backend='paddle_ocr'."""
    from PIL import Image

    from src.services import llm_processor as llm
    from src.services import paddle_ocr as pomod
    from src.services import text_to_receipt

    monkeypatch.setattr(pomod, "is_available", lambda: True)
    monkeypatch.setattr(pomod, "extract_text_from_pil", lambda img: "VENDOR\nTOTAL 9.64")

    seen = {}

    def _fake_structure(text, **kw):
        seen["text"] = text
        return {"error": "stub-stop"}  # truthy error short-circuits post-processing

    monkeypatch.setattr(text_to_receipt, "structure_text_as_receipt", _fake_structure)

    out = llm._process_via_text_extraction(
        [Image.new("RGB", (10, 10), "white")], is_multi=False, backend="paddle_ocr"
    )
    assert seen["text"] == "VENDOR\nTOTAL 9.64"
    assert out[0]["metadata"]["ocr_text"] == "VENDOR\nTOTAL 9.64"


def test_process_via_text_extraction_reports_unavailable_backend(monkeypatch):
    from PIL import Image

    from src.services import llm_processor as llm
    from src.services import paddle_ocr as pomod

    monkeypatch.setattr(pomod, "is_available", lambda: False)
    monkeypatch.setattr(pomod, "unavailability_reason", lambda: "not installed")

    out = llm._process_via_text_extraction(
        [Image.new("RGB", (10, 10), "white")], is_multi=False, backend="paddle_ocr"
    )
    assert "PaddleOCR OCR not available" in out[0]["metadata"]["error"]
