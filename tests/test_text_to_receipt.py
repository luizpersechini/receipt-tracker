"""Tests for the OCR-text → receipt-metadata structuring helper.

We mock the Ollama HTTP call so the tests are fast and deterministic. The
structuring service's job is to call the model and parse its response;
those are the units under test.
"""

import json

from src.config import settings
from src.services import text_to_receipt as t2r

# effective_text_model() is pinned to the settings default globally by the
# conftest `_isolate_ocr_config` fixture, so a saved picker choice in
# data/ocr_config.json can't shadow the model these tests assert on.


def _fake_response(content: str):
    """Minimal Ollama /api/chat response shape."""

    class _R:
        def __init__(self, c):
            self._c = c

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": self._c}}

    return _R(content)


def test_empty_text_returns_error_dict():
    out = t2r.structure_text_as_receipt("")
    assert out.get("error")


def test_parses_clean_json_response(monkeypatch):
    captured = {}

    def _fake_chat(model, prompt):
        captured["model"] = model
        captured["prompt"] = prompt
        return {
            "message": {
                "content": json.dumps(
                    {
                        "vendor_name": "Trees Coffee",
                        "total_amount": "13.97",
                        "currency": "CAD",
                        "date": "2026-03-10",
                        "city": "Vancouver",
                        "country": "CA",
                        "category": "meals",
                    }
                )
            }
        }

    monkeypatch.setattr(t2r, "_ollama_chat", _fake_chat)

    out = t2r.structure_text_as_receipt("TREES COFFEE GASTOWN\nTotal $13.97\nVancouver")
    assert out["vendor_name"] == "Trees Coffee"
    assert out["country"] == "CA"
    assert "TREES COFFEE" in captured["prompt"]
    assert captured["model"] == "llama3.2:3b"


def test_recovers_when_model_wraps_json_in_text(monkeypatch):
    """Some models return 'Here is the JSON: {...} hope that helps' even
    when format=json is requested. Make sure we still extract the object."""

    def _fake_chat(*_a, **_k):
        return {
            "message": {
                "content": 'Sure! Here is the structured data: {"vendor_name":"X","total_amount":"5"} -- done.'
            }
        }

    monkeypatch.setattr(t2r, "_ollama_chat", _fake_chat)

    out = t2r.structure_text_as_receipt("X 5")
    assert out["vendor_name"] == "X"


def test_returns_error_on_unparseable_response(monkeypatch):
    def _fake_chat(*_a, **_k):
        return {"message": {"content": "no JSON here, sorry"}}

    monkeypatch.setattr(t2r, "_ollama_chat", _fake_chat)

    out = t2r.structure_text_as_receipt("foo")
    assert out.get("error")


def test_handles_request_timeout(monkeypatch):
    import requests

    def _fake_chat(*_a, **_k):
        raise requests.Timeout("simulated")

    monkeypatch.setattr(t2r, "_ollama_chat", _fake_chat)

    out = t2r.structure_text_as_receipt("foo")
    assert "timed out" in out.get("error", "").lower()


# ─── structuring backend = anthropic ────────────────────────────────────────


def test_anthropic_backend_routes_and_parses(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "structure_backend", "anthropic")
    captured = {}

    def _fake_anthropic(prompt, *, model=None):
        captured["prompt"] = prompt
        captured["model"] = model
        return json.dumps({"vendor_name": "Padaria", "total_amount": "70,00", "currency": "BRL"})

    def _no_ollama(*_a, **_k):
        raise AssertionError("Ollama must not be called when backend=anthropic")

    monkeypatch.setattr(t2r, "_anthropic_chat", _fake_anthropic)
    monkeypatch.setattr(t2r, "_ollama_chat", _no_ollama)

    out = t2r.structure_text_as_receipt("PADARIA\nTotal R$ 70,00")
    assert out["vendor_name"] == "Padaria"
    assert out["currency"] == "BRL"
    assert "PADARIA" in captured["prompt"]
    assert captured["model"] is None  # falls back to settings.anthropic_model


def test_anthropic_backend_errors_are_caught(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "structure_backend", "anthropic")

    def _boom(prompt, *, model=None):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(t2r, "_anthropic_chat", _boom)

    out = t2r.structure_text_as_receipt("foo")
    assert "anthropic structuring failed" in out.get("error", "")
    assert "401" in out["error"]


def test_uses_custom_model_when_passed(monkeypatch):
    captured = {}

    def _fake_chat(model, _prompt):
        captured["model"] = model
        return {"message": {"content": '{"vendor_name":"X","total_amount":"1"}'}}

    monkeypatch.setattr(t2r, "_ollama_chat", _fake_chat)

    t2r.structure_text_as_receipt("foo", model="qwen2.5:3b")
    assert captured["model"] == "qwen2.5:3b"


# ─── Long receipts: scaled timeout + capped item list ───────────────────────


def test_structuring_timeout_scales_with_text(monkeypatch):
    monkeypatch.setattr(settings, "ocr_timeout_seconds", 120.0)
    assert t2r._structuring_timeout(500) == 120.0
    assert t2r._structuring_timeout(2000) == 120.0
    assert t2r._structuring_timeout(2001) == 180.0
    assert t2r._structuring_timeout(6590) == 120.0 + 60.0 * 4  # the hotel-buffet case


def test_long_receipt_prompt_caps_items_and_scales_timeout(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"vendor_name": "X"}'}}

    def fake_post(url, json=None, timeout=None):
        seen["prompt"] = json["messages"][0]["content"]
        seen["timeout"] = timeout
        return R()

    monkeypatch.setattr(t2r.requests, "post", fake_post)
    monkeypatch.setattr(settings, "ocr_timeout_seconds", 120.0)

    short = "LOJA X\nCAFE 4,00\nTOTAL 4,00"
    assert t2r.structure_text_as_receipt(short, model="m")["vendor_name"] == "X"
    assert "at most" not in seen["prompt"] and seen["timeout"] == 120.0

    long = "\n".join(["HOTEL"] + [f"{i} Self Service 0.5 KG x R$ 105.00  R$ 52,50" for i in range(70)])
    t2r.structure_text_as_receipt(long, model="m")
    assert f"list at most {t2r.LONG_RECEIPT_MAX_ITEMS} items" in seen["prompt"]
    assert "71 lines" in seen["prompt"]
    assert seen["timeout"] > 120.0
