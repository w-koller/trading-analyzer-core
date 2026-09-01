"""Sector rotation: where money moved, across sectors and sub-industries.

Read-only and PULL-only, the same discipline `routers/signals.py` states for
opportunities and for the same reason (decisions #66). This engine is
watchlist-WIDE and then some — 262 plates rather than 50 tickers — so it
never writes an alert row, never enters push_service, never raises a
notification and never carries a badge. Alerts remain the only thing that
demands attention, and they remain held-positions-only.

Every response carries `available` plus a `reason` (the decisions #47 shape):
an empty corpus, an unscored market, or a plate the member refresh has not
reached yet are all 200s that say what is missing, never a fabricated zero
and never a 502.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app import scheduler
from app.services import (sector_etfs, sector_flow, sector_narrative,
                          sector_universe)
from app.services.moomoo_gateway import get_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sectors", tags=["sectors"])

#: Markets whose plate universe this account can actually enumerate. US only
#: today: every enabled ticker is US, the account has no ASX quote
#: entitlement, and get_plate_list has not been probed for HK or AU. A
#: non-US request degrades to available:false rather than guessing.
SUPPORTED_MARKETS = ("US",)


def _check_window(window: int) -> None:
    if window not in sector_flow.WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"window must be one of {list(sector_flow.WINDOWS)}",
        )


def _check_market(market: str) -> dict[str, Any] | None:
    """None if supported, else the degraded body to return."""
    if market in SUPPORTED_MARKETS:
        return None
    return {
        "available": False,
        "reason": (
            f"{market} sector data is not available on this account — "
            f"supported: {', '.join(SUPPORTED_MARKETS)}"
        ),
        "market": market,
        "windows": list(sector_flow.WINDOWS),
        "inflow": [],
        "outflow": [],
    }


@router.get("")
async def universe(market: str = "US"):
    """The plate universe, with how fresh it is and how complete."""
    degraded = _check_market(market)
    if degraded:
        return {**degraded, "plates": [], "counts": {}}

    def _build() -> dict[str, Any]:
        plates = sector_universe.plate_universe(market)
        by_class: dict[str, int] = {}
        for p in plates:
            by_class[p["plate_class"]] = by_class.get(p["plate_class"], 0) + 1
        # A plate with 0 constituents has not been VISITED yet — the member
        # refresh is a rotating slice — so it is counted separately rather
        # than being folded in as an empty sector.
        unvisited = sum(1 for p in plates if not (p.get("constituent_count") or 0))
        return {
            "available": bool(plates),
            "reason": None if plates else "the sector refresh has not run yet",
            "market": market,
            "windows": list(sector_flow.WINDOWS),
            "counts": {**by_class, "total": len(plates)},
            "members_unvisited": unvisited,
            "universe_age_days": sector_universe.universe_age_days(market),
            "universe_max_age_days": sector_universe.UNIVERSE_MAX_AGE_DAYS,
            "plates": plates,
        }

    return await run_in_threadpool(_build)


@router.get("/rotation")
async def rotation(
    market: str = "US",
    window: int = 5,
    top_n: int = 10,
    plate_class: str | None = None,
):
    """The board: sectors money moved into, and out of, over one window."""
    _check_window(window)
    if not 1 <= top_n <= 50:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 50")
    if plate_class is not None and plate_class not in sector_universe.PLATE_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"plate_class must be one of {list(sector_universe.PLATE_CLASSES)}",
        )
    degraded = _check_market(market)
    if degraded:
        return {**degraded, "window_days": window}
    return await run_in_threadpool(
        sector_flow.rotation_board, market, window, top_n, plate_class
    )


@router.get("/pairs")
async def pairs(market: str = "US", window: int = 5, top_n: int = 5):
    """Related sectors that moved in opposite directions over the same window.

    **Not traced flows.** Nothing available links a dollar leaving one sector
    to a dollar arriving in another, which is exactly why this is a ranked
    list with a stated basis rather than a Sankey. The `link_basis` on every
    row says what made the two related in the first place.
    """
    _check_window(window)
    if not 1 <= top_n <= 20:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 20")
    degraded = _check_market(market)
    if degraded:
        return {"available": False, "reason": degraded["reason"], "pairs": []}

    # The service owns the availability decision, because only it knows how
    # much constituent overlap data exists — an empty list here would read as
    # "nothing is rotating" when the truth is "member lists are still loading".
    return await run_in_threadpool(sector_flow.rotation_pairs, market, window, top_n)


@router.get("/etfs")
async def etf_flows(days: int = 21):
    """Signed block-sized order flow for every registered sector ETF."""
    if not 1 <= days <= 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")

    def _build() -> dict[str, Any]:
        from app import db

        codes = [e.code for e in sector_etfs.ETFS]
        series = db.get_etf_flows(codes, days=days)
        rows = []
        for etf in sector_etfs.ETFS:
            data = [r for r in series.get(etf.code, []) if r.get("in_flow") is not None]
            if not data:
                continue
            rows.append(
                {
                    "code": etf.code,
                    "label": etf.label,
                    "asset_class": etf.asset_class,
                    "plate_codes": list(etf.plate_codes),
                    "sessions": len(data),
                    "net_flow": round(sum(r["in_flow"] or 0.0 for r in data), 2),
                    "main_flow": round(sum(r["main_in_flow"] or 0.0 for r in data), 2),
                }
            )
        rows.sort(key=lambda r: r["main_flow"], reverse=True)
        return {
            "available": bool(rows),
            "reason": None if rows else "no ETF flow captured yet",
            "days": days,
            "etfs": rows,
            "note": (
                "main_flow is net block-sized order flow, not reported block "
                "trades and not fund creations."
            ),
        }

    return await run_in_threadpool(_build)


@router.post("/narratives/run")
async def run_narratives(window: int = sector_narrative.NARRATIVE_WINDOW,
                         top_n: int = sector_narrative.TOP_N_EACH_WAY,
                         market: str = "US"):
    """Write narratives for the day's biggest movers, rather than waiting.

    Deliberately does NOT take `_scan_lock` — unlike `/sectors/refresh`, this
    makes zero OpenD calls, and holding the quote mutex through minutes of
    inference would delay a pre-market scan for nothing (decisions #51). GPU
    contention is handled by `llm_slots` per generation, so a chat can always
    get the other slot.

    Given no timeout ceiling by the client for the same reason `runScan` is:
    three to six generations at 30-120s each.
    """
    _check_window(window)
    if not 1 <= top_n <= 10:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 10")
    if _check_market(market):
        raise HTTPException(
            status_code=400,
            detail=f"market must be one of {list(SUPPORTED_MARKETS)}",
        )
    return await run_in_threadpool(
        sector_narrative.refresh_narratives, market, window, top_n
    )


@router.get("/{plate_code}/narrative")
async def narrative(plate_code: str, window: int = sector_narrative.NARRATIVE_WINDOW):
    """The stored narrative for one sector, if there is one.

    Read-only: it never generates on demand. A narrative costs GPU time and
    is written by the nightly job, so a page load must not be able to trigger
    one — and `available: false` here means "not written yet", which the UI
    renders as nothing at all rather than an empty shell.
    """
    _check_window(window)

    def _build() -> dict[str, Any]:
        found = sector_narrative.narrative_for(plate_code, window)
        if not found:
            return {
                "available": False,
                "reason": "no narrative written for this sector and window yet",
                "plate_code": plate_code,
                "window_days": window,
            }
        return {
            "available": True,
            "reason": None,
            **found,
            # Stated in the payload rather than left to the UI to remember.
            # This is the one endpoint here whose content a model wrote, and
            # it sits next to numbers that a model did not.
            "disclaimer": (
                "Interpretation, not measurement. The rotation score was "
                "computed in Python and this text did not affect it."
            ),
        }

    return await run_in_threadpool(_build)


@router.get("/{plate_code}")
async def detail(plate_code: str, window: int = 5):
    """One sector: score, history, constituents, neighbours and ETF flow."""
    _check_window(window)
    return await run_in_threadpool(sector_flow.sector_detail, plate_code, window)


def _refresh(market: str) -> dict[str, Any]:
    return sector_flow.ingest(get_gateway(), market=market)


@router.post("/refresh")
async def refresh(market: str = "US"):
    """Re-ingest plate bars and rescore now, rather than waiting for 21:00 ET.

    Takes `_scan_lock` and 409s if a scan holds it, exactly as
    `POST /signals/scorecard/run` and `POST /earnings/refresh` do
    (decisions #51): this makes ~300 quote calls and the alternative is a
    GatewayTimeout surfacing as a red error.

    Nothing is lost by waiting, and less than in either of those cases: the
    kline call BACKFILLS, so whatever this run would have written is still
    written by the next one, over the same bars.
    """
    if _check_market(market):
        raise HTTPException(
            status_code=400,
            detail=f"market must be one of {list(SUPPORTED_MARKETS)}",
        )
    if not scheduler.acquire_scan_lock():
        raise HTTPException(
            status_code=409,
            detail="a scan is using the gateway; the sector refresh can wait for it",
        )
    try:
        return await run_in_threadpool(_refresh, market)
    finally:
        scheduler.release_scan_lock()
