"""Tests for editing receipt membership on an issued reimbursement report.

Covers the store-level mutations (`update_membership`, `reissue`) and the
endpoint behaviour: the status truth-table (add/remove on completed vs. not),
single-membership rejection, re-issue (version bump / stale clear / empty 400),
and the delete-reverts-a-completed-report fix. Plus one end-to-end flow.

Invariant under test: a receipt is `reimbursed` iff it is a member of a
*completed* report, and belongs to at most one report.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import settings


# ─── Store-level (no DB / no rendering) ─────────────────────────────────────


@pytest.fixture
def hist_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", tmp_path / "reports")
    from src.services import report_history

    return report_history


def test_record_initializes_editing_fields(hist_env):
    rec = hist_env.record(b"%PDF", kind="reimbursement", title="R", receipt_ids=[1, 2])
    assert rec["version"] == 1
    assert rec["pdf_stale"] is False
    assert rec["updated_at"] == rec["created_at"]


def test_norm_defaults_legacy_records(hist_env):
    # A record written before this feature (no version/pdf_stale) reads back
    # with sane defaults.
    import json

    rh = hist_env
    rh.record(b"%PDF", kind="reimbursement", title="R", created_at="2026-05-01T10:00:00")
    p = rh._index_path()
    raw = json.loads(p.read_text())
    for r in raw:
        r.pop("version", None)
        r.pop("pdf_stale", None)
        r.pop("updated_at", None)
    p.write_text(json.dumps(raw))

    got = rh.get_record(1)
    assert got["version"] == 1
    assert got["pdf_stale"] is False
    assert got["updated_at"] == "2026-05-01T10:00:00"


def test_update_membership_mutates_and_marks_stale(hist_env):
    rh = hist_env
    rec = rh.record(b"%PDF", kind="reimbursement", title="R", receipt_ids=[1, 2, 3])
    out = rh.update_membership(rec["id"], add=[4], remove=[2])
    assert out["receipt_ids"] == [1, 3, 4]  # 2 removed, 4 appended, order preserved
    assert out["pdf_stale"] is True
    assert out["version"] == 1  # version only bumps on re-issue
    # Adding an existing member is a no-op (deduped).
    again = rh.update_membership(rec["id"], add=[1], remove=[])
    assert again["receipt_ids"] == [1, 3, 4]
    assert rh.update_membership(9999, add=[1], remove=[]) is None


def test_reissue_bumps_version_clears_stale_overwrites_file(hist_env):
    rh = hist_env
    rec = rh.record(b"%PDF-OLD", kind="reimbursement", title="R", receipt_ids=[1])
    rh.update_membership(rec["id"], add=[2], remove=[])  # sets pdf_stale
    out = rh.reissue(rec["id"], b"%PDF-NEW-LONGER")
    assert out["version"] == 2
    assert out["pdf_stale"] is False
    assert out["size"] == len(b"%PDF-NEW-LONGER")
    assert rh.read_pdf(rec["id"]) == b"%PDF-NEW-LONGER"
    assert rh.reissue(9999, b"x") is None


# ─── Endpoint-level (DB + TestClient) ───────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "receipts_dir", tmp_path / "receipts")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "reports_dir", tmp_path / "reports")
    (tmp_path / "receipts").mkdir()
    (tmp_path / "data").mkdir()

    from src.db import database
    from src.services import report_history

    database.init_db()
    from src.main import app

    return TestClient(app), database, report_history


def _reimb(db, vendor="Padaria", total="70,00", currency="BRL", date="2026-05-01") -> int:
    rid = db.save_receipt(
        f"{vendor}.png",
        f"/x/{vendor}.png",
        {"vendor_name": vendor, "total_amount": total, "currency": currency, "date": date},
        description=vendor,
    )
    db.update_receipt_expense_type(rid, "reimbursement")
    return rid


def _report(rh, ids, *, completed=False):
    rec = rh.record(b"%PDF", kind="reimbursement", title="Reimb", receipt_ids=list(ids))
    if completed:
        rh.set_completed(rec["id"], True)
    return rec["id"]


def test_detail_lists_members_and_addable_pool(client):
    c, db, rh = client
    r1, r2, r3 = _reimb(db, "A"), _reimb(db, "B"), _reimb(db, "C")
    rid = _report(rh, [r1])

    resp = c.get(f"/api/report-history/{rid}")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["id"] for m in body["receipts"]] == [r1]
    pool_ids = {m["id"] for m in body["addable"]}
    assert pool_ids == {r2, r3}  # on no report
    assert r1 not in pool_ids


def test_add_remove_not_completed_leaves_status(client):
    c, db, rh = client
    r1, r2 = _reimb(db, "A"), _reimb(db, "B")
    rid = _report(rh, [r1], completed=False)

    resp = c.post(f"/api/report-history/{rid}/receipts", json={"add": [r2], "remove": [r1]})
    assert resp.status_code == 200
    body = resp.json()
    assert {m["id"] for m in body["receipts"]} == {r2}
    assert body["report"]["pdf_stale"] is True
    # Not completed → no status flips.
    assert db.get_receipt_by_id(r1)["match_status"] == "pending"
    assert db.get_receipt_by_id(r2)["match_status"] == "pending"


def test_add_remove_completed_flips_status(client):
    c, db, rh = client
    r1, r2 = _reimb(db, "A"), _reimb(db, "B")
    rid = _report(rh, [r1])
    # Complete via the endpoint → r1 becomes reimbursed.
    c.post(f"/api/report-history/{rid}/complete", json={"completed": True})
    assert db.get_receipt_by_id(r1)["match_status"] == "reimbursed"

    resp = c.post(f"/api/report-history/{rid}/receipts", json={"add": [r2], "remove": [r1]})
    assert resp.status_code == 200
    # Completed → add flips to reimbursed, remove flips back to pending.
    assert db.get_receipt_by_id(r2)["match_status"] == "reimbursed"
    assert db.get_receipt_by_id(r1)["match_status"] == "pending"


def test_single_membership_and_type_rejection(client):
    c, db, rh = client
    r_on_a = _reimb(db, "A")
    corp = db.save_receipt("corp.png", "/x/corp.png", {"vendor_name": "Corp"}, description="Corp")
    # corp stays expense_type=corp_card (default)
    _report(rh, [r_on_a])  # report A owns r_on_a
    rid_b = _report(rh, [])

    resp = c.post(f"/api/report-history/{rid_b}/receipts", json={"add": [r_on_a, corp]})
    assert resp.status_code == 200
    body = resp.json()
    reasons = {x["id"]: x["reason"] for x in body["rejected"]}
    assert "already on another report" in reasons[r_on_a]
    assert "not a reimbursement receipt" in reasons[corp]
    assert body["report"]["receipt_ids"] == []  # nothing added


def test_reissue_bumps_version_and_clears_stale(client, monkeypatch):
    c, db, rh = client
    from src.services import expense_report_pdf

    monkeypatch.setattr(
        expense_report_pdf, "build_reimbursement_report_from_ids", lambda ids, user_id=None: b"%PDF-NEW"
    )
    r1, r2 = _reimb(db, "A"), _reimb(db, "B")
    rid = _report(rh, [r1])
    c.post(f"/api/report-history/{rid}/receipts", json={"add": [r2]})  # → pdf_stale

    resp = c.post(f"/api/report-history/{rid}/reissue")
    assert resp.status_code == 200
    assert resp.json()["report"]["version"] == 2
    assert resp.json()["report"]["pdf_stale"] is False
    assert c.get(f"/api/report-history/{rid}/download").content == b"%PDF-NEW"


def test_reissue_empty_report_blocked(client):
    c, db, rh = client
    r1 = _reimb(db, "A")
    rid = _report(rh, [r1])
    c.post(f"/api/report-history/{rid}/receipts", json={"remove": [r1]})  # now empty
    resp = c.post(f"/api/report-history/{rid}/reissue")
    assert resp.status_code == 400


def test_delete_completed_report_reverts_receipts(client):
    c, db, rh = client
    r1, r2 = _reimb(db, "A"), _reimb(db, "B")
    rid = _report(rh, [r1, r2])
    c.post(f"/api/report-history/{rid}/complete", json={"completed": True})
    assert db.get_receipt_by_id(r1)["match_status"] == "reimbursed"

    assert c.delete(f"/api/report-history/{rid}").status_code == 204
    assert db.get_receipt_by_id(r1)["match_status"] == "pending"
    assert db.get_receipt_by_id(r2)["match_status"] == "pending"


def test_end_to_end_membership_flow(client, monkeypatch):
    c, db, rh = client
    from src.services import expense_report_pdf

    monkeypatch.setattr(
        expense_report_pdf, "build_reimbursement_report_from_ids", lambda ids, user_id=None: b"%PDF2"
    )
    r1, r2, r3 = _reimb(db, "A"), _reimb(db, "B"), _reimb(db, "C")
    rid = _report(rh, [r1])

    # add r2 → re-issue → complete (r1,r2 reimbursed) → remove r1 (→ pending)
    c.post(f"/api/report-history/{rid}/receipts", json={"add": [r2]})
    assert c.post(f"/api/report-history/{rid}/reissue").json()["report"]["version"] == 2
    c.post(f"/api/report-history/{rid}/complete", json={"completed": True})
    assert db.get_receipt_by_id(r1)["match_status"] == "reimbursed"
    assert db.get_receipt_by_id(r2)["match_status"] == "reimbursed"

    c.post(f"/api/report-history/{rid}/receipts", json={"remove": [r1]})
    assert db.get_receipt_by_id(r1)["match_status"] == "pending"  # left a completed report
    assert db.get_receipt_by_id(r2)["match_status"] == "reimbursed"  # still a member
    # r3 was never added.
    assert db.get_receipt_by_id(r3)["match_status"] == "pending"
