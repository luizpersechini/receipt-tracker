"""The startup warmup must load the Ollama model with the same context cap
the real OCR calls use. A bare load takes the model's default window
(llama3.1: 131072 tokens → ~23GB of KV cache), which on a 24GB Mac swapped
the whole machine at app start."""

from src.config import settings
from src.services import llm_processor as lp


def test_warmup_caps_context_window(monkeypatch):
    monkeypatch.setattr(lp.ocr_settings, "effective_backend", lambda: "apple_vision")
    monkeypatch.setattr(lp.ocr_settings, "effective_text_model", lambda: "llama3.1:8b")
    monkeypatch.setattr(settings, "structure_backend", "ollama")

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(lp.requests, "post", fake_post)

    assert lp.warmup() == "llama3.1:8b"
    assert captured["url"].endswith("/api/generate")
    payload = captured["payload"]
    assert payload["model"] == "llama3.1:8b"
    assert payload["options"]["num_ctx"] == settings.ocr_num_ctx
    assert settings.ocr_num_ctx <= 16384, "cap must stay small — this is a receipt OCR"
