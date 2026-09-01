"""Earnings calendar and the AI outlook per reporting event.

Reads are pure SQLite — they never touch OpenD, so the page keeps working
while a pre-market scan owns the gateway for an hour. Only the two POSTs go
out to the network.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app import db, scheduler
from app.services import earnings_service, llm_slots
from app.services.moomoo_gateway import get_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/earnings", tags=["earnings"])


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    outlook = None
    if row.get("headline"):
        outlook = {
            "headline": row["headline"],
            "what_to_watch": row.get("what_to_watch") or [],
            "news_summary": row.get("news_summary"),
            "uncertainty": row.get("uncertainty"),
            "generated_at": row.get("outlook_generated_at"),
            "model": row.get("outlook_model"),
            "sources": row.get("outlook_sources") or {},
        }
    return {
        "code": row["code"],
        "name": row.get("ticker_name") or row.get("name") or row["code"],
        "market": row["market"],
        "earnings_date": row["earnings_date"],
        "days_until": earnings_service._days_until(row["earnings_date"]),
        "pub_type": row["pub_type"],
        "period_text": row.get("period_text"),
        "eps_predict": row.get("eps_predict"),
        "eps_actual": row.get("eps_actual"),
        "revenue_predict": row.get("revenue_predict"),
        "iv": row.get("iv"),
        "iv_rank": row.get("iv_rank"),
        "iv_percentile": row.get("iv_percentile"),
        "outlook": outlook,
    }


@router.get("")
async def list_earnings(
    days: int = Query(earnings_service.HORIZON_DAYS, ge=1, le=90),
    market: str | None = None,
) -> dict[str, Any]:
    """Stored calendar rows. Never contacts OpenD."""
    rows = await run_in_threadpool(
        db.get_upcoming_earnings, None, days, 0, market.upper() if market else None
    )
    refreshed = await run_in_threadpool(db.earnings_last_refreshed_at)
    return {
        "events": [_shape(r) for r in rows],
        "count": len(rows),
        "horizon_days": days,
        # Permanent, known gaps — reported in the payload so the page can
        # state them in its own flow. Deliberately NOT a health condition and
        # not a banner: a source that never covered AU is not an outage, and
        # banners about non-outages train people to ignore banners (#45).
        "unsupported_markets": earnings_service.UNSUPPORTED_REASON,
        "refreshed_at": refreshed,
    }


@router.get("/{code}")
async def earnings_for_code(code: str) -> dict[str, Any]:
    event = await run_in_threadpool(db.get_next_earnings_for_code, code)
    if event is None:
        return {"code": code, "event": None, "outlook": None}
    outlook = await run_in_threadpool(db.get_outlook, code, event["earnings_date"])
    return {
        "code": code,
        "event": _shape({**event, "ticker_name": event.get("name")}),
        "outlook": outlook,
    }


@router.post("/refresh")
async def refresh_earnings() -> dict[str, Any]:
    """Re-fetch the calendar now.

    Takes `_scan_lock`, because this touches OpenD and decisions #44 defines
    that lock as strictly OpenD's single-threaded-context mutex. The visible
    consequence is a 409 for the hour a pre-market scan runs — which is the
    right trade: the alternative is a GatewayTimeout surfacing as a red error
    on the page, the same symptom decisions #33 fixed for POST /scan/run. The
    data changes at most daily and the job runs four times a day, so a skipped
    refresh costs nothing.
    """
    if not scheduler.acquire_scan_lock():
        raise HTTPException(
            status_code=409,
            detail="A scan is using the market-data gateway. Earnings refresh "
                   "will run on its own schedule, or try again when it finishes.",
        )
    try:
        return await run_in_threadpool(earnings_service.refresh, get_gateway())
    finally:
        scheduler.release_scan_lock()


@router.post("/{code}/outlook")
async def generate_outlook(code: str) -> dict[str, Any]:
    """Write the AI briefing for this ticker's next report.

    Blocks for 60-120s on one model call, so the frontend passes no timeout —
    same reasoning as the scan button.
    """
    event = await run_in_threadpool(db.get_next_earnings_for_code, code)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"No upcoming earnings on record for {code}.",
        )

    token = await run_in_threadpool(
        llm_slots.acquire, f"outlook {code}", llm_slots.INTERACTIVE_TIMEOUT
    )
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="The model is busy. Try again in a moment.",
        )
    try:
        return await run_in_threadpool(
            earnings_service.generate_outlook, get_gateway(), event
        )
    except earnings_service.OutlookError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        llm_slots.release(token)
