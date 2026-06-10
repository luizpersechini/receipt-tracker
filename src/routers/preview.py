"""Receipt image preview endpoint (handles HEIC/HEIF conversion on the fly)."""

import asyncio
import io
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image

from ..config import settings

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

router = APIRouter(prefix="/api/preview", tags=["preview"])

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
_HEIF_SUFFIXES = (".heic", ".heif", ".avif")


@router.get("/{filename}")
async def preview_receipt(filename: str):
    # Basename only — never let an encoded separator walk out of the dir.
    filepath = settings.receipts_dir / Path(filename).name
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = filepath.suffix.lower()
    if suffix in _HEIF_SUFFIXES:
        # A 12MP HEIC decode+re-encode is ~1s of CPU; the receipts table
        # requests many previews at once — keep it off the event loop.
        def _convert() -> bytes:
            img = Image.open(str(filepath)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            return buf.getvalue()

        content = await asyncio.to_thread(_convert)
        return Response(content=content, media_type="image/jpeg")

    return FileResponse(
        str(filepath),
        media_type=_MEDIA_TYPES.get(suffix, "application/octet-stream"),
    )
