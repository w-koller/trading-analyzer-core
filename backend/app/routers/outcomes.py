"""Trade outcome endpoints.

Rule #5: outcomes come preferentially from Moomoo's historical deals;
manual logging is the fallback. The Moomoo deal sync is not wired yet — the
gateway is quote-side only by design (CLAUDE.md: advisory-only, no trade
context is opened), so pulling deals needs a trade-context decision the user
should make deliberately. `POST /outcomes/sync` accepts a batch of deals from
a caller for now, and manual logging works today.

The setup<->deal match is a heuristic (db.find_candidate_setup_for_deal), so
the manual override here is load-bearing, not polish.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import db
from app.services import outcome_sync
from app.services.moomoo_trade_gateway import TradeGatewayError, get_trade_gateway

router = APIRouter(prefix="/outcomes", tags=["outcomes"])


class OutcomeIn(BaseModel):
    setup_id: int
    entry_price: float | None = None
    exit_price: float | None = None
    pnl_abs: float | None = None
    pnl_pct: float | None = None
    hold_time_hours: float | None = None
    exit_reason: str | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    notes: str | None = None


class DealBatch(BaseModel):
    deals: list[dict[str, Any]]


def _exists(setup_id: int) -> bool:
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM trade_setups WHERE id = ?", (setup_id,)
        ).fetchone() is not None


@router.post("")
async def log_outcome(payload: OutcomeIn):
    if not await run_in_threadpool(_exists, payload.setup_id):
        raise HTTPException(status_code=404,
                            detail=f"no setup {payload.setup_id}")
    outcome_id = await run_in_threadpool(
        lambda: db.log_outcome(source="manual", **payload.model_dump())
    )
    return {"outcome_id": outcome_id, "setup_id": payload.setup_id}


@router.post("/sync")
async def sync_deals(payload: DealBatch):
    """Upsert a caller-supplied batch of closed deals; dedup by moomoo_deal_id."""
    return await run_in_threadpool(db.sync_moomoo_outcomes, payload.deals)


@router.post("/sync/moomoo")
async def sync_from_moomoo(days: int = 360):
    """Pull real fills from Moomoo, pair them into round trips, and store them.

    This is rule #5's primary path: outcomes come from what actually happened
    in the account, and manual logging is the fallback. Read-only — it queries
    deal history and writes only to the local database.

    Slow-ish (one Moomoo call per 360-day window) but not scan-slow, so it
    runs inline. Deals that match no setup are counted, not forced: a trade
    made for reasons this tool never saw should not be attributed to a thesis.
    """
    try:
        result = await run_in_threadpool(
            outcome_sync.sync_outcomes, get_trade_gateway(), db, days,
        )
    except TradeGatewayError as exc:
        # Same contract as /positions: a dead trade session is reported, not
        # raised as a server error.
        return {"available": False, "reason": str(exc)}
    return {"available": True, **result}


def _list(limit: int, code: str | None):
    query = """
        SELECT o.*, s.code, s.trade_direction, s.conviction_score
        FROM trade_outcomes o JOIN trade_setups s ON s.id = o.setup_id
    """
    params: list[Any] = []
    if code:
        query += " WHERE s.code = ?"
        params.append(code)
    query += " ORDER BY o.created_at DESC LIMIT ?"
    params.append(limit)
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


@router.get("")
async def list_outcomes(limit: int = 50, code: str | None = None):
    outcomes = await run_in_threadpool(_list, limit, code)
    return {"outcomes": outcomes, "count": len(outcomes)}


@router.get("/stats")
async def stats(code: str | None = None):
    return await run_in_threadpool(db.get_win_rate_stats, code)
