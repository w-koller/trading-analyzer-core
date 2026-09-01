"""Health / environment endpoints.

Three surfaces, deliberately separate, because they answer different
questions for different readers:

  /livez   Is the process alive and is the event loop turning? Nothing else.
           No DB, no OpenD, no Ollama, and crucially no threadpool — see the
           note on the endpoint. This is what the watchdog probes.
  /readyz  Is it able to do its job? Cheap local checks only (DB ping,
           scheduler state, gateway counters). Machine-facing, and the one
           endpoint here that returns a non-200.
  /health  The rich human/dashboard view. Always 200, including when
           everything it depends on is broken.

`/health` returning 200 for a dead dependency is deliberate, not an
oversight: its job is to *report* that OpenD's session died or the Ollama LXC
is down, which it cannot do if it fails along with them. The frontend
distinguishes the two cases by exactly this — a transport error means
"unreachable", a 200 with `status: "degraded"` means "reachable but sick".
Changing it to a non-200 would collapse that distinction.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from starlette.concurrency import run_in_threadpool

from app import db, scheduler, startup_check
from app.auth import require_auth
from app.config import settings
from app.db import DB_PATH
from app.services.moomoo_gateway import get_gateway
from app.services import llm_slots, ollama_models
from app.services.moomoo_trade_gateway import get_trade_gateway

router = APIRouter(tags=["health"])

# Process start, captured at import so /livez can report uptime without
# touching anything.
_BOOT_MONOTONIC = time.monotonic()
_BOOT_WALL = datetime.now(timezone.utc)


def _opend_status() -> dict:
    try:
        return get_gateway().health().to_dict()
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


def _trade_status() -> dict:
    """Trade-session health. Read-only, like everything on that gateway."""
    try:
        return get_trade_gateway().health().to_dict()
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


def _ollama_status() -> dict:
    import httpx

    base = settings.ollama_base_url.rstrip("/")
    try:
        response = httpx.get(f"{base}/models", timeout=5.0)
        response.raise_for_status()
        models = [m.get("id") for m in response.json().get("data", [])]
        return {
            "reachable": True,
            "models": models,
            # The ACTIVE model, which may be a persisted override —
            # reporting the env default while something else is running
            # would make every diagnostic downstream wrong.
            "configured_model_present": ollama_models.active_model() in models,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


@router.get("/livez")
async def livez() -> dict:
    """Liveness: the event loop is turning. Nothing more.

    This does **no** IO of any kind, and specifically does not use
    `run_in_threadpool`. That exclusion is the whole design: a
    full-watchlist scan legitimately occupies threadpool workers for over an
    hour, and a liveness probe that queued behind it would look like a hang
    and make the watchdog restart the backend in the middle of the very work
    it was waiting on. Keeping this path free of the threadpool is what lets
    a one-minute watchdog coexist with a sixty-minute scan.

    Must stay trivial. Anything added here weakens the guarantee.
    """
    return {
        "status": "alive",
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - _BOOT_MONOTONIC, 1),
        "started_at": _BOOT_WALL.isoformat(),
    }


def _db_ping() -> dict:
    try:
        with db.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    """Readiness: cheap local checks only — no OpenD or Ollama round-trip.

    Unlike `/health`, this **does** return a non-200 (503) when the backend
    cannot do its job. That is safe to introduce here because this endpoint
    is new and machine-facing; reacting to a status code is the entire point
    of a readiness probe. The watchdog reads `scan_in_progress` and the
    gateway counters from here to decide whether a slow `/livez` is a wedge
    or just a scan.
    """
    db_state = await run_in_threadpool(_db_ping)
    sched = scheduler.scheduler_status()
    gateway_stats = get_gateway().stats()

    ready = bool(db_state.get("ok")) and bool(sched.get("running"))
    if not ready:
        response.status_code = 503

    return {
        "status": "ready" if ready else "not_ready",
        "db": db_state,
        "scheduler": {
            "running": sched.get("running"),
            "paused": sched.get("paused"),
            "scan_in_progress": sched.get("scan_in_progress"),
            "next_run": sched.get("next_run"),
        },
        "gateway": gateway_stats,
    }


async def _gather() -> tuple[dict, dict, dict]:
    # Gathered rather than awaited in sequence: these were 10s + 5s of
    # worst-case latency back to back, which made the dashboard's 30s poll
    # feel like a hang whenever OpenD was slow.
    return await asyncio.gather(                                # type: ignore[return-value]
        run_in_threadpool(_opend_status),
        run_in_threadpool(_ollama_status),
        run_in_threadpool(_trade_status),
    )


@router.get("/health")
async def health() -> dict:
    """Unauthenticated, always 200, and deliberately thin.

    This endpoint is public because the dashboard has to decide whether the
    backend is reachable BEFORE anyone has signed in, and because the systemd
    watchdog and any other dumb prober should never need a credential. The
    always-200 contract is load-bearing in its own right: the frontend tells
    *unreachable* (a thrown transport error) apart from *degraded* (a 200 whose
    body says so) by exactly that difference, so a 401 here would be a third
    state that neither branch models — it would render as nothing at all, on a
    poll that keeps failing every 30 seconds, which is the worst possible way
    for a health indicator to fail.

    The answer is therefore to make it safe to expose rather than to close it.
    Everything identifying — the account id, the database path, the OpenD and
    Ollama addresses, the installed model list, which tickers were last scanned
    and what errors they raised — lives on /health/detail behind require_auth.
    What is left is exactly what components/health-banner.tsx renders.

    `llm_slots` keeps only its counters here. `holders` carries labels that
    name the ticker being worked on, which is watchlist data.
    """
    opend, ollama, _trade = await _gather()

    healthy = bool(opend.get("qot_logined")) and ollama.get("reachable")
    slots = llm_slots.stats()
    return {
        "status": "ok" if healthy else "degraded",
        "db_exists": Path(DB_PATH).exists(),
        "ollama_model": ollama_models.active_model(),
        "opend": {
            "connected": bool(opend.get("connected")),
            "qot_logined": opend.get("qot_logined"),
        },
        "ollama": {
            "reachable": bool(ollama.get("reachable")),
            "configured_model_present": ollama.get("configured_model_present"),
        },
        "llm_slots": {"active": slots["active"], "capacity": slots["capacity"]},
    }


@router.get("/health/detail", dependencies=[Depends(require_auth)])
async def health_detail() -> dict:
    """The full picture, for a signed-in operator.

    This is the payload /health used to return unauthenticated. It names the
    Moomoo account, the paths and hosts, every installed model, and the last
    scan cycle's per-ticker results including raw error strings.
    """
    opend, ollama, trade = await _gather()

    healthy = bool(opend.get("qot_logined")) and ollama.get("reachable")
    return {
        "status": "ok" if healthy else "degraded",
        "db_path": str(DB_PATH),
        "db_exists": Path(DB_PATH).exists(),
        "opend_target": f"{settings.opend_host}:{settings.opend_port}",
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": ollama_models.active_model(),
        "ollama_model_source": ollama_models.active_model_source(),
        "ollama_model_env_default": settings.ollama_model,
        "scan_interval_seconds": settings.scan_interval_seconds,
        "opend": opend,
        "ollama": ollama,
        "trade": trade,
        "scheduler": scheduler.scheduler_status(),
        "gateway": get_gateway().stats(),
        # Not a health condition — a busy GPU is the system working. It is
        # here so a LEAKED slot is visible: without it, a slot never released
        # would present as "chat is broken" with nothing to look at.
        "llm_slots": llm_slots.stats(),
        "startup_check": startup_check.LAST_RESULT,
        "uptime_seconds": round(time.monotonic() - _BOOT_MONOTONIC, 1),
    }
