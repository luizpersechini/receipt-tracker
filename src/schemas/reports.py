"""Pydantic models for report-generation endpoints."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ExpenseReportRequest(BaseModel):
    report_type: Literal["corp_card", "reimbursement", "all"] = "all"
    target_currency: str = Field(default="BRL", min_length=3, max_length=3)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
