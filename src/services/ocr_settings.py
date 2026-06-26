"""Runtime OCR backend/model selection, overlaid on the .env `settings`.

By default the OCR backend and models come from the environment (config.py),
which only change on restart. The sidebar model picker overrides them at
runtime — persisted to data/ocr_config.json — so you can switch the
vision/OCR model without editing .env or restarting. The OCR code reads the
effective values through this module.

  backend       — "ollama_vision" (one vision model reads + structures),
                  "apple_vision" (macOS on-device text extraction + a text
                  model), or "paddle_ocr" (cross-platform PaddleOCR text
                  extraction + a text model).
  vision_model  — the Ollama vision model used by the ollama_vision backend.
  text_model    — the Ollama text model used by apple_vision/paddle_ocr to
                  structure the extracted text.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import settings
from .json_store import locked, write_json

logger = logging.getLogger(__name__)

VALID_BACKENDS = {"ollama_vision", "apple_vision", "paddle_ocr"}


def _path() -> Path:
    return settings.data_dir / "ocr_config.json"


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("ocr_config.json unreadable (%s)", e)
        return {}


def _write(data: dict) -> None:
    write_json(_path(), data)


def effective_backend() -> str:
    b = _read().get("backend")
    return b if b in VALID_BACKENDS else settings.ocr_backend


def effective_vision_model() -> str:
    return (_read().get("vision_model") or "").strip() or settings.ollama_model


def effective_text_model() -> str:
    return (_read().get("text_model") or "").strip() or settings.ocr_text_model


def get_config() -> dict:
    return {
        "backend": effective_backend(),
        "vision_model": effective_vision_model(),
        "text_model": effective_text_model(),
    }


@locked(_path)
def set_config(patch: dict) -> dict:
    """Persist an override. Only known keys with sane values are kept; blank
    values fall back to the .env default."""
    data = _read()
    if isinstance(patch, dict):
        if "backend" in patch:
            b = (patch.get("backend") or "").strip()
            if b in VALID_BACKENDS:
                data["backend"] = b
        for key in ("vision_model", "text_model"):
            if key in patch:
                data[key] = (patch.get(key) or "").strip()
    _write({k: v for k, v in data.items() if v})
    return get_config()
