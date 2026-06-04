"""Tests for the box-review queue endpoints + the PDF/scan queuing behaviour.

OCR and PDF segmentation are stubbed — we verify the orchestration: queuing
every PDF page on upload, committing user-confirmed boxes (one receipt per
box) while draining the queue, and discarding a queued page.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired to a throwaway DB + receipts dir + data dir."""
    db_file = tmp_path / "test.db"
    receipts = tmp_path / "receipts"
    data = tmp_path / "data"
    receipts.mkdir()
    data.mkdir()
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "receipts_dir", receipts)
    monkeypatch.setattr(settings, "data_dir", data)

    from src.db import database

    database.init_db()

    from src.main import app

    return TestClient(app), {"receipts": receipts, "data": data, "db": database}


def _page(receipts: Path, name="receipt_x_p1.png", size=(400, 600)):
    p = receipts / name
    Image.new("RGB", size, color=(240, 240, 235)).save(str(p))
    return p


def _stub_ocr(monkeypatch, n_vendor="Cafe"):
    """ocr_crops → one metadata row per crop handed in."""
    from src.routers import receipts as receipts_router

    def fake(crops):
        return [
            {
                "metadata": {
                    "vendor_name": f"{n_vendor}{i}",
                    "total_amount": 10 + i,
                    "currency": "CAD",
                },
                "crop": c,
            }
            for i, c in enumerate(crops)
        ]

    monkeypatch.setattr(receipts_router, "ocr_crops", fake)


# ─── GET /api/review-queue ──────────────────────────────────────────────────


def test_get_queue_empty(client):
    c, _ = client
    r = c.get("/api/review-queue")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "count": 0}


def test_get_queue_returns_shape(client):
    c, env = client
    from src.services import review_queue

    review_queue.add(
        filename="receipt_x_p1.png",
        image_w=400,
        image_h=600,
        bboxes=[{"x": 1, "y": 2, "w": 3, "h": 4}],
        source="Recibos.pdf · p1/2",
        description="hi",
    )
    body = c.get("/api/review-queue").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["image_url"] == "/api/preview/receipt_x_p1.png"
    assert item["image_size"] == {"w": 400, "h": 600}
    assert item["source"] == "Recibos.pdf · p1/2"
    assert item["description"] == "hi"
    assert len(item["bboxes"]) == 1


# ─── POST /api/review-queue/{id}/commit ─────────────────────────────────────


def test_commit_ocrs_boxes_and_drops_item(client, monkeypatch):
    c, env = client
    _stub_ocr(monkeypatch)
    _page(env["receipts"], "receipt_x_p1.png")
    from src.services import review_queue

    it = review_queue.add(filename="receipt_x_p1.png", image_w=400, image_h=600, bboxes=[])
    r = c.post(
        f"/api/review-queue/{it['id']}/commit",
        json={
            "bboxes": [{"x": 0, "y": 0, "w": 200, "h": 300}, {"x": 0, "y": 300, "w": 200, "h": 300}]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["count"] == 2  # one receipt per box
    # Item drained from the queue
    assert review_queue.get(it["id"]) is None
    # Two DB rows persisted
    assert len(env["db"].get_all_receipts()) == 2


def test_commit_unknown_item_404(client, monkeypatch):
    c, _ = client
    _stub_ocr(monkeypatch)
    r = c.post("/api/review-queue/999/commit", json={"bboxes": [{"x": 0, "y": 0, "w": 1, "h": 1}]})
    assert r.status_code == 404


def test_commit_without_boxes_400(client):
    c, env = client
    from src.services import review_queue

    it = review_queue.add(filename="p.png", image_w=1, image_h=1, bboxes=[])
    r = c.post(f"/api/review-queue/{it['id']}/commit", json={"bboxes": []})
    assert r.status_code == 400
    # Item stays queued — nothing was committed.
    assert review_queue.get(it["id"]) is not None


# ─── DELETE /api/review-queue/{id} ──────────────────────────────────────────


def test_discard_removes_item_and_page_file(client):
    c, env = client
    from src.services import review_queue

    page = _page(env["receipts"], "receipt_x_p1.png")
    it = review_queue.add(filename="receipt_x_p1.png", image_w=400, image_h=600, bboxes=[])
    r = c.delete(f"/api/review-queue/{it['id']}")
    assert r.status_code == 204
    assert review_queue.get(it["id"]) is None
    assert not page.exists()  # best-effort page cleanup ran


def test_discard_unknown_item_404(client):
    c, _ = client
    assert c.delete("/api/review-queue/123").status_code == 404


# ─── PDF upload queues every page (no OCR) ──────────────────────────────────

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")


def _pdf_bytes(pages=2, size=(400, 600)):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=size[0], height=size[1])
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_preview_queues_every_page(client, monkeypatch):
    c, env = client
    # Segmenter returns 2 boxes — but the PDF path queues regardless of count.
    from src.routers import receipts as receipts_router

    monkeypatch.setattr(
        receipts_router,
        "segment_with_boxes",
        lambda _p: (
            [{"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": 20, "w": 10, "h": 10}],
            (400, 600),
        ),
    )
    # Guard: OCR must NOT run on the upload path.
    monkeypatch.setattr(
        receipts_router,
        "ocr_crops",
        lambda *_a, **_k: pytest.fail("OCR must not run during PDF preview"),
    )

    r = c.post(
        "/api/upload/receipt/preview",
        files={"file": ("Recibos.pdf", _pdf_bytes(pages=3), "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == 3
    assert body["review_queued"] == 3
    # Queue now holds 3 pages; no receipts saved yet.
    assert c.get("/api/review-queue").json()["count"] == 3
    assert env["db"].get_all_receipts() == []
