"""
main.py — FastAPI application entrypoint.

Run in dev with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Run in production via the trading-backend.service systemd unit, which
calls this same target without --reload.

The scheduler starts paused. A scan cycle spends 60-120s of local inference
per ticker, so kicking one off automatically the moment the service boots
would tie up OpenD and the model before anyone has looked at the dashboard.
POST /scan/schedule/resume turns it on.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import require_auth
from app.config import settings
from app.db import init_db, migrate_schema, reconcile_interrupted_runs
from app.routers import (
    alerts, app_settings, auth, chat, earnings, health, market, news,
    outcomes, positions, push, scan,
    sectors, signals, setups, watchlist,
)
from app.scheduler import shutdown_scheduler, start_scheduler
from app.startup_check import run_startup_checks

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logging.getLogger("moomoo").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Add columns that _SCHEMA gained after this database was created.
    # init_db() only runs CREATE TABLE IF NOT EXISTS, so on the live box a
    # new column reaches the schema file and never reaches the table — every
    # INSERT naming it would then fail. Here rather than inside init_db()
    # for the same reason as reconcile_interrupted_runs below: the tests call
    # init_db(), and it is documented to do nothing but create the schema.
    added = migrate_schema()
    if added:
        logger.warning("schema migrated: added %s", ", ".join(added))
    logger.info("database ready")

    # Close out runs a previous restart killed mid-flight. Done here rather
    # than in init_db(), which the tests call and which must stay free of
    # data mutations. Before the scheduler starts, so a new cycle can't be
    # caught by its own reconciliation.
    interrupted = reconcile_interrupted_runs()
    if interrupted:
        logger.warning(
            "marked %d scan run(s) as interrupted by a previous restart", interrupted
        )

    run_startup_checks()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title="Trading Analyzer",
    description="Advisory-only setup scanner. This API never places orders.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /health stays open alongside /livez and /readyz: the dashboard reads it to
# decide whether the backend is reachable at all, and it must be able to do
# that BEFORE anyone has signed in. It was reduced to carry no account or
# market data precisely so it could stay open — the detail it used to leak
# (account id, db path, model list, scanned tickers) moved to
# /health/detail, which is guarded. Everything else sits behind require_auth.
# health.router carries /livez, /readyz and /health — all three intentionally
# open. Its /health/detail route is the exception and guards itself with an
# explicit dependency, since a router cannot be half-mounted.
app.include_router(health.router)

# Login itself cannot require being logged in. Everything in this router is
# reachable unauthenticated, which is why auth_service enforces a per-IP
# lockout rather than treating it as optional polish.
app.include_router(auth.router)

_guarded = [Depends(require_auth)]
app.include_router(watchlist.router, dependencies=_guarded)
app.include_router(setups.router, dependencies=_guarded)
app.include_router(scan.router, dependencies=_guarded)
app.include_router(outcomes.router, dependencies=_guarded)
app.include_router(market.router, dependencies=_guarded)
app.include_router(positions.router, dependencies=_guarded)
app.include_router(app_settings.router, dependencies=_guarded)
app.include_router(news.router, dependencies=_guarded)
app.include_router(chat.router, dependencies=_guarded)
app.include_router(earnings.router, dependencies=_guarded)
app.include_router(alerts.router, dependencies=_guarded)
app.include_router(signals.router, dependencies=_guarded)
app.include_router(sectors.router, dependencies=_guarded)
app.include_router(push.router, dependencies=_guarded)
