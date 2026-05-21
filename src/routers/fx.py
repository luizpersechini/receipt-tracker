"""FX rates endpoint."""

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..db import database as db
from ..services.fx_fetcher import fetch_rates, get_unique_currencies

router = APIRouter(prefix="/api/fx", tags=["fx"])
logger = logging.getLogger(__name__)


@router.get("/rates")
async def get_fx_rates(base: str = Query("BRL")):
    """Return today's exchange rates for all currencies present in receipts."""
    try:
        base = base.upper()
        receipts = await asyncio.to_thread(db.get_all_receipts)
        currencies = get_unique_currencies(receipts)
        for c in ("USD", "EUR", "GBP"):
            if c not in currencies:
                currencies.append(c)

        rates = await asyncio.to_thread(fetch_rates, currencies, base)
        return JSONResponse({
            "date": date.today().isoformat(),
            "base": base,
            "rates": rates,
        })
    except Exception as e:
        logger.exception("get_fx_rates failed")
        raise HTTPException(status_code=500, detail=str(e))
