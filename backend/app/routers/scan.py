"""Scanner control and run-history endpoints.

A manual scan is exposed as a synchronous call that really does wait for the
model. One ticker costs 60-120s of local inference, so the frontend must
treat this as a long request — it is not an oversight that it doesn't return
immediately, and making it fire-and-forget would hide failures that the user
needs to see.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import db, scheduler
from app.services import scanner
from app.services.moomoo_gateway import get_gateway

router = APIRouter(prefix="/scan", tags=["scanner"])


class ScanRequest(BaseModel):
    """Payload for an on-demand scan.

    `tickers` is accepted as an alias for `codes` — both names are in use
    against this endpoint and silently ignoring one would scan the whole
    rotation slice instead of the requested ticker, which looks like it
    worked. `max_tickers: null` means the entire enabled watchlist.
    """

    codes: list[str] | None = None
    tickers: list[str] | None = None
    max_tickers: int | None = scanner.DEFAULT_MAX_TICKERS
    sync_first: bool = False
    with_walls: bool = True
    with_news: bool = True
    market: str | None = None
    force: bool = False

    @property
    def target_codes(self) -> list[str] | None:
        return self.codes or self.tickers


@router.post("/run")
async def run_scan(payload: ScanRequest):
    """Run one cycle now. Blocks for ~60-120s per ticker.

    Takes the same `_scan_lock` the scheduled jobs take. Without it a manual
    scan and a rotation cycle could run concurrently and then contend on the
    gateway's (now bounded) lock, where the loser raises GatewayTimeout —
    which `scan_ticker` swallows as a per-ticker failure. The symptom was a
    scan quietly failing in a way that looks like OpenD being sick.

    Refuses rather than queues: a scan that starts an hour late runs against
    data an hour staler, which is the same reasoning the scheduler uses.
    """
    if not scheduler.acquire_scan_lock():
        raise HTTPException(
            status_code=409,
            detail="A scan is already running. Wait for it to finish, or check "
                   "GET /scan/status (scan_in_progress).",
        )
    try:
        result = await run_in_threadpool(
            scanner.run_cycle,
            get_gateway(),
            payload.max_tickers,
            payload.sync_first,
            payload.market,
            payload.target_codes,
            payload.with_walls,
            payload.with_news,
            payload.force,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        scheduler.release_scan_lock()
    return result.to_dict()


def _runs(limit: int):
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM scanner_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/runs")
async def list_runs(limit: int = 20):
    runs = await run_in_threadpool(_runs, limit)
    return {"runs": runs, "count": len(runs)}


@router.get("/status")
async def status():
    return scheduler.scheduler_status()


@router.post("/schedule/resume")
async def resume():
    scheduler.resume()
    return scheduler.scheduler_status()


@router.post("/schedule/pause")
async def pause():
    scheduler.pause()
    return scheduler.scheduler_status()
