"""Cross-source dedup: the same Uber ride imported via CSV + Gmail should
not produce two rows.

Tests use a temporary SQLite DB by patching settings.db_path so we never
touch the user's live data.
"""

import json

import pytest

from src.config import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Use a throwaway SQLite file for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    # Import inside so the patched path is used
    from src.db import database

    database.init_db()
    return database


def _save_uber(db, *, filename, date, amount, currency, description="test"):
    return db.save_receipt(
        filename=filename,
        filepath=f"data/receipts/{filename}",
        metadata={
            "date": date,
            "vendor_name": "Uber",
            "total_amount": amount,
            "currency": currency,
            "category": "transportation",
        },
        description=description,
    )


# ─── find_uber_receipt_by_date_amount ──────────────────────────────────────


def test_finds_exact_match(fresh_db):
    db = fresh_db
    _save_uber(db, filename="uber_syn-abc.csv", date="2026-04-06", amount=42.56, currency="BRL")
    hit = db.find_uber_receipt_by_date_amount("2026-04-06", 42.56, "BRL")
    assert hit is not None
    assert hit["filename"] == "uber_syn-abc.csv"


def test_match_within_one_cent_tolerance(fresh_db):
    db = fresh_db
    _save_uber(db, filename="uber_syn-abc.csv", date="2026-04-06", amount=42.56, currency="BRL")
    # Gmail extracted 42.55 (rounding diff) — still matches.
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.55, "BRL") is not None
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.57, "BRL") is not None
    # 1.5 cents off is NOT a match
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.585, "BRL") is None


def test_returns_none_when_date_differs(fresh_db):
    db = fresh_db
    _save_uber(db, filename="uber_syn-abc.csv", date="2026-04-06", amount=42.56, currency="BRL")
    assert db.find_uber_receipt_by_date_amount("2026-04-07", 42.56, "BRL") is None


def test_returns_none_when_currency_differs(fresh_db):
    db = fresh_db
    _save_uber(db, filename="uber_syn-abc.csv", date="2026-04-06", amount=42.56, currency="BRL")
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.56, "CAD") is None


def test_only_matches_uber_rows(fresh_db):
    """Non-Uber filenames must never appear in the result, even if the
    metadata happens to share date/amount/currency."""
    db = fresh_db
    # An ordinary receipt — not from Uber
    db.save_receipt(
        filename="receipt_20260406_120000_Foo.jpg",
        filepath="data/receipts/receipt_20260406_120000_Foo.jpg",
        metadata={
            "date": "2026-04-06",
            "vendor_name": "Foo",
            "total_amount": 42.56,
            "currency": "BRL",
            "category": "meals",
        },
        description=None,
    )
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.56, "BRL") is None


def test_helper_handles_missing_inputs(fresh_db):
    db = fresh_db
    assert db.find_uber_receipt_by_date_amount(None, 42.56, "BRL") is None
    assert db.find_uber_receipt_by_date_amount("2026-04-06", None, "BRL") is None
    assert db.find_uber_receipt_by_date_amount("2026-04-06", 42.56, "") is None


# ─── update_receipt_file ───────────────────────────────────────────────────


def test_update_receipt_file_swaps_filename_and_path(fresh_db):
    db = fresh_db
    rid = _save_uber(
        db, filename="uber_syn-abc.csv", date="2026-04-06", amount=42.56, currency="BRL"
    )
    ok = db.update_receipt_file(rid, "uber_gm-xyz.pdf", "data/receipts/uber_gm-xyz.pdf")
    assert ok is True
    row = db.get_receipt_by_filename("uber_gm-xyz.pdf")
    assert row is not None
    assert row["id"] == rid
    # Old filename is gone
    assert db.get_receipt_by_filename("uber_syn-abc.csv") is None


def test_update_receipt_file_returns_false_for_unknown_id(fresh_db):
    db = fresh_db
    assert db.update_receipt_file(99999, "x", "y") is False


# ─── attach_only mode (the new "CSV is source of truth" path) ──────────────


def _patch_gmail_for_attach_only(monkeypatch, fixtures):
    """Stub out Gmail API calls so sync_new_receipts can run without network."""
    from src.services import gmail_ingest

    class _Lbls:
        def list(self, **_):
            return self

        def execute(self):
            return {"labels": [{"id": "L1", "name": "rt-processed"}]}

        def create(self, **_):
            return self

    class _Msgs:
        def __init__(self, fx):
            self.fx = fx

        def list(self, **_):
            return self

        def get(self, *, userId, id, format):
            self._gid = id
            return self

        def modify(self, **_):
            return self

        def execute(self):
            # Both list() and get() funnel through execute. The interleaving
            # makes this awkward, so we branch on whether _gid is set.
            if hasattr(self, "_gid"):
                gid, self._gid = self._gid, None
                return self.fx[gid]
            return {"messages": [{"id": mid} for mid in self.fx]}

    class _Service:
        def __init__(self, fx):
            self._fx = fx

        def users(self):
            return self

        def labels(self):
            return _Lbls()

        def messages(self):
            return _Msgs(self._fx)

        def me(self):
            return self

    monkeypatch.setattr(gmail_ingest, "get_service", lambda: _Service(fixtures))
    # Skip the PDF render so tests don't depend on WeasyPrint
    monkeypatch.setattr(
        "src.services.receipt_pdf.is_available",
        lambda: False,
        raising=False,
    )


def _gmail_msg(msg_id, *, subject, body_html, date="Mon, 25 Apr 2026 10:00:00 -0700"):
    import base64

    enc = base64.urlsafe_b64encode(body_html.encode("utf-8")).decode("ascii")
    return {
        "id": msg_id,
        "payload": {
            "headers": [{"name": "Subject", "value": subject}, {"name": "Date", "value": date}],
            "mimeType": "text/html",
            "body": {"data": enc},
        },
    }


def test_attach_only_skips_creating_rows_when_no_csv_match(fresh_db, monkeypatch):
    """attach_only=True: an email with no CSV match must NOT produce a new row."""
    db = fresh_db
    # No CSV rows in DB at all — every email is "unmatched".
    fx = {
        "m1": _gmail_msg(
            "m1",
            subject="[Business] Your trip with Uber",
            body_html="""
                <html><body>
                  <p>Apr 17, 2026</p>
                  <p>Total R$18,98</p>
                  <a href="https://riders.uber.com/trips/abc-123">Receipt</a>
                </body></html>
            """,
        ),
    }
    _patch_gmail_for_attach_only(monkeypatch, fx)
    from src.services.gmail_ingest import sync_new_receipts

    res = sync_new_receipts(attach_only=True)
    assert res["success"] is True
    assert res["imported"] == 0
    assert len(res["unmatched"]) == 1
    assert res["unmatched"][0]["amount"] == 18.98
    # And critically: no new row was inserted
    assert db.get_all_receipts() == []


def test_attach_only_upgrades_existing_csv_row(fresh_db, monkeypatch, tmp_path):
    """attach_only=True: an email matching an existing CSV row attaches its
    PDF (filename/filepath are updated). Strict matcher — exact amount."""
    db = fresh_db
    from src.config import settings

    monkeypatch.setattr(settings, "receipts_dir", tmp_path)
    rid = _save_uber(
        db, filename="uber_syn-existing.csv", date="2026-04-17", amount=18.98, currency="BRL"
    )
    fx = {
        "m1": _gmail_msg(
            "m1",
            subject="[Business] Your trip with Uber",
            body_html="""
                <html><body>
                  <p>Apr 17, 2026</p>
                  <p>Total R$18,98</p>
                  <a href="https://riders.uber.com/trips/abc-123">Receipt</a>
                </body></html>
            """,
        ),
    }
    _patch_gmail_for_attach_only(monkeypatch, fx)
    # Force the PDF renderer to succeed (with stub bytes) so we exercise the
    # upgrade path. Without this, _persist_receipt_pdf falls back to .eml.
    monkeypatch.setattr(
        "src.services.receipt_pdf.is_available",
        lambda: True,
        raising=False,
    )
    monkeypatch.setattr(
        "src.services.receipt_pdf.render_html_to_pdf",
        lambda *a, **k: b"%PDF-1.4\nstub",
        raising=False,
    )
    from src.services.gmail_ingest import sync_new_receipts

    res = sync_new_receipts(attach_only=True)
    assert res["success"] is True
    assert res["imported"] == 0  # no new rows
    assert res["upgraded"] == 1  # existing row gained the PDF
    assert len(res["unmatched"]) == 0
    # Existing row id is preserved; filename now points at the PDF
    rows = db.get_all_receipts()
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["filename"].endswith(".pdf")


def test_attach_only_drops_processed_label_from_gmail_query(fresh_db, monkeypatch):
    """attach_only mode shouldn't exclude rt-processed messages — what
    matters is whether the matching row has a PDF, not whether the message
    was previously examined."""
    captured_queries: list = []
    from src.services import gmail_ingest

    class _Lbls:
        def list(self, **_):
            return self

        def execute(self):
            return {"labels": [{"id": "L1", "name": "rt-processed"}]}

        def create(self, **_):
            return self

    class _Msgs:
        def list(self, *, userId, q, maxResults, pageToken=None):
            captured_queries.append(q)
            self._mode = "list"
            return self

        def get(self, **_):
            self._mode = "get"
            return self

        def modify(self, **_):
            self._mode = "modify"
            return self

        def execute(self):
            return {"messages": []}

    class _Service:
        def users(self):
            return self

        def labels(self):
            return _Lbls()

        def messages(self):
            return _Msgs()

        def me(self):
            return self

    monkeypatch.setattr(gmail_ingest, "get_service", lambda: _Service())

    # Normal mode should include the label exclusion
    gmail_ingest.sync_new_receipts(attach_only=False)
    assert any("-label:rt-processed" in q for q in captured_queries)

    # Attach-only mode should NOT include it
    captured_queries.clear()
    gmail_ingest.sync_new_receipts(attach_only=True)
    assert captured_queries, "expected at least one Gmail query"
    assert not any("-label:rt-processed" in q for q in captured_queries), (
        f"attach_only mode leaked the label exclusion: {captured_queries}"
    )


def test_attach_only_does_not_label_unmatched_messages(fresh_db, monkeypatch):
    """Unmatched emails should remain re-syncable after a wider CSV import.
    The implementation guarantees this by skipping the label call."""
    db = fresh_db
    fx = {
        "m1": _gmail_msg(
            "m1",
            subject="[Business] Your trip with Uber",
            body_html="<p>Apr 17, 2026</p><p>Total R$99,99</p>",
        ),
    }
    _patch_gmail_for_attach_only(monkeypatch, fx)
    # Spy on label calls
    from src.services import gmail_ingest

    label_calls = []
    monkeypatch.setattr(
        gmail_ingest,
        "_apply_label",
        lambda service, msg_id, label_id: label_calls.append(msg_id),
    )
    from src.services.gmail_ingest import sync_new_receipts

    res = sync_new_receipts(attach_only=True)
    assert len(res["unmatched"]) == 1
    assert label_calls == []  # unmatched message stayed un-labeled


def test_find_uber_receipt_source_filter(fresh_db):
    """Regression: CSV imports deduped against rows inserted seconds earlier
    in the SAME import — the second of two same-day same-fare rides vanished.
    The finder must honour the source filter."""
    from src.db import database as db

    db.save_receipt(
        "uber_trip-a.csv",
        "/x/u.csv",
        {"date": "2026-05-01", "total_amount": 50.0, "currency": "BRL", "uber_source": "csv"},
        "",
    )
    # A CSV-import dedup pass looks for GMAIL rows only → no hit.
    assert db.find_uber_receipt_by_date_amount("2026-05-01", 50.0, "BRL", source="gmail") is None
    # A Gmail-sync dedup pass looks for CSV rows → hit.
    assert db.find_uber_receipt_by_date_amount("2026-05-01", 50.0, "BRL", source="csv") is not None
    # Legacy rows without uber_source count as CSV.
    db.save_receipt(
        "uber_trip-b.csv",
        "/x/u.csv",
        {"date": "2026-05-02", "total_amount": 60.0, "currency": "BRL"},
        "",
    )
    assert db.find_uber_receipt_by_date_amount("2026-05-02", 60.0, "BRL", source="csv") is not None


# ─── same-ride re-send dedup (multiple Gmail emails, one ride) ──────────────

# An Uber email with NO trips/<uuid> link → parser yields trip_id=None, so the
# synthetic filename falls back to gm-<msg_id> (per-email). This is the shape
# that used to slip past dedup and create duplicate rows.
_BODY_NO_TRIP = """
    <html><body>
      <p>May 20, 2026</p>
      <p>Total R$15,04</p>
    </body></html>
"""


def test_same_ride_two_emails_no_trip_id_creates_one_row(fresh_db, monkeypatch):
    """Uber sends a ride twice (trip receipt + tax/summary), different
    message-ids, no trip id. Only ONE receipt row may result — the second
    email must dedupe against the first on (date, amount, currency)."""
    db = fresh_db
    fx = {
        "m1": _gmail_msg("m1", subject="[Business] Your trip with Uber", body_html=_BODY_NO_TRIP),
        "m2": _gmail_msg("m2", subject="[Business] Your Uber tax invoice", body_html=_BODY_NO_TRIP),
    }
    _patch_gmail_for_attach_only(monkeypatch, fx)
    from src.services.gmail_ingest import sync_new_receipts

    res = sync_new_receipts(attach_only=False)
    assert res["success"] is True
    assert res["imported"] == 1, f"expected exactly one import, got {res}"
    assert res["skipped"] >= 1, f"second email should have been skipped: {res}"
    rows = db.get_all_receipts()
    assert len(rows) == 1, f"duplicate ride rows: {[r['filename'] for r in rows]}"


def test_distinct_same_fare_rides_with_trip_ids_not_collapsed(fresh_db, monkeypatch):
    """The guard must not over-reach: two genuinely distinct rides that happen
    to share date + fare each carry their own trip id, so both rows survive."""
    db = fresh_db

    def body(uuid):
        return f"""
            <html><body>
              <p>May 20, 2026</p>
              <p>Total R$15,04</p>
              <a href="https://riders.uber.com/trips/{uuid}">Receipt</a>
            </body></html>
        """

    # Trip ids must be hex/dashes per the parser's `trips/([0-9a-fA-F-]{8,})`.
    fx = {
        "m1": _gmail_msg(
            "m1", subject="[Business] Your trip with Uber", body_html=body("aaaaaaaa-1111")
        ),
        "m2": _gmail_msg(
            "m2", subject="[Business] Your trip with Uber", body_html=body("bbbbbbbb-2222")
        ),
    }
    _patch_gmail_for_attach_only(monkeypatch, fx)
    from src.services.gmail_ingest import sync_new_receipts

    res = sync_new_receipts(attach_only=False)
    assert res["imported"] == 2, f"distinct rides were collapsed: {res}"
    assert len(db.get_all_receipts()) == 2
