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


def test_build_index_mapping_reorder_and_drop():
    from src.services.statement_parser import build_index_mapping

    old = [
        {"date": "2026-01-05", "amount": 10.0, "description": "A"},
        {"date": "2026-01-06", "amount": 20.0, "description": "B"},
        {"date": "2026-01-07", "amount": 30.0, "description": "C"},
    ]
    # Parser fix reorders B before A and drops C.
    new = [old[1], old[0]]
    m = build_index_mapping(old, new)
    assert m == {0: 1, 1: 0}  # C (index 2) unmappable → absent


def test_build_index_mapping_duplicates_pair_in_order():
    from src.services.statement_parser import build_index_mapping

    dup = {"date": "2026-01-05", "amount": 10.0, "description": "UBER"}
    m = build_index_mapping([dup, dict(dup)], [dict(dup), dict(dup)])
    assert m == {0: 0, 1: 1}


def test_remap_statement_rekeys_and_drops(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from src.services import statement_notes

    statement_notes.set_note(7, 0, "first")
    statement_notes.set_note(7, 2, "third")
    statement_notes.remap_statement(7, {0: 1})  # row 0 → 1, row 2 vanished
    assert statement_notes.get_notes(7) == {1: "first"}
