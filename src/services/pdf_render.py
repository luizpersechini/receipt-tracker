"""
Rasterise PDF pages to PIL images so the OCR pipeline (which only speaks
pixels) can ingest PDF receipts.

Uses PyMuPDF (fitz) — already a transitive dependency. Lazy import keeps it
optional at module-import time so environments without it (or tests that
never touch a PDF) don't pay for it.
"""

from __future__ import annotations

import logging
from typing import List

from PIL import Image

logger = logging.getLogger(__name__)

# A receipt PDF is almost always one page. Cap pages so a pathological
# multi-hundred-page PDF can't wedge the OCR queue.
MAX_PAGES = 10
DEFAULT_DPI = 200


def is_pdf(path: str) -> bool:
    return str(path).lower().endswith(".pdf")


def render_pdf_to_images(
    path: str, *, dpi: int = DEFAULT_DPI, max_pages: int = MAX_PAGES
) -> List[Image.Image]:
    """
    Render up to `max_pages` pages of a PDF to RGB PIL images at `dpi`.

    Returns a list (one image per page). Raises RuntimeError if PyMuPDF is
    unavailable or the file can't be opened, so the caller can record a
    per-file error rather than crashing a batch.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PyMuPDF (fitz) is required to OCR PDFs — `pip install pymupdf`") from e

    zoom = dpi / 72.0  # 72 is the PDF's native point-per-inch
    matrix = fitz.Matrix(zoom, zoom)

    images: List[Image.Image] = []
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise RuntimeError(f"Could not open PDF {path}: {e}") from e

    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                logger.warning("PDF %s has >%d pages; truncating", path, max_pages)
                break
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(img)
    finally:
        doc.close()

    if not images:
        raise RuntimeError(f"PDF {path} produced no pages")
    return images
