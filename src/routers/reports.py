"""Report generation endpoints."""

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from ..db import database as db
from ..schemas.reports import ExpenseReportRequest
from ..services import expense_report_pdf, report_profile, report_users
from ..services.fx_fetcher import fetch_rates, get_unique_currencies
from ..services.report_generator import generate_excel_report, generate_expense_report

router = APIRouter(prefix="/api", tags=["reports"])
logger = logging.getLogger(__name__)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ─── Report profile (saved header/footer fields for the PDF report) ─────────


@router.get("/report-profile")
async def get_report_profile():
    profile = await asyncio.to_thread(report_profile.load_profile)
    return JSONResponse(profile)


@router.put("/report-profile")
async def put_report_profile(patch: dict = Body(...)):
    try:
        saved = await asyncio.to_thread(report_profile.save_profile, patch)
        return JSONResponse(saved)
    except Exception as e:
        logger.exception("put_report_profile failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Report users (roster of cardholders) ───────────────────────────────────


@router.get("/report-users")
async def list_report_users():
    users = await asyncio.to_thread(report_users.list_users)
    return JSONResponse({"users": users})


@router.post("/report-users")
async def create_report_user(data: dict = Body(...)):
    try:
        user = await asyncio.to_thread(report_users.create_user, data)
        return JSONResponse(user)
    except Exception as e:
        logger.exception("create_report_user failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/report-users/{user_id}")
async def update_report_user(user_id: int, patch: dict = Body(...)):
    user = await asyncio.to_thread(report_users.update_user, user_id, patch)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return JSONResponse(user)


@router.delete("/report-users/{user_id}", status_code=204)
async def delete_report_user(user_id: int):
    ok = await asyncio.to_thread(report_users.delete_user, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return Response(status_code=204)


@router.get("/statements/{statement_id}/guess-user")
async def guess_statement_user(statement_id: int):
    """Best-guess roster user for a statement, by cardholder name. Returns
    {user_id, nome} or {user_id: null} when nothing matches."""
    statement = await asyncio.to_thread(db.get_statement_by_id, statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    guessed = await asyncio.to_thread(report_users.guess_user_for_statement, statement)
    if not guessed:
        return JSONResponse({"user_id": None})
    return JSONResponse({"user_id": guessed.get("id"), "nome": guessed.get("nome")})


# ─── Combined expense-report PDF, built from a reconciled statement ─────────


@router.get("/statements/{statement_id}/report")
async def statement_report_pdf(statement_id: int, user_id: int | None = Query(None)):
    """Build the combined 'Prestação de Contas' PDF (front-page summary +
    receipt-image grid) for one reconciled statement. `user_id` picks a
    roster user's profile; omitted → guess by cardholder name → global
    default."""
    try:
        pdf_bytes = await asyncio.to_thread(
            expense_report_pdf.build_statement_report, statement_id, user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("statement_report_pdf failed")
        raise HTTPException(status_code=500, detail=str(e))

    fname = f"prestacao_contas_statement_{statement_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


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
            generate_excel_report,
            receipts,
            statements,
            target,
            fx_rates,
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
            db.get_receipts_by_period,
            period_start,
            period_end,
            expense_type_filter,
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
            receipts,
            report_type,
            target,
            fx_rates,
            matches,
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
