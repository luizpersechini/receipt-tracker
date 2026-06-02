"""Tests for the cardholder roster + statement name-guessing."""

from __future__ import annotations

import pytest


@pytest.fixture
def users_env(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.services import report_users

    return report_users


# ─── CRUD ───────────────────────────────────────────────────────────────────


def test_create_list_get_update_delete(users_env):
    ru = users_env
    assert ru.list_users() == []

    u = ru.create_user({"nome": "Alice Silva", "cost_center": "100"})
    assert u["id"] == 1
    assert u["nome"] == "Alice Silva"
    # Unknown keys are dropped; known defaults are filled.
    assert "moeda" in u

    u2 = ru.create_user({"nome": "Bob Souza"})
    assert u2["id"] == 2
    assert len(ru.list_users()) == 2

    got = ru.get_user(1)
    assert got and got["nome"] == "Alice Silva"

    upd = ru.update_user(1, {"local": "MG"})
    assert upd["local"] == "MG"
    assert upd["nome"] == "Alice Silva"  # untouched fields preserved

    assert ru.delete_user(2) is True
    assert ru.get_user(2) is None
    assert ru.delete_user(999) is False


def test_ids_increment_monotonically_after_delete(users_env):
    ru = users_env
    ru.create_user({"nome": "A"})
    ru.create_user({"nome": "B"})
    ru.delete_user(1)
    c = ru.create_user({"nome": "C"})
    # Next id is max(existing)+1 = 3, not a reused 1.
    assert c["id"] == 3


# ─── Name guessing ──────────────────────────────────────────────────────────


def test_guess_by_filename(users_env):
    ru = users_env
    ru.create_user({"nome": "LUIZ EDUARDO PERSECHINI"})
    ru.create_user({"nome": "TONY HERCULES LIMA"})

    g = ru.guess_user_for_statement(
        {
            "id": 1,
            "filename": "statement_20260526_144911_LUIZ PERSECHINI 22.700,57.pdf",
            "filepath": "",
        }
    )
    assert g and g["nome"] == "LUIZ EDUARDO PERSECHINI"

    g2 = ru.guess_user_for_statement(
        {"id": 2, "filename": "Cartão corporativo Tony 20MAI2026.pdf", "filepath": ""}
    )
    assert g2 and g2["nome"] == "TONY HERCULES LIMA"


def test_guess_returns_none_when_no_match(users_env):
    ru = users_env
    ru.create_user({"nome": "LUIZ PERSECHINI"})
    g = ru.guess_user_for_statement({"id": 1, "filename": "random_statement.csv", "filepath": ""})
    assert g is None


def test_guess_none_with_empty_roster(users_env):
    ru = users_env
    g = ru.guess_user_for_statement({"id": 1, "filename": "LUIZ PERSECHINI.pdf", "filepath": ""})
    assert g is None


def test_guess_ignores_short_common_tokens(users_env):
    ru = users_env
    # A user whose only overlap would be a short particle must not match.
    ru.create_user({"nome": "Ana de Souza"})
    g = ru.guess_user_for_statement(
        {"id": 1, "filename": "fatura cartao de credito.pdf", "filepath": ""}
    )
    # "de" is a stopword, "cartao"/"credito" aren't name tokens → no match.
    assert g is None
