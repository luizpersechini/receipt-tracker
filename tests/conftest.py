"""Make `src` importable without installing the package."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_ocr_config(monkeypatch):
    """Pin the runtime OCR getters to the `settings` defaults for every test.

    The OCR code reads the backend/models through `ocr_settings.effective_*()`,
    which overlay the persisted `data/ocr_config.json` (written by the sidebar
    picker) on top of settings. That file is real machine state — when it
    drifts (e.g. a saved `apple_vision` backend or an `llama3.1:8b` text model)
    it silently shadows what tests assume and they fail for the wrong reason.
    Binding the getters to `settings` here makes every test deterministic;
    a test that needs a specific backend just monkeypatches `settings`
    (or re-patches the getter).
    """
    from src.config import settings
    from src.services import ocr_settings

    monkeypatch.setattr(ocr_settings, "effective_backend", lambda: settings.ocr_backend)
    monkeypatch.setattr(ocr_settings, "effective_vision_model", lambda: settings.ollama_model)
    monkeypatch.setattr(ocr_settings, "effective_text_model", lambda: settings.ocr_text_model)
