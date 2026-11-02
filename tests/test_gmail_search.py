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
