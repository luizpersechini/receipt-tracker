"""Unit tests for the per-transaction override stores used by the report:
statement_labels (Detalhamento) and statement_costcenters (Centro de Custo).

Both mirror statement_notes: JSON keyed by (statement_id, transaction_index),
blank input clears, and clear_statement drops a whole statement's entries.
"""

from __future__ import annotations

import pytest

from src.services import statement_costcenters, statement_labels


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def test_labels_roundtrip_and_clear(data_dir):
    statement_labels.set_label(1, 0, "MOTORISTA RAYLANDER")
    statement_labels.set_label(1, 2, "WI-FI ON BOARD")
    assert statement_labels.get_label(1, 0) == "MOTORISTA RAYLANDER"
    assert statement_labels.get_labels(1) == {0: "MOTORISTA RAYLANDER", 2: "WI-FI ON BOARD"}
    # Blank clears just that entry.
    statement_labels.set_label(1, 0, "   ")
    assert statement_labels.get_label(1, 0) is None
    assert statement_labels.get_labels(1) == {2: "WI-FI ON BOARD"}
    # clear_statement drops the rest.
    statement_labels.clear_statement(1)
    assert statement_labels.get_labels(1) == {}


def test_cost_centers_roundtrip_and_clear(data_dir):
    statement_costcenters.set_cost_center(5, 3, "20101/AA7")
    assert statement_costcenters.get_cost_center(5, 3) == "20101/AA7"
    assert statement_costcenters.get_cost_centers(5) == {3: "20101/AA7"}
    statement_costcenters.set_cost_center(5, 3, "")
    assert statement_costcenters.get_cost_centers(5) == {}
    statement_costcenters.set_cost_center(5, 1, "20101/AA1")
    statement_costcenters.clear_statement(5)
    assert statement_costcenters.get_cost_centers(5) == {}


def test_stores_are_independent(data_dir):
    # Two stores must not collide on disk (different filenames).
    statement_labels.set_label(9, 0, "UBER")
    statement_costcenters.set_cost_center(9, 0, "20101/AA1")
    assert statement_labels.get_label(9, 0) == "UBER"
    assert statement_costcenters.get_cost_center(9, 0) == "20101/AA1"
    assert (data_dir / "statement_labels.json").exists()
    assert (data_dir / "statement_costcenters.json").exists()
