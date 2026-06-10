"""Tests for marking a statement complete (archived) and reopening it."""

import pytest
from fastapi.testclient import TestClient

from src.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "receipts_dir", tmp_path / "receipts")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    (tmp_path / "receipts").mkdir()
    (tmp_path / "data").mkdir()

    from src.db import database

    database.init_db()
    from src.main import app

    return TestClient(app), database


def _make_statement(db, n=2):
    return db.save_statement(
        "itau.csv", "/x/itau.csv", [{"amount": "10", "date": "2026-04-01"}] * n
    )


# ─── DB helper ───────────────────────────────────────────────────────────────


def test_archived_column_defaults_to_zero(client):
    _, db = client
    sid = _make_statement(db)
    s = db.get_statement_by_id(sid)
    assert s["archived"] == 0


def test_set_statement_archived_toggles(client):
    _, db = client
    sid = _make_statement(db)
    assert db.set_statement_archived(sid, True) is True
    assert db.get_statement_by_id(sid)["archived"] == 1
    assert db.set_statement_archived(sid, False) is True
    assert db.get_statement_by_id(sid)["archived"] == 0


def test_set_statement_archived_unknown_id(client):
    _, db = client
    assert db.set_statement_archived(99999, True) is False


# ─── Endpoint ────────────────────────────────────────────────────────────────


def test_archive_endpoint_marks_complete(client):
    c, db = client
    sid = _make_statement(db)
    r = c.post(f"/api/statements/{sid}/archive", json={"archived": True})
    assert r.status_code == 200
    assert r.json() == {"success": True, "archived": True}
    assert db.get_statement_by_id(sid)["archived"] == 1
    # default body archives
    db.set_statement_archived(sid, False)
    assert c.post(f"/api/statements/{sid}/archive", json={}).json()["archived"] is True


def test_archive_endpoint_reopen(client):
    c, db = client
    sid = _make_statement(db)
    db.set_statement_archived(sid, True)
    r = c.post(f"/api/statements/{sid}/archive", json={"archived": False})
    assert r.status_code == 200 and r.json()["archived"] is False
    assert db.get_statement_by_id(sid)["archived"] == 0


def test_archive_endpoint_unknown_statement_404(client):
    c, _ = client
    assert c.post("/api/statements/424242/archive", json={"archived": True}).status_code == 404


def test_archived_statement_still_listed_by_get(client):
    """Archiving hides it in the UI (frontend filter), but the API still
    returns it with archived=1 — the 'Completed/All' views need it."""
    c, db = client
    sid = _make_statement(db)
    c.post(f"/api/statements/{sid}/archive", json={"archived": True})
    rows = c.get("/api/statements").json()["statements"]
    row = next(s for s in rows if s["id"] == sid)
    assert row["archived"] == 1


def test_delete_statement_releases_matched_receipts(client):
    """Regression: deleting a statement removed its matches but left the
    linked receipts 'matched' — orphaned and hidden by the default filter."""
    _, db = client
    sid = _make_statement(db)
    rid = db.save_receipt("r.jpg", "/x/r.jpg", {"total_amount": 10.0}, "")
    db.save_match(rid, sid, {"amount": 10.0}, 0.9, match_kind="auto", transaction_index=0)
    db.update_receipt_match_status(rid, "matched")

    db.delete_statement(sid)

    r = db.get_receipt_by_id(rid)
    assert r["match_status"] == "pending"


def test_delete_statement_keeps_receipts_matched_elsewhere(client):
    _, db = client
    sid1 = _make_statement(db)
    sid2 = _make_statement(db)
    rid = db.save_receipt("r.jpg", "/x/r.jpg", {"total_amount": 10.0}, "")
    db.save_match(rid, sid1, {"amount": 10.0}, 0.9, match_kind="auto", transaction_index=0)
    db.save_match(rid, sid2, {"amount": 10.0}, 0.9, match_kind="manual", transaction_index=1)
    db.update_receipt_match_status(rid, "matched")

    db.delete_statement(sid1)

    r = db.get_receipt_by_id(rid)
    assert r["match_status"] == "matched"  # still matched on sid2


def test_delete_receipt_removes_unreferenced_file(client):
    _, db = client
    f = settings.receipts_dir / "r1.jpg"
    f.write_bytes(b"x")
    rid = db.save_receipt("r1.jpg", str(f), {}, "")
    db.delete_receipt(rid)
    assert not f.exists()


def test_delete_receipt_keeps_file_shared_by_other_rows(client):
    _, db = client
    f = settings.receipts_dir / "page.png"
    f.write_bytes(b"x")
    r1 = db.save_receipt("page_seg1.png", str(f), {}, "")
    db.save_receipt("page_seg2.png", str(f), {}, "")
    db.delete_receipt(r1)
    assert f.exists()


def test_delete_statement_removes_its_file(client):
    _, db = client
    (settings.data_dir / "statements").mkdir(parents=True, exist_ok=True)
    f = settings.data_dir / "statements" / "stmt.csv"
    f.write_bytes(b"x")
    sid = db.save_statement("stmt.csv", str(f), [{"amount": "10", "date": "2026-04-01"}])
    db.delete_statement(sid)
    assert not f.exists()
