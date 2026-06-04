"""Tests for the on-demand folder scan + PDF rasterisation.

The OCR pipeline itself (process_with_segments) is mocked so these tests
stay fast and deterministic — we're verifying the *orchestration*: file
discovery, PDF rendering, DB persistence, and the move-to-processed/failed
bookkeeping, not the vision model.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from src.config import settings


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    """Throwaway DB + receipts dir + inbox, all under tmp_path."""
    db_file = tmp_path / "test.db"
    receipts = tmp_path / "receipts"
    inbox = tmp_path / "inbox"
    receipts.mkdir()
    inbox.mkdir()
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "receipts_dir", receipts)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "watch_folder", str(inbox))
    monkeypatch.setattr(settings, "watch_log_path", tmp_path / "watch_log.jsonl")

    from src.db import database

    database.init_db()

    from src.services import folder_watcher

    return {
        "db": database,
        "fw": folder_watcher,
        "inbox": inbox,
        "receipts": receipts,
        "tmp": tmp_path,
    }


def _make_image(path: Path, size=(400, 600), color=(245, 245, 240)):
    Image.new("RGB", size, color=color).save(str(path))


def _stub_segments(monkeypatch, fw, results):
    """Patch the OCR entry point to return a fixed list of items.
    `results` is a list of (metadata_dict, crop_or_None) tuples."""
    from src.services import llm_processor

    def fake(_path, **_kwargs):
        # Swallow kwargs like `skip_segmentation` so the stub stays compatible
        # with both old and new call signatures.
        return [{"metadata": m, "crop": c} for (m, c) in results]

    monkeypatch.setattr(llm_processor, "process_with_segments", fake)


# ─── resolve_inbox / count_pending ──────────────────────────────────────────


def test_resolve_inbox_uses_watch_folder_and_creates_processed(scan_env):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    resolved = fw.resolve_inbox()
    assert resolved == inbox.resolve()
    assert (resolved / "processed").is_dir()


def test_resolve_inbox_defaults_to_data_dir_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "watch_folder", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.services import folder_watcher

    resolved = folder_watcher.resolve_inbox()
    assert resolved == (tmp_path / "inbox").resolve()
    assert resolved.is_dir()


def test_count_pending_ignores_subdirs_and_unsupported(scan_env):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    _make_image(inbox / "a.png")
    _make_image(inbox / "b.jpg")
    (inbox / "notes.txt").write_text("not a receipt")
    # File already in processed/ must not count
    (inbox / "processed").mkdir(exist_ok=True)
    _make_image(inbox / "processed" / "old.png")
    assert fw.count_pending() == 2


# ─── scan_folder happy paths ────────────────────────────────────────────────


def test_scan_imports_images_and_moves_to_processed(scan_env, monkeypatch):
    fw, inbox, receipts = scan_env["fw"], scan_env["inbox"], scan_env["receipts"]
    _make_image(inbox / "r1.png")
    _make_image(inbox / "r2.png")
    _stub_segments(
        monkeypatch,
        fw,
        [
            ({"vendor_name": "Cafe", "total_amount": 12.5, "currency": "CAD"}, None),
        ],
    )

    summary = fw.scan_folder()
    assert summary["scanned"] == 2
    assert summary["files_ok"] == 2
    assert summary["files_failed"] == 0
    assert summary["imported_receipts"] == 2

    # Sources moved out of the inbox root, into processed/
    assert not (inbox / "r1.png").exists()
    assert not (inbox / "r2.png").exists()
    assert (inbox / "processed" / "r1.png").exists()
    assert (inbox / "processed" / "r2.png").exists()

    # DB has 2 rows
    rows = scan_env["db"].get_all_receipts()
    assert len(rows) == 2
    assert {json.loads(r["metadata"])["vendor_name"] for r in rows} == {"Cafe"}


def test_scan_saves_one_row_per_segment_with_crops(scan_env, monkeypatch):
    """A single image that the segmenter splits into 2 receipts → 2 DB rows,
    each with its own _segN.png crop file on disk."""
    fw, inbox, receipts = scan_env["fw"], scan_env["inbox"], scan_env["receipts"]
    _make_image(inbox / "multi.png")
    crop_a = Image.new("RGB", (200, 300), color="white")
    crop_b = Image.new("RGB", (200, 300), color="gray")
    _stub_segments(
        monkeypatch,
        fw,
        [
            ({"vendor_name": "A", "total_amount": 1.0, "currency": "CAD"}, crop_a),
            ({"vendor_name": "B", "total_amount": 2.0, "currency": "CAD"}, crop_b),
        ],
    )

    summary = fw.scan_folder()
    assert summary["imported_receipts"] == 2
    rows = scan_env["db"].get_all_receipts()
    assert len(rows) == 2
    # Per-segment crops saved as _seg1.png / _seg2.png
    seg_files = sorted(p.name for p in receipts.glob("*_seg*.png"))
    assert len(seg_files) == 2
    for r in rows:
        assert Path(r["filepath"]).exists()


def test_scan_queues_multireceipt_page_instead_of_ocr(scan_env, monkeypatch):
    """A page the segmenter splits into >1 receipt is queued for box review —
    not OCR'd. No DB row; the page lands in the review queue; summary counts it."""
    fw, inbox, receipts = scan_env["fw"], scan_env["inbox"], scan_env["receipts"]
    _make_image(inbox / "multi.png")

    from src.services import receipt_segmenter, review_queue

    monkeypatch.setattr(
        receipt_segmenter,
        "segment_with_boxes",
        lambda _p: (
            [{"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": 20, "w": 10, "h": 10}],
            (400, 600),
        ),
    )
    # OCR must not run for a queued page.
    from src.services import llm_processor

    monkeypatch.setattr(
        llm_processor,
        "process_with_segments",
        lambda *_a, **_k: pytest.fail("queued page must not be OCR'd"),
    )

    summary = fw.scan_folder()
    assert summary["queued"] == 1
    assert summary["imported_receipts"] == 0
    assert summary["files_ok"] == 1  # file handled cleanly, just deferred
    assert review_queue.count() == 1
    qitem = review_queue.list_items()[0]
    assert qitem["source"] == "multi.png (scan)"
    assert len(qitem["bboxes"]) == 2
    # No receipts persisted; source moved to processed/
    assert scan_env["db"].get_all_receipts() == []
    assert (inbox / "processed" / "multi.png").exists()


def test_scan_single_receipt_page_still_ocrs(scan_env, monkeypatch):
    """A page with <=1 detected box bypasses the queue and OCRs as before."""
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    _make_image(inbox / "single.png")

    from src.services import receipt_segmenter, review_queue

    monkeypatch.setattr(
        receipt_segmenter,
        "segment_with_boxes",
        lambda _p: ([{"x": 0, "y": 0, "w": 10, "h": 10}], (400, 600)),
    )
    _stub_segments(
        monkeypatch,
        fw,
        [({"vendor_name": "Solo", "total_amount": 3.0, "currency": "CAD"}, None)],
    )

    summary = fw.scan_folder()
    assert summary["queued"] == 0
    assert summary["imported_receipts"] == 1
    assert review_queue.count() == 0
    assert len(scan_env["db"].get_all_receipts()) == 1


def test_scan_empty_inbox_returns_zero(scan_env):
    summary = scan_env["fw"].scan_folder()
    assert summary["scanned"] == 0
    assert summary["imported_receipts"] == 0
    assert summary["results"] == []


def test_scan_skips_unsupported_extensions(scan_env, monkeypatch):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    (inbox / "notes.txt").write_text("nope")
    (inbox / "data.csv").write_text("a,b,c")
    _stub_segments(monkeypatch, fw, [({"vendor_name": "X"}, None)])
    summary = fw.scan_folder()
    assert summary["scanned"] == 0
    # Unsupported files are left untouched in the inbox
    assert (inbox / "notes.txt").exists()
    assert (inbox / "data.csv").exists()


# ─── scan_folder failure handling ───────────────────────────────────────────


def test_scan_moves_failed_file_to_failed_dir(scan_env, monkeypatch):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    _make_image(inbox / "boom.png")

    from src.services import llm_processor

    def explode(_path, **_kwargs):
        raise RuntimeError("ocr blew up")

    monkeypatch.setattr(llm_processor, "process_with_segments", explode)

    summary = fw.scan_folder()
    assert summary["files_failed"] == 1
    assert summary["files_ok"] == 0
    assert not (inbox / "boom.png").exists()
    assert (inbox / "failed" / "boom.png").exists()
    # Nothing persisted
    assert scan_env["db"].get_all_receipts() == []


def test_scan_one_bad_file_does_not_abort_the_batch(scan_env, monkeypatch):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    # Use a distinctive token ("explode") in the failing file's name so the
    # selector can't accidentally match the tmp dir path (pytest names the
    # tmp_path after the test, which would contain generic words like "bad").
    _make_image(inbox / "keepme.png")
    _make_image(inbox / "explode.png")

    from src.services import llm_processor

    def selective(path, **_kwargs):
        from pathlib import Path as _P

        if "explode" in _P(path).name:
            raise RuntimeError("nope")
        return [
            {
                "metadata": {"vendor_name": "Good", "total_amount": 5.0, "currency": "CAD"},
                "crop": None,
            }
        ]

    monkeypatch.setattr(llm_processor, "process_with_segments", selective)

    summary = fw.scan_folder()
    assert summary["files_ok"] == 1
    assert summary["files_failed"] == 1
    assert (inbox / "processed" / "keepme.png").exists()
    assert (inbox / "failed" / "explode.png").exists()
    assert len(scan_env["db"].get_all_receipts()) == 1


def test_scan_writes_watch_log(scan_env, monkeypatch):
    fw, inbox = scan_env["fw"], scan_env["inbox"]
    _make_image(inbox / "logged.png")
    _stub_segments(monkeypatch, fw, [({"vendor_name": "LogCafe"}, None)])
    fw.scan_folder()
    log = fw.get_watch_log(10)
    assert any(e.get("filename") == "logged.png" and e.get("status") == "ok" for e in log)


# ─── PDF rasterisation ──────────────────────────────────────────────────────

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")


def _make_pdf(path: Path, pages: int = 1, size=(400, 600)):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=size[0], height=size[1])
    doc.save(str(path))
    doc.close()


def test_render_pdf_single_page_returns_one_image(tmp_path):
    from src.services.pdf_render import render_pdf_to_images

    pdf = tmp_path / "one.pdf"
    _make_pdf(pdf, pages=1)
    imgs = render_pdf_to_images(str(pdf))
    assert len(imgs) == 1
    assert imgs[0].mode == "RGB"
    assert imgs[0].width > 0 and imgs[0].height > 0


def test_render_pdf_multipage_returns_image_per_page(tmp_path):
    from src.services.pdf_render import render_pdf_to_images

    pdf = tmp_path / "three.pdf"
    _make_pdf(pdf, pages=3)
    imgs = render_pdf_to_images(str(pdf))
    assert len(imgs) == 3


def test_render_pdf_respects_max_pages(tmp_path):
    from src.services.pdf_render import render_pdf_to_images

    pdf = tmp_path / "many.pdf"
    _make_pdf(pdf, pages=5)
    imgs = render_pdf_to_images(str(pdf), max_pages=2)
    assert len(imgs) == 2


def test_render_pdf_dpi_scales_resolution(tmp_path):
    from src.services.pdf_render import render_pdf_to_images

    pdf = tmp_path / "dpi.pdf"
    _make_pdf(pdf, pages=1, size=(100, 100))
    low = render_pdf_to_images(str(pdf), dpi=72)[0]
    high = render_pdf_to_images(str(pdf), dpi=200)[0]
    assert high.width > low.width


def test_is_pdf_helper(tmp_path):
    from src.services.pdf_render import is_pdf

    assert is_pdf("/x/y/receipt.pdf")
    assert is_pdf("/x/y/RECEIPT.PDF")
    assert not is_pdf("/x/y/receipt.png")


def test_scan_queues_every_pdf_page(scan_env, monkeypatch):
    """A PDF in the inbox is rasterised and EVERY page is queued for box
    review (same as a direct PDF upload) — never OCR'd straight through,
    even a single-region page."""
    fw, inbox, receipts = scan_env["fw"], scan_env["inbox"], scan_env["receipts"]
    _make_pdf(inbox / "receipt.pdf", pages=2)
    # OCR must not run for a PDF page under the new behaviour.
    from src.services import llm_processor

    monkeypatch.setattr(
        llm_processor,
        "process_with_segments",
        lambda *_a, **_k: pytest.fail("PDF page must be queued, not OCR'd"),
    )

    from src.services import review_queue

    summary = fw.scan_folder()
    assert summary["files_ok"] == 1
    assert summary["imported_receipts"] == 0
    assert summary["queued"] == 2
    # Both rasterised page PNGs written into receipts_dir
    assert len(list(receipts.glob("*_p*.png"))) == 2
    # Source PDF moved to processed/
    assert (inbox / "processed" / "receipt.pdf").exists()
    # No DB rows; two review-queue items with per-page source labels
    assert scan_env["db"].get_all_receipts() == []
    assert review_queue.count() == 2
    sources = sorted(it["source"] for it in review_queue.list_items())
    assert sources == ["receipt.pdf · p1/2 (scan)", "receipt.pdf · p2/2 (scan)"]
