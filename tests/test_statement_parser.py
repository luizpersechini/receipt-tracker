"""Unit tests for CSV statement parsing."""

from pathlib import Path

from src.services.statement_parser import parse_statement_csv


def test_parses_standard_columns(tmp_path: Path):
    csv = tmp_path / "stmt.csv"
    csv.write_text(
        "Date,Description,Amount,Category\n"
        "2026-04-30,Starbucks,12.50,Meals\n"
        "2026-05-01,Uber,24.50,Transportation\n",
        encoding="utf-8",
    )
    txns = parse_statement_csv(str(csv))
    assert len(txns) == 2
    assert txns[0]["date"] == "2026-04-30"
    assert txns[0]["description"] == "Starbucks"
    assert txns[0]["amount"] == 12.50
    assert txns[0]["category"] == "Meals"


def test_normalises_european_date_format(tmp_path: Path):
    csv = tmp_path / "stmt.csv"
    csv.write_text(
        "Date,Description,Amount\n"
        "30/04/2026,Padaria,15.00\n",
        encoding="utf-8",
    )
    txns = parse_statement_csv(str(csv))
    assert txns[0]["date"] == "2026-04-30"


def test_strips_currency_symbols_and_uses_dot_decimal(tmp_path: Path):
    csv = tmp_path / "stmt.csv"
    # Quote the amount because the comma in "R$ 12,34" would otherwise split
    # the row into 4 fields.
    csv.write_text(
        'Date,Description,Amount\n'
        '2026-04-30,Padaria,"R$ 12,34"\n',
        encoding="utf-8",
    )
    txns = parse_statement_csv(str(csv))
    assert txns[0]["amount"] == 12.34


def test_portuguese_headers(tmp_path: Path):
    csv = tmp_path / "stmt.csv"
    csv.write_text(
        "Data,Estabelecimento,Valor,Categoria\n"
        "2026-04-30,Café Central,8.75,Alimentação\n",
        encoding="utf-8",
    )
    txns = parse_statement_csv(str(csv))
    assert txns[0]["date"] == "2026-04-30"
    assert txns[0]["description"] == "Café Central"
    assert txns[0]["amount"] == 8.75
