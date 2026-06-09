"""Saved profile for the corporate-card expense report front page.

The Atlantic Nickel "Prestação de Contas" form has a bunch of fields that
aren't in the receipt/statement data — employee name, cost centre, bank
details, the form's document code, etc. They change rarely (per employee /
per company), so we persist them once in data/report_profile.json and reuse
them for every generated report.

Editable via GET/PUT /api/report-profile (a small settings modal on the
frontend), or by hand-editing the JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import settings
from .json_store import locked, write_json

logger = logging.getLogger(__name__)


# Field set mirrors the front-page form. Values are plain strings; empty
# strings render as blank lines to be hand-filled after printing.
DEFAULT_PROFILE: dict[str, Any] = {
    # Header band
    "codigo": "MMB-F-REG-0101-001-00",  # the form's document code
    "company_name": "ATLANTIC NICKEL MINERACAO LTDA",
    "report_title": "Prestação de Contas - Cartão Corporativo",
    # Employee band
    "nome": "",
    "cost_center": "",  # C/C
    "centro_custo": "",  # "Centro de Custo+Uneg" default for each row (e.g. 20101/AA1)
    "num_cartao": "",  # Nº Cartão
    "motivo": "Despesas",  # Motivo do Gasto
    "local": "",
    "moeda": "R$",
    "conta_contabil": "",  # default GL account (fallback when no per-column one)
    # Per-column GL accounts ("Conta Contábil" can differ by expense type).
    # A row uses the account for its report column, falling back to
    # `conta_contabil`. Keys mirror the 4 form columns.
    "conta_alimentacao": "",  # Alimentação
    "conta_abastecimento": "",  # Abastecimento / Estac. / Taxi
    "conta_hospedagem": "",  # Hospedagem
    "conta_outros": "",  # Outros
    "expense_kind": "cartao",  # "cartao" (Cartão Crédito) or "reembolso"
    # Bank / signature footer
    "favorecido": "",
    "banco": "",
    "agencia": "",
    "conta_corrente": "",
    "cpf_rg": "",
    "projeto": "",  # optional default for the Projeto column
}


def _profile_path() -> Path:
    return settings.data_dir / "report_profile.json"


def load_profile() -> dict[str, Any]:
    """Return the saved profile merged over the defaults (so new fields
    added to DEFAULT_PROFILE always have a value)."""
    path = _profile_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("report_profile.json unreadable (%s) — using defaults", e)
            data = {}
    raw = data if isinstance(data, dict) else {}
    merged = {**DEFAULT_PROFILE, **raw}
    return _migrate_accounts(merged, raw)


def _migrate_accounts(merged: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Map the legacy 5-column GL accounts onto the current 4-column form.

    Old keys: conta_combustivel / conta_taxi / conta_material.
    New keys: conta_abastecimento (fuel+parking+taxi) / conta_hospedagem.
    Material has no column on this form, so its account is dropped.
    """
    if not merged.get("conta_abastecimento"):
        merged["conta_abastecimento"] = raw.get("conta_combustivel") or raw.get("conta_taxi") or ""
    for old in ("conta_combustivel", "conta_taxi", "conta_material"):
        merged.pop(old, None)
    return merged


@locked(_profile_path)
def save_profile(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the saved profile and persist. Unknown keys are
    ignored so a stray field from the client can't bloat the file."""
    current = load_profile()
    for k, v in (patch or {}).items():
        if k in DEFAULT_PROFILE:
            current[k] = "" if v is None else str(v)
    write_json(_profile_path(), current)
    return current
