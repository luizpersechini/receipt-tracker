"""
HTML → PDF rendering for archiving Uber email receipts at their original
template fidelity.

WeasyPrint is the workhorse. macOS System Integrity Protection strips the
DYLD_* env vars from system Python before exec, so even with
`brew install pango` installed, weasyprint can't find libgobject / libpango
/ libcairo via their default names because those names don't resolve in the
default dlopen search path on Apple Silicon.

We patch ctypes.util.find_library() to also probe /opt/homebrew/lib and
/usr/local/lib before importing weasyprint. cffi's library resolver falls
back to find_library() when dlopen-by-name fails, so this hooks into the
exact retry path that was missing.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import platform
from typing import Optional

logger = logging.getLogger(__name__)


# ─── macOS Homebrew shim ─────────────────────────────────────────────────────

def _install_homebrew_find_library_shim() -> None:
    """On macOS, extend ctypes.util.find_library() to fall back to Brew paths.

    macOS dyld doesn't search /opt/homebrew/lib (Apple Silicon) or
    /usr/local/lib (Intel) by default, and SIP strips DYLD_LIBRARY_PATH for
    the system Python — so weasyprint's native deps live somewhere dlopen
    won't find them. cffi's fallback path calls find_library; patching it
    lets weasyprint resolve absolute paths to libgobject / libpango etc.
    """
    if platform.system() != "Darwin":
        return

    _orig = ctypes.util.find_library

    def _patched(name: str) -> Optional[str]:
        hit = _orig(name)
        if hit:
            return hit
        for d in ("/opt/homebrew/lib", "/usr/local/lib"):
            for cand in (
                f"{d}/{name}",
                f"{d}/{name}.dylib",
                f"{d}/lib{name}.dylib",
                f"{d}/lib{name}.0.dylib",
                f"{d}/lib{name}.1.dylib",
                f"{d}/lib{name}.2.dylib",
            ):
                if os.path.exists(cand):
                    return cand
        return None

    ctypes.util.find_library = _patched  # type: ignore[assignment]


_install_homebrew_find_library_shim()


# ─── WeasyPrint import (deferred so a missing install is recoverable) ────────

_weasyprint = None
_weasyprint_error: Optional[str] = None


def _load_weasyprint():
    """Lazy-load weasyprint. Returns the module or None on failure."""
    global _weasyprint, _weasyprint_error
    if _weasyprint is not None:
        return _weasyprint
    if _weasyprint_error is not None:
        return None  # already failed; don't keep retrying
    try:
        import weasyprint  # noqa: E402
        _weasyprint = weasyprint
        return weasyprint
    except Exception as e:
        _weasyprint_error = (
            f"WeasyPrint isn't usable: {e}. "
            "On macOS Apple Silicon, run `brew install pango` and "
            "`pip install --user weasyprint>=60`."
        )
        logger.warning("WeasyPrint unavailable: %s", e)
        return None


def is_available() -> bool:
    return _load_weasyprint() is not None


def render_html_to_pdf(html: str, *, base_url: Optional[str] = None) -> bytes:
    """
    Render an HTML string to a PDF byte string.

    `base_url` is used by WeasyPrint to resolve relative URLs (images,
    stylesheets). For Uber emails it can stay None — assets are absolute.

    Raises RuntimeError if WeasyPrint isn't installed / can't load native
    libs. Caller decides whether to surface the error or skip silently.
    """
    weasyprint = _load_weasyprint()
    if weasyprint is None:
        raise RuntimeError(_weasyprint_error or "WeasyPrint unavailable")
    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()
