"""Tests for the generated-report history index."""

from __future__ import annotations

import pytest


@pytest.fixture
def hist_env(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "reports_dir", tmp_path / "reports")
    from src.services import report_history

    return report_history


def test_record_list_read_delete(hist_env):
    rh = hist_env
    assert rh.list_history() == []

    a = rh.record(
        b"%PDF-A",
        kind="reimbursement",
        title="Reimb · Tony",
        cardholder="Tony",
        currency="BRL",
        created_at="2026-05-01T10:00:00",
    )
    b = rh.record(
        b"%PDF-B",
        kind="statement",
        title="Stmt · Luiz",
        cardholder="Luiz",
        created_at="2026-05-02T10:00:00",
    )

    assert a["id"] == 1 and b["id"] == 2
    # Newest first.
    assert [r["id"] for r in rh.list_history()] == [2, 1]
    # File persisted + readable.
    assert rh.read_pdf(1) == b"%PDF-A"
    assert rh.get_record(2)["kind"] == "statement"

    assert rh.delete_record(1) is True
    assert rh.read_pdf(1) is None
    assert [r["id"] for r in rh.list_history()] == [2]
    assert rh.delete_record(999) is False


def test_ids_monotonic_after_delete(hist_env):
    rh = hist_env
    rh.record(b"x", kind="statement", title="a")
    rh.record(b"y", kind="statement", title="b")
    rh.delete_record(1)
    c = rh.record(b"z", kind="statement", title="c")
    assert c["id"] == 3  # not reused


def test_record_carries_metadata(hist_env):
    rh = hist_env
    r = rh.record(
        b"%PDF",
        kind="reimbursement",
        title="t",
        cardholder="Ana",
        currency="CAD",
        period="2026-04-01 to 2026-04-30",
        download_name="reembolso.pdf",
    )
    assert r["cardholder"] == "Ana"
    assert r["currency"] == "CAD"
    assert r["period"] == "2026-04-01 to 2026-04-30"
    assert r["download_name"] == "reembolso.pdf"
    assert r["size"] == len(b"%PDF")


def test_set_completed_toggles_and_stamps(hist_env):
    """Completing a report flips its flag + stamps completed_at; reopening
    clears it. Unknown id → None. receipt_ids round-trip through the record."""
    rh = hist_env
    rec = rh.record(
        b"%PDF",
        kind="reimbursement",
        title="Reimb",
        receipt_ids=[5, 7, 9],
        created_at="2026-05-01T10:00:00",
    )
    assert rec["receipt_ids"] == [5, 7, 9]
    assert rec["completed"] is False and rec["completed_at"] is None

    done = rh.set_completed(rec["id"], True)
    assert done["completed"] is True and done["completed_at"]
    assert rh.get_record(rec["id"])["completed"] is True

    reopened = rh.set_completed(rec["id"], False)
    assert reopened["completed"] is False and reopened["completed_at"] is None

    assert rh.set_completed(9999, True) is None
