"""Current holdings, read-only.

Exists so the dashboard can mark tickers the user actually holds. The
underlying gateway (`moomoo_trade_gateway`) is query-only by construction —
see its module docstring. Nothing here writes, and there is no order path.

A dead trade session returns 200 with `available: false` rather than an
error status: the holdings badge is decoration on top of every other view,
and a 502 here would turn "positions are temporarily unknown" into a broken
dashboard. Same reasoning as `health.py`'s treatment of a dead OpenD.
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from app.services.moomoo_trade_gateway import (
    TradeGatewayError,
    get_trade_gateway,
)

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("")
async def list_positions(market: str | None = None):
    try:
        positions = await run_in_threadpool(
            get_trade_gateway().list_positions, market
        )
    except TradeGatewayError as exc:
        return {"available": False, "reason": str(exc), "positions": [], "count": 0}

    return {
        "available": True,
        "reason": None,
        "positions": positions,
        "count": len(positions),
    }
