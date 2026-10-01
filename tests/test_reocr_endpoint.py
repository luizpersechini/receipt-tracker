"""POST /api/receipts/{id}/reocr with per-call setting overrides.

OCR itself is stubbed at `process_with_segments`; we verify the overrides
travel, the row's metadata is replaced, bad input is rejected, and — at the
pipeline level — that an override is visible inside the call and gone after.
"""

import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    receipts = tmp_path / "receipts"
    data = tmp_path / "data"
    receipts.mkdir()
    data.mkdir()
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "receipts_dir", receipts)
    monkeypatch.setattr(settings, "data_dir", data)

    from src.db import database

    database.init_db()
    from src.main import app

    return TestClient(app), receipts, database


def _row(receipts, database, name="r.png"):
    p = receipts / name
    Image.new("RGB", (200, 300), "white").save(p)
    return database.save_receipt(name, str(p), {"vendor_name": "OLD", "total_amount": 1.0})


def _stub(monkeypatch, captured, meta=None):
    from src.routers import receipts as router

    def fake(path, *, skip_segmentation=False, overrides=None):
        captured.update({"path": path, "skip": skip_segmentation, "overrides": overrides})
        return [{"metadata": meta or {"vendor_name": "NEW", "total_amount": 42.5}, "crop": None}]

    monkeypatch.setattr(router, "process_with_segments", fake)


def test_reocr_without_body_is_unchanged(client, monkeypatch):
    c, receipts, database = client
    rid = _row(receipts, database)
    cap = {}
    _stub(monkeypatch, cap)
    r = c.post(f"/api/receipts/{rid}/reocr")
    assert r.status_code == 200, r.text
    assert cap["skip"] is True and cap["overrides"] is None
    assert json.loads(database.get_receipt_by_id(rid)["metadata"])["vendor_name"] == "NEW"


def test_reocr_overrides_reach_pipeline_and_are_not_persisted(client, monkeypatch):
    c, receipts, database = client
    rid = _row(receipts, database)
    cap = {}
    _stub(monkeypatch, cap)
    body = {
        "backend": "apple_vision",
        "text_model": "llama3.1:8b",
        "structure_backend": "anthropic",
    }
    r = c.post(f"/api/receipts/{rid}/reocr", json=body)
    assert r.status_code == 200, r.text
    assert cap["overrides"] == body
    assert r.json()["overrides"] == body
    # global config untouched
    from src.services import ocr_settings

    assert ocr_settings.get_config()["text_model"] != "llama3.1:8b"
    assert (settings.structure_backend or "ollama") != "anthropic"


def test_reocr_rejects_unknown_backend(client, monkeypatch):
    c, receipts, database = client
    rid = _row(receipts, database)
    _stub(monkeypatch, {})
    assert c.post(f"/api/receipts/{rid}/reocr", json={"backend": "tesseract"}).status_code == 422
    assert (
        c.post(f"/api/receipts/{rid}/reocr", json={"structure_backend": "openai"}).status_code
        == 422
    )


def test_reocr_surfaces_ocr_error(client, monkeypatch):
    c, receipts, database = client
    rid = _row(receipts, database)
    _stub(monkeypatch, {}, meta={"error": "anthropic structuring failed: no key"})
    r = c.post(f"/api/receipts/{rid}/reocr", json={"structure_backend": "anthropic"})
    assert r.status_code == 500 and "no key" in r.json()["detail"]
    assert json.loads(database.get_receipt_by_id(rid)["metadata"])["vendor_name"] == "OLD"


def test_reocr_missing_row_and_file(client, monkeypatch):
    c, receipts, database = client
    assert c.post("/api/receipts/999/reocr").status_code == 404
    rid = database.save_receipt("gone.png", str(receipts / "gone.png"), {})
    assert c.post(f"/api/receipts/{rid}/reocr").status_code == 404


# ─── pipeline-level: the override is scoped to the call ────────────────────


def test_overrides_scoped_to_call(monkeypatch):
    from src.services import llm_processor as lp

    seen = {}

    def fake_text_path(segments, is_multi, *, backend):
        seen["backend"] = backend
        seen["text_model"] = lp._ov("text_model")
        seen["structure_backend"] = lp._ov("structure_backend")
        return [{"metadata": {"vendor_name": "X"}, "crop": None}]

    monkeypatch.setattr(lp, "_process_via_text_extraction", fake_text_path)
    monkeypatch.setattr(lp.ocr_settings, "effective_backend", lambda: "paddle_ocr")
    from src.services import receipt_segmenter as rs

    monkeypatch.setattr(rs, "_open_as_rgb", lambda p: Image.new("RGB", (10, 10)))

    out = lp.process_with_segments(
        "x.png",
        skip_segmentation=True,
        overrides={"backend": "apple_vision", "text_model": "m", "structure_backend": "anthropic"},
    )
    assert out[0]["metadata"]["vendor_name"] == "X"
    assert seen == {"backend": "apple_vision", "text_model": "m", "structure_backend": "anthropic"}
    # gone after the call; default path reads the global setting again
    assert lp._ov("backend") is None and lp._cfg_backend() == "paddle_ocr"
    lp.process_with_segments("x.png", skip_segmentation=True)
    assert seen["backend"] == "paddle_ocr" and seen["text_model"] is None


def test_structure_backend_param_overrides_setting(monkeypatch):
    from src.services import text_to_receipt as ttr

    monkeypatch.setattr(settings, "structure_backend", "ollama")
    calls = []
    monkeypatch.setattr(
        ttr,
        "_anthropic_chat",
        lambda prompt, model=None: (calls.append(model), '{"vendor_name":"A"}')[1],
    )
    monkeypatch.setattr(
        ttr, "_ollama_chat", lambda *a, **k: pytest.fail("ollama must not be called")
    )
    out = ttr.structure_text_as_receipt("TOTAL 10", model="claude-x", structure_backend="anthropic")
    assert out["vendor_name"] == "A" and calls == ["claude-x"]
