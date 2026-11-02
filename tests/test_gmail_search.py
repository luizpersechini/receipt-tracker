"""Ad-hoc Gmail search/import. The Gmail client is a fake — nothing here
touches the network, and the default Uber sync is untouched."""

import base64
import json

import pytest
from PIL import Image

from src.config import settings
from src.services import gmail_search as gs


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _msg(mid, *, subject="Your receipt", atts=(), html=None, labels=()):
    parts = []
    for i, (fname, mime) in enumerate(atts):
        parts.append({"mimeType": mime, "filename": fname, "body": {"attachmentId": f"att{i}", "size": 123}})
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html.encode())}})
    return {
        "id": mid,
        "threadId": f"t{mid}",
        "internalDate": "1788831858000",  # 2026-09-08T01:44 UTC
        "snippet": "snip",
        "labelIds": list(labels),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "invoice@x.com"}, {"name": "Subject", "value": subject}],
            "parts": parts,
        },
    }


class FakeGmail:
    """Just enough of googleapiclient's chained API."""

    def __init__(self, messages, labels=None, attachments=None):
        self.msgs = {m["id"]: m for m in messages}
        self.labels_ = labels or []
        self.atts = attachments or {}
        self.queries = []
        self.modified = []

    def users(self):
        return self

    # labels
    def labels(self):
        return self

    def list(self, userId=None, q=None, maxResults=None, pageToken=None):
        if q is None:
            return _Exec({"labels": self.labels_})
        self.queries.append(q)
        return _Exec({"messages": [{"id": i} for i in self.msgs][:maxResults]})

    def create(self, userId=None, body=None):
        lbl = {"id": "Label_new", "name": body["name"]}
        self.labels_.append(lbl)
        return _Exec(lbl)

    # messages
    def messages(self):
        return self

    def get(self, userId=None, id=None, format=None, messageId=None):
        if messageId is not None:  # attachments().get
            return _Exec({"data": _b64(self.atts[(messageId, id)])})
        return _Exec(self.msgs[id])

    def attachments(self):
        return self

    def modify(self, userId=None, id=None, body=None):
        self.modified.append((id, body))
        return _Exec({})


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "receipts_dir", tmp_path / "receipts")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.db import database

    database.init_db()
    return database


def test_build_query_excludes_categories_and_imported():
    q = gs.build_query("anthropic receipt")
    assert q.startswith("anthropic receipt")
    assert "-category:promotions" in q and "-category:social" in q
    assert f"-label:{settings.gmail_processed_label}" in q
    assert "-label:" not in gs.build_query("x", include_imported=True)


def test_query_for_charge_words_and_window():
    q = gs.query_for_charge("ANTHROPIC* CLAUDE SUB", "2026-09-08")
    assert q == "(ANTHROPIC OR CLAUDE OR SUB) after:2026/09/04 before:2026/09/15"
    assert gs.query_for_charge("MERCADOLIVRE*RCHSP01/03", None) == "(MERCADOLIVRE OR RCHSP)"
    assert gs.query_for_charge("OLLAMA", "2026-09-06") == "OLLAMA after:2026/09/02 before:2026/09/13"


def test_search_lists_attachments_and_imported_flag(monkeypatch):
    svc = FakeGmail(
        [
            _msg("a", atts=[("Receipt.pdf", "application/pdf"), ("logo.gif", "image/gif")], labels=["L1"]),
            _msg("b", html="<p>hi</p>"),
        ],
        labels=[{"id": "L1", "name": settings.gmail_processed_label}],
    )
    out = gs.search("receipt", 10, service=svc)
    assert "-category:promotions" in svc.queries[0]
    a, b = out["messages"]
    assert a["imported"] is True and b["imported"] is False
    assert [x["filename"] for x in a["attachments"]] == ["Receipt.pdf"]  # gif dropped
    assert a["date"] == "2026-09-08" and a["from"] == "invoice@x.com"
    assert b["attachments"] == [] and b["has_html"] is True


def test_search_requires_connection(monkeypatch):
    monkeypatch.setattr(gs.gmail_ingest, "get_service", lambda: None)
    with pytest.raises(RuntimeError):
        gs.search("x")


def test_import_attachment_runs_ocr_and_labels(env, monkeypatch):
    db = env
    pdf = b"%PDF-1.4 fake"
    svc = FakeGmail([_msg("m1", subject="Anthropic receipt", atts=[("Receipt-1.pdf", "application/pdf")])], attachments={("m1", "att0"): pdf})
    seen = {}

    def fake_ocr(path):
        seen["path"] = path
        return [{"metadata": {"vendor_name": "Anthropic", "total_amount": 550.0}, "crop": None}]

    monkeypatch.setattr(gs, "_ocr", fake_ocr)
    out = gs.import_message("m1", service=svc)
    assert out["files"] and out["files"][0].endswith("_Receipt-1.pdf")
    assert seen["path"].read_bytes() == pdf
    row = db.get_receipt_by_id(out["receipts"][0]["receipt_id"])
    assert json.loads(row["metadata"])["vendor_name"] == "Anthropic"
    assert row["description"] == "Gmail: Anthropic receipt"
    assert svc.modified == [("m1", {"addLabelIds": ["Label_new"]})]


def test_import_selected_attachments_only_and_crops(env, monkeypatch):
    svc = FakeGmail(
        [_msg("m2", atts=[("a.png", "image/png"), ("b.png", "image/png")])],
        attachments={("m2", "att0"): b"A", ("m2", "att1"): b"B"},
    )
    crop = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(gs, "_ocr", lambda p: [{"metadata": {"vendor_name": "1"}, "crop": crop}, {"metadata": {"vendor_name": "2"}, "crop": crop}])
    out = gs.import_message("m2", ["att1"], service=svc)
    assert len(out["files"]) == 1 and out["files"][0].endswith("_b.png")
    assert [r["filename"] for r in out["receipts"]] == [out["files"][0][:-4] + "_seg1.png", out["files"][0][:-4] + "_seg2.png"]
    assert all((settings.receipts_dir / r["filename"]).exists() for r in out["receipts"])


def test_import_body_only_renders_pdf(env, monkeypatch):
    svc = FakeGmail([_msg("m3", subject="Payment received", html="<b>R$13,67</b>")])
    from src.services import receipt_pdf

    monkeypatch.setattr(receipt_pdf, "is_available", lambda: True)
    monkeypatch.setattr(receipt_pdf, "render_html_to_pdf", lambda html, **k: b"%PDF rendered " + html.encode())
    monkeypatch.setattr(gs, "_ocr", lambda p: [{"metadata": {"vendor_name": "Google"}, "crop": None}])
    out = gs.import_message("m3", service=svc)
    assert out["files"][0].endswith(".pdf")
    assert (settings.receipts_dir / out["files"][0]).read_bytes().startswith(b"%PDF rendered")


def test_import_body_only_without_html_fails(env):
    svc = FakeGmail([_msg("m4")])
    with pytest.raises(ValueError):
        gs.import_message("m4", service=svc)
    assert svc.modified == []


def test_import_empty_attachment_list_falls_back_to_body(env, monkeypatch):
    svc = FakeGmail([_msg("m5", atts=[("x.pdf", "application/pdf")], html="<p>x</p>")])
    from src.services import receipt_pdf

    monkeypatch.setattr(receipt_pdf, "is_available", lambda: True)
    monkeypatch.setattr(receipt_pdf, "render_html_to_pdf", lambda html, **k: b"%PDF")
    monkeypatch.setattr(gs, "_ocr", lambda p: [])
    out = gs.import_message("m5", [], service=svc)
    assert out["files"][0].endswith(".pdf") and out["receipts"][0]["metadata"]["error"] == "no receipt"


# ─── router ──────────────────────────────────────────────────────────────────


def test_router_search_and_import(env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.main import app
    from src.services import gmail_ingest

    c = TestClient(app)
    monkeypatch.setattr(gmail_ingest, "is_configured", lambda: False)
    assert c.get("/api/gmail/search", params={"q": "abc"}).status_code == 400

    monkeypatch.setattr(gmail_ingest, "is_configured", lambda: True)
    monkeypatch.setattr(gs, "search", lambda q, n, include_imported=False: {"query": q, "messages": [], "n": n, "inc": include_imported})
    r = c.get("/api/gmail/search", params={"q": "ollama", "max_results": 5, "include_imported": "true"})
    assert r.status_code == 200 and r.json() == {"query": "ollama", "messages": [], "n": 5, "inc": True}

    monkeypatch.setattr(gs, "import_message", lambda mid, ids: {"message_id": mid, "files": [], "receipts": [], "ids": ids})
    r = c.post("/api/gmail/import", json={"message_id": "m1", "attachment_ids": ["a"]})
    assert r.status_code == 200 and r.json()["ids"] == ["a"]
    assert c.post("/api/gmail/import", json={}).status_code == 422


# ─── per-transaction finder ──────────────────────────────────────────────────


def _txn(desc, amount, date="2026-09-08", **raw):
    return {"date": date, "description": desc, "amount": amount, "raw_data": raw}


def test_score_exact_amount_date_vendor_attachment():
    s = {"date": "2026-09-08", "from": "invoice@mail.anthropic.com", "subject": "Your receipt from Anthropic", "snippet": "Total R$580.44", "attachments": [{"id": "a"}]}
    score, reasons = gs.score_candidate(_txn("ANTHROPIC* CLAUDE SUB", 580.44), s, "")
    assert score == 1.0
    assert any("580.44 found" in r for r in reasons) and any("anthropic" in r for r in reasons)


def test_score_amount_within_iof_tolerance():
    s = {"date": "2026-09-07", "from": "x@y", "subject": "Receipt", "snippet": "", "attachments": []}
    score, reasons = gs.score_candidate(_txn("ANTHROPIC* CLAUDE SUB", 580.44), s, "Amount paid R$550.00")
    assert score == 0.5  # 0.3 near + 0.2 date
    assert any("within 7%" in r for r in reasons)


def test_score_native_amount_and_wrong_amount():
    s = {"date": "2026-09-20", "from": "billing@metalpriceapi.com", "subject": "Invoice", "snippet": "USD 11.99", "attachments": []}
    score, _ = gs.score_candidate(_txn("METALPRICEAPI.COM", 65.59, "2026-09-17", original_amount=11.99), s, "")
    assert score == 0.8  # exact native + 3 days + vendor
    s2 = {**s, "snippet": "USD 99.00", "from": "a@b", "subject": "hi"}
    assert gs.score_candidate(_txn("METALPRICEAPI.COM", 65.59, "2026-09-17"), s2, "")[0] == 0.1


def test_find_for_transactions_shares_fetches_and_ranks(monkeypatch):
    m1 = _msg("m1", subject="Ollama receipt", html="<p>Total R$ 113,33</p>")
    m2 = _msg("m2", subject="Ollama usage", html="<p>nothing</p>")
    svc = FakeGmail([m1, m2])
    fetches = []
    orig = gs.gmail_ingest.fetch_message
    monkeypatch.setattr(gs.gmail_ingest, "fetch_message", lambda s, mid: (fetches.append(mid), orig(s, mid))[1])
    st = {"id": 1, "transactions": [_txn("OLLAMA", 113.33, "2026-09-06"), _txn("OLLAMA", 512.08, "2026-09-10"), _txn("X", 1.0)]}
    out = gs.find_for_transactions(st, [0, 1, 99], max_per_txn=1, service=svc)
    assert [r["index"] for r in out] == [0, 1]
    assert out[0]["candidates"][0]["id"] == "m1" and out[0]["candidates"][0]["score"] >= 0.7
    assert "after:2026/09/02" in out[0]["query"] and "-category:promotions" in out[0]["query"]
    assert sorted(fetches) == ["m1", "m2"]  # fetched once, reused for line 1


def test_import_and_match_creates_manual_match(env, monkeypatch):
    db = env
    sid = db.save_statement("s.pdf", "s.pdf", [_txn("ANTHROPIC* CLAUDE SUB", 580.44)])
    svc = FakeGmail([_msg("m1", subject="Anthropic", atts=[("r.pdf", "application/pdf")])], attachments={("m1", "att0"): b"%PDF"})
    monkeypatch.setattr(gs, "_ocr", lambda p: [{"metadata": {"vendor_name": "Anthropic", "total_amount": 550.0}, "crop": None}])
    out = gs.import_and_match(sid, 0, "m1", service=svc)
    assert out["match_id"]
    ms = db.get_matches_for_statement(sid)
    assert len(ms) == 1 and ms[0]["transaction_index"] == 0 and ms[0]["match_kind"] == "manual"
    assert db.get_receipt_by_id(out["receipts"][0]["receipt_id"])["match_status"] == "matched"
    with pytest.raises(LookupError):
        gs.import_and_match(sid, 5, "m1", service=svc)


def test_router_find_receipts_skips_matched_credit_and_cancelled(env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.main import app
    from src.services import gmail_ingest

    db = env
    sid = db.save_statement(
        "s.pdf",
        "s.pdf",
        [
            _txn("A", 10.0),
            _txn("B", 20.0),
            _txn("B", -20.0, credit=True),
            _txn("C", 30.0),
        ],
    )
    rid = db.save_receipt("r.png", "r.png", {})
    db.save_match(rid, sid, {}, None, match_kind="manual", transaction_index=3)
    monkeypatch.setattr(gmail_ingest, "is_configured", lambda: True)
    seen = {}
    monkeypatch.setattr(gs, "find_for_transactions", lambda st, idx, max_per_txn=3: (seen.update(idx=idx), [])[1])
    c = TestClient(app)
    r = c.post("/api/gmail/find-receipts", json={"statement_id": sid})
    assert r.status_code == 200 and seen["idx"] == [0]  # B cancelled by its estorno, C matched
    r = c.post("/api/gmail/find-receipts", json={"statement_id": sid, "indices": [1, 3]})
    assert seen["idx"] == []
    assert c.post("/api/gmail/find-receipts", json={"statement_id": 999}).status_code == 404

    monkeypatch.setattr(gs, "import_and_match", lambda s, i, m, a: {"match_id": 7, "receipts": []})
    r = c.post("/api/gmail/import-and-match", json={"statement_id": sid, "transaction_index": 0, "message_id": "m"})
    assert r.status_code == 200 and r.json()["match_id"] == 7
