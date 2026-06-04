"""OCR model/backend selection endpoints — back the sidebar picker.

Lets the UI see which Ollama models are installed and choose the vision/OCR
model (and backend) at runtime, without editing .env or restarting.
"""

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from ..config import settings
from ..services import ocr_settings
from ..services.llm_processor import GB, _is_vision_allowlisted, _list_available_models

router = APIRouter(prefix="/api", tags=["ocr"])
logger = logging.getLogger(__name__)


def _ollama_reachable() -> bool:
    try:
        import requests

        host = (settings.ollama_host or "http://localhost:11434").rstrip("/")
        requests.get(f"{host}/api/tags", timeout=2).raise_for_status()
        return True
    except Exception:
        return False


@router.get("/ocr/config")
async def get_ocr_config():
    """Current OCR selection + installed Ollama models (with vision flag)."""
    cfg = await asyncio.to_thread(ocr_settings.get_config)
    available = await asyncio.to_thread(_list_available_models)
    reachable = bool(available) or await asyncio.to_thread(_ollama_reachable)
    cap = settings.ocr_max_model_gb
    models = [
        {
            "name": name,
            "size_gb": round(size / GB, 1),
            "vision": _is_vision_allowlisted(name),
            "over_cap": size > cap * GB,
        }
        for name, size in sorted(available.items())
    ]
    return JSONResponse(
        {
            **cfg,
            "ollama_reachable": reachable,
            "max_model_gb": cap,
            "models": models,
        }
    )


@router.put("/ocr/config")
async def put_ocr_config(patch: dict = Body(...)):
    """Set the OCR backend / vision model / text model. Persisted; applies to
    the next OCR run (no restart)."""
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    try:
        cfg = await asyncio.to_thread(ocr_settings.set_config, patch)
    except Exception as e:
        logger.exception("put_ocr_config failed")
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(cfg)
