"""Report generation endpoints."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..db import database as db
from ..schemas.reports import ExpenseReportRequest
from ..services.fx_fetcher import fetch_rates, get_unique_currencies
from ..services.report_generator import generate_excel_report, generate_expense_report

router = APIRouter(prefix="/api", tags=["reports"])
logger = logging.getLogger(__name__)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/report")
async def generate_report(currency: str = Query("BRL")):
    """Full reconciliation report with FX conversion."""
    try:
        receipts, statements = await asyncio.gather(
            asyncio.to_thread(db.get_all_receipts),
            asyncio.to_thread(db.get_all_statements),
        )
        target = currency.upper()
        currencies = get_unique_currencies(receipts)
        fx_rates = await asyncio.to_thread(fetch_rates, currencies, target) if currencies else {}
        report_path = await asyncio.to_thread(
            generate_excel_report, receipts, statements, target, fx_rates,
        )
        return FileResponse(
            report_path,
            media_type=XLSX_MEDIA_TYPE,
            filename=f"receipt_report_{target}.xlsx",
        )
    except Exception as e:
        logger.exception("generate_report failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reports/expense")
async def create_expense_report(body: ExpenseReportRequest):
    """Generate a typed expense report."""
    try:
        report_type = body.report_type
        target = body.target_currency.upper()
        period_start = body.period_start
        period_end = body.period_end

        expense_type_filter = None if report_type == "all" else report_type
        receipts = await asyncio.to_thread(
            db.get_receipts_by_period, period_start, period_end, expense_type_filter,
        )

        currencies = get_unique_currencies(receipts)
        fx_rates = await asyncio.to_thread(fetch_rates, currencies, target) if currencies else {}

        matches_raw = await asyncio.to_thread(db.get_all_matches)
        matches = [
            {
                "receipt_id": m["receipt_id"],
                "transaction_date": m.get("transaction_data", {}).get("date"),
                "transaction_description": m.get("transaction_data", {}).get("description"),
                "transaction_amount": m.get("transaction_data", {}).get("amount"),
                "match_score": m.get("match_score", 0),
            }
            for m in matches_raw
        ]

        report_path = await asyncio.to_thread(
            generate_expense_report,
            receipts, report_type, target, fx_rates, matches,
        )

        label = report_type.replace("_", " ").title()
        period_label = ""
        if period_start or period_end:
            period_label = f" {period_start or '...'} to {period_end or '...'}"
        await asyncio.to_thread(
            db.save_expense_report,
            f"{label} Report{period_label}",
            report_type,
            target,
            period_start,
            period_end,
        )

        return FileResponse(
            report_path,
            media_type=XLSX_MEDIA_TYPE,
            filename=f"expense_report_{report_type}_{target}.xlsx",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("create_expense_report failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reports/expense")
async def list_expense_reports():
    reports = await asyncio.to_thread(db.get_expense_reports)
    return JSONResponse({"reports": reports})
