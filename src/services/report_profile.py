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
    "motivo": "Despesas",  # Motivo do Gasto
    "local": "",
    "moeda": "R$",
    "conta_contabil": "",  # accounting account code shown per row
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
    merged = {**DEFAULT_PROFILE, **(data if isinstance(data, dict) else {})}
    return merged


def save_profile(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the saved profile and persist. Unknown keys are
    ignored so a stray field from the client can't bloat the file."""
    current = load_profile()
    for k, v in (patch or {}).items():
        if k in DEFAULT_PROFILE:
            current[k] = "" if v is None else str(v)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _profile_path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current
