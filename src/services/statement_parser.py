"""Statement parser for corporate-card statements (CSV + PDF)."""

import csv
import logging
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from .money import parse_money

logger = logging.getLogger(__name__)


def parse_statement(filepath: str) -> list:
    """Top-level dispatcher: pick the right parser based on file extension.

    .pdf → Itaú-style PT-BR card statement (statement_pdf_parser)
    everything else → CSV (pandas-based, see parse_statement_csv)
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        from .statement_pdf_parser import parse_statement_pdf

        return parse_statement_pdf(filepath)
    return parse_statement_csv(filepath)


_COLUMN_MAPPING = {
    "date": ["date", "transaction date", "data", "data da transação"],
    "description": ["description", "merchant", "vendor", "descrição", "estabelecimento"],
    "amount": ["amount", "value", "valor", "debit", "credit"],
    "category": ["category", "categoria", "type", "tipo"],
}

_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"]


def parse_statement_csv(filepath: str) -> list:
    """Parse a corporate card CSV statement into a list of transactions."""
    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        logger.warning("pandas read failed, falling back to simple CSV: %s", e)
        return _parse_simple_csv(filepath)

    df.columns = df.columns.str.lower().str.strip()

    actual_columns: dict[str, str] = {}
    for standard, candidates in _COLUMN_MAPPING.items():
        for c in candidates:
            if c in df.columns:
                actual_columns[standard] = c
                break

    transactions: list[dict] = []
    for _, row in df.iterrows():
        txn: dict = {}

        if "date" in actual_columns:
            date_str = str(row[actual_columns["date"]])
            txn["date"] = _parse_date(date_str)

        txn["description"] = (
            str(row[actual_columns["description"]])
            if "description" in actual_columns
            else "Unknown"
        )

        if "amount" in actual_columns:
            txn["amount"] = _parse_amount(row[actual_columns["amount"]])
        else:
            txn["amount"] = 0.0

        txn["category"] = (
            str(row[actual_columns["category"]])
            if "category" in actual_columns
            else "uncategorized"
        )
        txn["raw_data"] = _json_safe_row(row.to_dict())
        transactions.append(txn)

    return transactions


def _parse_date(date_str: str) -> str:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def _json_safe_row(d: dict) -> dict:
    """JSON-safe copy of a CSV row: pandas NaN/±inf become None (json.dumps
    would emit the non-standard NaN token, which round-trips into responses
    FastAPI refuses to serialize), numpy scalars become Python scalars."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
        elif hasattr(v, "item") and not isinstance(v, (str, bytes)):
            out[k] = v.item()
        else:
            out[k] = v
    return out


def _parse_amount(value) -> float:
    parsed = parse_money(value)
    if parsed is None:
        logger.warning("Unparseable statement amount %r — stored as 0.0", value)
        return 0.0
    return parsed


def _parse_simple_csv(filepath: str) -> list:
    transactions: list[dict] = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)
            for row in reader:
                if len(row) < 3:
                    continue
                transactions.append(
                    {
                        "date": row[0] if len(row) > 0 else "",
                        "description": row[1] if len(row) > 1 else "",
                        "amount": _parse_amount(row[2]) if len(row) > 2 else 0.0,
                        "category": row[3] if len(row) > 3 else "uncategorized",
                        "raw_data": dict(zip(headers, row)),
                    }
                )
    except Exception as e:
        logger.error("Simple CSV parsing failed: %s", e)
    return transactions


def detect_csv_format(filepath: str) -> dict:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            first = f.readline().strip()
            second = f.readline().strip()
        headers = first.split(",")
        return {"headers": headers, "sample": second.split(","), "header_count": len(headers)}
    except OSError:
        return {"headers": [], "sample": [], "header_count": 0}
