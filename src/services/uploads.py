"""Streaming upload persistence shared by the upload endpoints.

Three guarantees the bare `shutil.copyfileobj(file.file, open(...))` pattern
lacked:
- the multipart filename is reduced to its basename (client-controlled; a
  path separator would escape the data dir, or 500 on open);
- `settings.max_upload_mb` is actually enforced (413, partial file removed);
- the disk write runs off the event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException, UploadFile

from ..config import settings

_CHUNK = 1024 * 1024


class _TooLarge(Exception):
    pass


def safe_name(filename) -> str:
    """Basename of a client-supplied filename, never empty."""
    name = Path(str(filename or "upload")).name.strip()
    return name or "upload"


async def save_upload(file: UploadFile, dest: Path) -> None:
    """Stream the upload to `dest`, enforcing the size cap off the loop."""
    limit = int(settings.max_upload_mb) * 1024 * 1024

    def _write():
        size = 0
        try:
            with open(dest, "wb") as out:
                while True:
                    chunk = file.file.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise _TooLarge()
                    out.write(chunk)
        except _TooLarge:
            dest.unlink(missing_ok=True)
            raise

    try:
        await asyncio.to_thread(_write)
    except _TooLarge:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {settings.max_upload_mb} MB upload limit",
        )
