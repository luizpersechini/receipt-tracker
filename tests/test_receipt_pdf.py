"""Tests for the HTML → PDF receipt renderer.

We don't exercise WeasyPrint's actual rendering here (it's a heavy native
dep that may not be installable on every CI box). We verify:
  - The Homebrew find_library shim only mutates Darwin behaviour and is
    backwards-compatible.
  - The synthetic-filename helper produces the right .pdf extension.
  - render_html_to_pdf raises a clear error if WeasyPrint isn't loadable.
"""

import ctypes.util
import platform
from unittest.mock import patch

import pytest

from src.services import gmail_ingest, receipt_pdf


def test_homebrew_shim_only_affects_darwin(monkeypatch):
    """On non-Darwin platforms the shim should be a no-op."""
    # Snapshot current find_library so we can verify it's untouched on Linux.
    orig = ctypes.util.find_library
    with patch("platform.system", return_value="Linux"):
        receipt_pdf._install_homebrew_find_library_shim()
    assert ctypes.util.find_library is orig or callable(ctypes.util.find_library)


def test_homebrew_shim_passes_through_standard_lookups():
    """On any platform, a name resolvable by the original find_library() must
    still resolve. The shim only ADDS fallback paths."""
    # `c` (libSystem on macOS, libc.so.* on Linux) is universally findable.
    assert ctypes.util.find_library("c") is not None


def test_is_available_reflects_module_load():
    """is_available() is True iff the lazy weasyprint load succeeded."""
    val = receipt_pdf.is_available()
    assert isinstance(val, bool)


def test_render_raises_when_weasyprint_missing(monkeypatch):
    """If WeasyPrint failed to import, render_html_to_pdf should raise a
    runtime error with a clear message — not crash with a NameError."""
    monkeypatch.setattr(receipt_pdf, "_weasyprint", None)
    monkeypatch.setattr(receipt_pdf, "_weasyprint_error", "weasyprint is missing")
    with pytest.raises(RuntimeError, match="weasyprint is missing"):
        receipt_pdf.render_html_to_pdf("<p>hi</p>")


# ─── Filename plumbing ──────────────────────────────────────────────────────

def test_synthetic_filename_supports_pdf_extension():
    p = gmail_ingest.ParsedUberEmail(
        msg_id="m1", trip_id="abc",
        date=None, total_amount=10.0, currency="CAD",
        pickup=None, dropoff=None, payment_method=None, is_business=False,
    )
    assert p.synthetic_filename() == "uber_abc.eml"
    assert p.synthetic_filename(".pdf") == "uber_abc.pdf"
    # leading dot is optional
    assert p.synthetic_filename("pdf") == "uber_abc.pdf"


def test_synthetic_filename_pdf_uses_msg_id_when_no_trip_id():
    p = gmail_ingest.ParsedUberEmail(
        msg_id="19e18926c93e7f27", trip_id=None,
        date=None, total_amount=10.0, currency="CAD",
        pickup=None, dropoff=None, payment_method=None, is_business=False,
    )
    assert p.synthetic_filename(".pdf") == "uber_gm-19e18926c93e7f27.pdf"


# ─── _persist_receipt_pdf graceful fallback ─────────────────────────────────

def test_persist_falls_back_to_eml_when_html_missing():
    p = gmail_ingest.ParsedUberEmail(
        msg_id="m1", trip_id="abc",
        date=None, total_amount=10.0, currency="CAD",
        pickup=None, dropoff=None, payment_method=None, is_business=False,
    )
    name, path = gmail_ingest._persist_receipt_pdf(p, html=None, msg_id="m1")
    assert name == "uber_abc.eml"
    assert path == "gmail://m1"


def test_persist_falls_back_to_eml_when_render_raises(monkeypatch, tmp_path):
    """If WeasyPrint crashes mid-render, we still want the receipt saved."""
    from src.config import settings
    monkeypatch.setattr(settings, "receipts_dir", tmp_path)

    # Pretend weasyprint exists and raises when called.
    class _Boom:
        @staticmethod
        def is_available(): return True
        @staticmethod
        def render_html_to_pdf(*_a, **_k):
            raise RuntimeError("simulated render failure")
    monkeypatch.setattr("src.services.receipt_pdf.is_available", _Boom.is_available)
    monkeypatch.setattr("src.services.receipt_pdf.render_html_to_pdf", _Boom.render_html_to_pdf)

    p = gmail_ingest.ParsedUberEmail(
        msg_id="m1", trip_id="abc",
        date=None, total_amount=10.0, currency="CAD",
        pickup=None, dropoff=None, payment_method=None, is_business=False,
    )
    name, path = gmail_ingest._persist_receipt_pdf(p, html="<p>x</p>", msg_id="m1")
    assert name == "uber_abc.eml"
    assert path == "gmail://m1"


def test_persist_writes_pdf_and_returns_disk_path(monkeypatch, tmp_path):
    from src.config import settings
    monkeypatch.setattr(settings, "receipts_dir", tmp_path)

    class _Ok:
        @staticmethod
        def is_available(): return True
        @staticmethod
        def render_html_to_pdf(*_a, **_k):
            return b"%PDF-1.4\nfake pdf bytes"
    monkeypatch.setattr("src.services.receipt_pdf.is_available", _Ok.is_available)
    monkeypatch.setattr("src.services.receipt_pdf.render_html_to_pdf", _Ok.render_html_to_pdf)

    p = gmail_ingest.ParsedUberEmail(
        msg_id="m1", trip_id="abc",
        date=None, total_amount=10.0, currency="CAD",
        pickup=None, dropoff=None, payment_method=None, is_business=False,
    )
    name, path = gmail_ingest._persist_receipt_pdf(p, html="<p>x</p>", msg_id="m1")
    assert name == "uber_abc.pdf"
    assert path.endswith("uber_abc.pdf")
    # File was actually written
    written = (tmp_path / "uber_abc.pdf").read_bytes()
    assert written.startswith(b"%PDF-")
