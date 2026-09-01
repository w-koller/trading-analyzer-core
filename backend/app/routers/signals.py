"""Ranked opportunities, and the scorecard that keeps them honest.

Read-only and PULL-only. Nothing here schedules a notification, writes an
alert, or touches push_service — see the scope note in services/signals.py
for why that separation is structural rather than incidental.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.services import market_data, signals as signals_service, thesis_scorecard
from app.services.moomoo_gateway import get_gateway
from app.services.moomoo_trade_gateway import get_trade_gateway
from app import db
from app import scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


def _held_codes() -> set[str]:
    """Which tickers are held right now, best effort.

    A dead trade session must not empty the rankings — holdings only decide
    a badge and one suppression here, unlike `/alerts` where "we cannot see
    your positions" is the whole answer and has to be said out loud.
    """
    try:
        return {p["code"] for p in get_trade_gateway().list_positions()}
    except Exception as exc:
        logger.info("signals: holdings unavailable (%s)", exc)
        return set()


def _movers_by_code(market: str | None) -> dict[str, dict[str, Any]]:
    """Live quotes, best effort — they refine the ranking, not gate it.

    The short-horizon model reads extended-hours movement, and spot sharpens
    the entry level. But every one of those inputs has a stored fallback, so
    an OpenD outage should degrade the ranking rather than blank the
    dashboard section.
    """
    try:
        tickers = db.get_enabled_tickers(market)
        data = market_data.get_movers(get_gateway(), tickers)
        return {m["code"]: m for m in data.get("movers", [])}
    except Exception as exc:
        logger.info("signals: movers unavailable, ranking on stored data (%s)", exc)
        return {}


def _build(market: str | None, top_n: int) -> dict[str, Any]:
    return signals_service.get_opportunities(
        market=market,
        held_codes=_held_codes(),
        movers=_movers_by_code(market),
        top_n=top_n,
    )


@router.get("/opportunities")
async def opportunities(market: str | None = None, top_n: int = signals_service.TOP_N):
    """Both horizons, ranked, with the components behind every score."""
    if not 1 <= top_n <= 25:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 25")
    return await run_in_threadpool(_build, market, top_n)


@router.get("/scorecard")
async def scorecard(horizon: int | None = None):
    """How past theses actually resolved, by direction and conviction."""
    if horizon is not None and horizon not in thesis_scorecard.HORIZONS:
        raise HTTPException(
            status_code=400,
            detail=f"horizon must be one of {list(thesis_scorecard.HORIZONS)}",
        )
    return await run_in_threadpool(thesis_scorecard.scorecard, horizon)


def _scored(limit: int) -> dict[str, Any]:
    return thesis_scorecard.run_scoring(get_gateway(), limit)


@router.post("/scorecard/run")
async def run_scoring(limit: int = 500):
    """Score unscored theses now, rather than waiting for the daily job.

    Bounded by `limit` because the first run over a corpus that has never
    been scored is the expensive one — it walks every ticker's bars, and a
    cold cache means a kline fetch each.

    Takes `_scan_lock` and 409s if a scan holds it, exactly as
    `POST /earnings/refresh` does (decisions #51): it can reach OpenD on a
    cache miss, and the alternative is a GatewayTimeout surfacing as a red
    error. Nothing is lost by waiting — the same rows are still unscored
    afterwards, and the daily job would pick them up regardless.
    """
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")
    if not scheduler.acquire_scan_lock():
        raise HTTPException(
            status_code=409,
            detail="a scan is using the gateway; scoring can wait for it",
        )
    try:
        return await run_in_threadpool(_scored, limit)
    finally:
        scheduler.release_scan_lock()
