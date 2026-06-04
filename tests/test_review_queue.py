"""Tests for the pending box-review queue store (data/review_queue.json)."""

import importlib

import pytest

from src.config import settings


@pytest.fixture
def rq(tmp_path, monkeypatch):
    """review_queue bound to a throwaway data_dir."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.services import review_queue

    importlib.reload(review_queue)  # ensure no module-level path caching
    return review_queue


def _box(x=0, y=0, w=10, h=10):
    return {"x": x, "y": y, "w": w, "h": h}


def test_empty_queue_starts_at_zero(rq):
    assert rq.list_items() == []
    assert rq.count() == 0
    assert rq.get(1) is None


def test_add_returns_item_with_incrementing_ids(rq):
    a = rq.add(filename="p1.png", image_w=400, image_h=600, bboxes=[_box(), _box(20)])
    b = rq.add(filename="p2.png", image_w=800, image_h=600, bboxes=[_box()])
    assert a["id"] == 1
    assert b["id"] == 2
    assert a["filename"] == "p1.png"
    assert a["image_w"] == 400 and a["image_h"] == 600
    assert len(a["bboxes"]) == 2
    assert rq.count() == 2


def test_add_persists_optional_fields(rq):
    it = rq.add(
        filename="p.png",
        image_w=1,
        image_h=1,
        bboxes=[],
        source="Recibos.pdf (scan)",
        description="imported from folder scan",
    )
    fetched = rq.get(it["id"])
    assert fetched["source"] == "Recibos.pdf (scan)"
    assert fetched["description"] == "imported from folder scan"


def test_get_returns_none_for_unknown_id(rq):
    rq.add(filename="p.png", image_w=1, image_h=1, bboxes=[])
    assert rq.get(999) is None


def test_remove_drops_item_and_returns_true(rq):
    a = rq.add(filename="a.png", image_w=1, image_h=1, bboxes=[])
    b = rq.add(filename="b.png", image_w=1, image_h=1, bboxes=[])
    assert rq.remove(a["id"]) is True
    assert rq.count() == 1
    assert rq.get(a["id"]) is None
    assert rq.get(b["id"]) is not None


def test_remove_unknown_id_returns_false(rq):
    assert rq.remove(42) is False


def test_ids_keep_incrementing_after_removal(rq):
    a = rq.add(filename="a.png", image_w=1, image_h=1, bboxes=[])
    rq.remove(a["id"])
    c = rq.add(filename="c.png", image_w=1, image_h=1, bboxes=[])
    # next_id is monotonic — a removed id is never reused
    assert c["id"] == 2


def test_state_persists_across_fresh_reads(rq, tmp_path):
    rq.add(filename="p.png", image_w=2, image_h=3, bboxes=[_box()])
    # A brand-new import of the module (same data_dir) sees the saved item.
    from src.services import review_queue as rq2

    assert rq2.count() == 1
    assert rq2.list_items()[0]["filename"] == "p.png"


def test_corrupt_json_falls_back_to_empty(rq, tmp_path):
    (tmp_path / "review_queue.json").write_text("{ not valid json", encoding="utf-8")
    assert rq.list_items() == []
    assert rq.count() == 0
    # And a subsequent add recovers cleanly from id 1.
    it = rq.add(filename="x.png", image_w=1, image_h=1, bboxes=[])
    assert it["id"] == 1
