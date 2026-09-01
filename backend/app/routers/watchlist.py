"""Watchlist and group endpoints.

Rule #4: system groups are read-only for writes. That is enforced in
db.add_ticker_to_group / remove_ticker_from_group, which raise ValueError;
this layer translates that into a 403 rather than re-implementing the check,
so there is exactly one place the rule lives.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import db
from app.services import market_data
from app.services.moomoo_gateway import get_gateway
from app.services.watchlist_service import sync_watchlist

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class EnabledUpdate(BaseModel):
    enabled: bool


class GroupMembership(BaseModel):
    code: str


@router.get("")
async def list_watchlist(market: str | None = None, enabled_only: bool = False):
    if enabled_only:
        tickers = await run_in_threadpool(db.get_enabled_tickers, market)
    else:
        tickers = await run_in_threadpool(_all_tickers, market)
    return {"tickers": tickers, "count": len(tickers)}


def _all_tickers(market: str | None):
    with db.get_connection() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM watchlist_cache WHERE market = ? ORDER BY code",
                (market,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watchlist_cache ORDER BY code"
            ).fetchall()
        return [dict(r) for r in rows]


@router.get("/movers")
async def movers(market: str | None = None):
    """Day change for every enabled ticker. Live call — see market_data.

    Not derivable from stored data: `watchlist_cache` holds no prices. A
    market the account has no quote entitlement for (AU, today) is reported
    in `skipped_markets` rather than failing the request.
    """
    tickers = await run_in_threadpool(db.get_enabled_tickers, market)
    return await run_in_threadpool(market_data.get_movers, get_gateway(), tickers)


@router.get("/groups")
async def list_groups():
    groups = await run_in_threadpool(db.get_all_groups)
    return {"groups": groups, "count": len(groups)}


@router.get("/groups/{group_id}/members")
async def group_members(group_id: str):
    return {"members": await run_in_threadpool(db.get_group_members, group_id)}


@router.post("/sync")
async def sync():
    """Pull groups and members from Moomoo. Slow — rate-limited to 8 calls/30s."""
    result = await run_in_threadpool(sync_watchlist, get_gateway())
    return result.to_dict()


@router.patch("/{code}/enabled")
async def set_enabled(code: str, payload: EnabledUpdate):
    await run_in_threadpool(db.set_ticker_enabled, code, payload.enabled)
    return {"code": code, "enabled": payload.enabled}


@router.post("/groups/{group_id}/members")
async def add_member(group_id: str, payload: GroupMembership):
    try:
        await run_in_threadpool(db.add_ticker_to_group, group_id, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"group_id": group_id, "code": payload.code, "added": True}


@router.delete("/groups/{group_id}/members/{code}")
async def remove_member(group_id: str, code: str):
    try:
        await run_in_threadpool(db.remove_ticker_from_group, group_id, code)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"group_id": group_id, "code": code, "removed": True}
