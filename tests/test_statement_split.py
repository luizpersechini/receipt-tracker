"""A corporate PDF bundling several cardholders must split into one
statement per cardholder on upload; a single-cardholder file stays one."""

import pytest
from fastapi.testclient import TestClient

from src.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    statements = tmp_path / "statements"
    statements.mkdir()
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "statements_dir", statements)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.db import database

    database.init_db()
    from src.main import app

    return TestClient(app)


def _multi_holder_txns():
    return [
        {"date": "2026-08-26", "description": "TAXI", "amount": 223.67, "cardholder": "TONY LIMA"},
        {
            "date": "2026-08-13",
            "description": "HOTEL",
            "amount": 1664.80,
            "cardholder": "LUIZ PERSECHINI",
        },
        {
            "date": "2026-08-31",
            "description": "STORE",
            "amount": 549.00,
            "cardholder": "LUIZ PERSECHINI",
        },
    ]


def _upload(client, name="corp.pdf"):
    # A tiny valid PDF byte string — the parser is monkeypatched, so content
    # doesn't matter, only that the .pdf path is taken.
    return client.post(
        "/api/upload/statement",
        files={"file": (name, b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )


def test_multi_cardholder_pdf_splits(client, monkeypatch):
    from src.routers import statements as st

    monkeypatch.setattr(st, "parse_statement", lambda _p: _multi_holder_txns())
    res = _upload(client)
    assert res.status_code == 200
    body = res.json()
    assert body["split"] is True
    holders = {s["cardholder"]: s["transaction_count"] for s in body["statements"]}
    assert holders == {"TONY LIMA": 1, "LUIZ PERSECHINI": 2}

    # Each cardholder is its own statement row, holding only its own rows.
    rows = client.get("/api/statements").json()["statements"]
    assert len(rows) == 2
    for r in rows:
        txns = r["transactions"]
        assert len({t["cardholder"] for t in txns}) == 1
        assert r["filename"].endswith(r["transactions"][0]["cardholder"])


def test_single_cardholder_pdf_stays_one(client, monkeypatch):
    from src.routers import statements as st

    monkeypatch.setattr(
        st,
        "parse_statement",
        lambda _p: [{"date": "2026-08-13", "description": "HOTEL", "amount": 1664.80}],
    )
    res = _upload(client)
    assert res.status_code == 200
    body = res.json()
    assert not body.get("split")
    assert body["transaction_count"] == 1
    assert len(client.get("/api/statements").json()["statements"]) == 1
