"""Checks for the resilience machinery: gateway recovery, boot-time state.

Run from backend/:  .venv/bin/python -m tests.test_resilience

The gateway checks matter most. The whole claim of the timeout handling is
that a transient OpenD hang costs one call rather than bricking every later
one, and that claim was previously untested — the bug it fixes (a single
worker left blocked inside the SDK, with every subsequent submit queued
behind it) is invisible until the second call fails too.

Runs entirely against fakes: no OpenD, no network, throwaway database.
"""

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app import db

_tmp = tempfile.mkdtemp(prefix="resilience-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import moomoo_gateway as mg                     # noqa: E402
from app.services import moomoo_trade_gateway as mtg              # noqa: E402
from app import startup_check                                     # noqa: E402

from tests.harness import check, report  # noqa: E402


# --- the gateway recovers from a hung call ----------------------------
gw = mg.MoomooGateway(timeout=0.5)
gw._ctx = object()          # pretend we're connected; nothing here talks to OpenD

_release = threading.Event()


def hangs_until_released():
    """Stands in for an SDK call that blocks past its timeout."""
    _release.wait(timeout=30)
    return (0, "late")


def returns_fine():
    return (0, "ok")


try:
    gw._call("hanging", hangs_until_released)
    check("a hung call raises GatewayTimeout", False, "no exception")
except mg.GatewayTimeout:
    check("a hung call raises GatewayTimeout", True)

check("the orphaned worker is counted", gw.stats()["orphaned_threads"] == 1,
      str(gw.stats()))
check("the context is dropped so the next call reconnects", gw._ctx is None)

# The regression that matters: with the old single-worker pool reused, this
# second call queued behind the still-blocked orphan and timed out forever.
gw._ctx = object()
try:
    result = gw._call("after-hang", returns_fine)
    check("the NEXT call still works after a hang", result == "ok", str(result))
except mg.GatewayTimeout:
    check("the NEXT call still works after a hang", False,
          "pool was not rebuilt — one hang bricks all quote access")

_release.set()
gw.close()

# --- lock acquisition is bounded --------------------------------------
gw2 = mg.MoomooGateway(timeout=0.3)
gw2._ctx = object()
gw2._lock.acquire()          # simulate another thread holding it indefinitely
started = time.monotonic()
try:
    gw2._call("blocked", returns_fine)
    check("waiting on a held lock eventually gives up", False, "no exception")
except mg.GatewayTimeout as exc:
    waited = time.monotonic() - started
    check("waiting on a held lock eventually gives up", "lock" in str(exc).lower(),
          f"{waited:.1f}s: {exc}")
    check("it gives up promptly, not indefinitely", waited < 10, f"{waited:.1f}s")
gw2._lock.release()
gw2.close()

# --- stats never block ------------------------------------------------
gw3 = mg.MoomooGateway()
gw3._lock.acquire()
t0 = time.monotonic()
s = gw3.stats()
check("stats() does not block on the lock it reports", time.monotonic() - t0 < 0.5,
      "a stats call that waits on the lock cannot diagnose a stuck lock")
check("stats() reports the connection state", s["connected"] is False, str(s))
gw3._lock.release()
gw3.close()

# --- the TRADE gateway recovers identically ---------------------------
#
# Same four claims as above, against the other gateway. They share their call
# plumbing, so these are not redundant: they are what stops a change made for
# one side from silently regressing the other. The trade context is the one
# that answers "what do I hold" and "how did my trades go", so a wedged trade
# gateway shows the dashboard an empty portfolio rather than an error.
#
# `_call` here takes a METHOD NAME, not a callable — it resolves the attribute
# off the live context inside the lock — so the fake context is an object
# carrying those names.

_trade_release = threading.Event()


def trade_hangs():
    """Stands in for a trade SDK call that blocks past its timeout."""
    _trade_release.wait(timeout=30)
    return (0, "late")


def trade_returns_fine():
    return (0, "ok")


fake_ctx = SimpleNamespace(hanging=trade_hangs, fine=trade_returns_fine)

tgw = mtg.MoomooTradeGateway(timeout=0.5)
tgw._ctx = fake_ctx

try:
    tgw._call("hanging", "hanging")
    check("a hung trade call raises TradeGatewayTimeout", False, "no exception")
except mtg.TradeGatewayTimeout:
    check("a hung trade call raises TradeGatewayTimeout", True)

check("the orphaned trade worker is counted",
      tgw.stats()["orphaned_threads"] == 1, str(tgw.stats()))
check("the trade context is dropped so the next call reconnects", tgw._ctx is None)

tgw._ctx = fake_ctx
try:
    result = tgw._call("after-hang", "fine")
    check("the NEXT trade call still works after a hang", result == "ok", str(result))
except mtg.TradeGatewayTimeout:
    check("the NEXT trade call still works after a hang", False,
          "pool was not rebuilt — one hang bricks all holdings and deal history")

_trade_release.set()
tgw.close()

# Bounded lock acquisition, same reasoning as the quote side.
tgw2 = mtg.MoomooTradeGateway(timeout=0.3)
tgw2._ctx = fake_ctx
tgw2._lock.acquire()
started = time.monotonic()
try:
    tgw2._call("blocked", "fine")
    check("a held trade lock eventually gives up", False, "no exception")
except mtg.TradeGatewayTimeout as exc:
    waited = time.monotonic() - started
    check("a held trade lock eventually gives up", "lock" in str(exc).lower(),
          f"{waited:.1f}s: {exc}")
    check("the trade gateway gives up promptly", waited < 10, f"{waited:.1f}s")
tgw2._lock.release()
tgw2.close()

tgw3 = mtg.MoomooTradeGateway()
tgw3._lock.acquire()
t0 = time.monotonic()
ts = tgw3.stats()
check("trade stats() does not block on the lock it reports",
      time.monotonic() - t0 < 0.5,
      "a stats call that waits on the lock cannot diagnose a stuck lock")
check("trade stats() reports the connection state", ts["connected"] is False, str(ts))
tgw3._lock.release()
tgw3.close()

# The advisory-only invariant, asserted where it is cheapest to assert. The
# live suite checks this too, but that one needs OpenD; this one always runs.
for forbidden in ("place_order", "modify_order", "cancel_order", "unlock_trade"):
    check(f"the trade gateway has no {forbidden}()",
          not hasattr(mtg.MoomooTradeGateway, forbidden),
          "this project never places orders — see CLAUDE.md decisions #21")

# --- persisted key/value state ----------------------------------------
check("absent state returns the default",
      db.get_app_state("nope", "fallback") == "fallback")
db.set_app_state("k", "1")
check("state round-trips", db.get_app_state("k") == "1")
db.set_app_state("k", "0")
check("state is overwritten, not duplicated", db.get_app_state("k") == "0")
with db.get_connection() as conn:
    n = conn.execute("SELECT count(*) FROM app_state WHERE key='k'").fetchone()[0]
check("an upsert leaves exactly one row", n == 1, f"{n} rows")

# --- interrupted runs are closed out at boot ---------------------------
run_a = db.insert_scanner_run()
run_b = db.insert_scanner_run()
db.finish_scanner_run(run_b, 1, 1, 0, status="completed")

closed = db.reconcile_interrupted_runs()
check("only the still-running row is reconciled", closed == 1, f"{closed} closed")

with db.get_connection() as conn:
    a = conn.execute("SELECT * FROM scanner_runs WHERE id=?", (run_a,)).fetchone()
    b = conn.execute("SELECT * FROM scanner_runs WHERE id=?", (run_b,)).fetchone()
check("the interrupted run is marked failed", a["status"] == "failed", a["status"])
check("and says why", "interrupted by restart" in (a["error_summary"] or ""),
      str(a["error_summary"]))
check("it gets a finished_at so it stops looking live", a["finished_at"] is not None)
check("an already-completed run is untouched", b["status"] == "completed")
check("reconciling twice is a no-op", db.reconcile_interrupted_runs() == 0)

# --- startup self-check ------------------------------------------------
result = startup_check.run_startup_checks()
check("the self-check returns a result for every probe",
      set(result["checks"]) == {"database", "config", "opend", "ollama"},
      str(sorted(result["checks"])))
check("the database probe passes against a writable temp DB",
      result["checks"]["database"]["ok"] is True,
      result["checks"]["database"]["detail"])
check("the probe leaves no residue in app_state",
      db.get_app_state("_startup_probe") is None,
      "the write probe must roll back what it wrote")
check("results are cached for /health", startup_check.LAST_RESULT is not None)

# A non-writable database must be caught here rather than at the first scan.
_saved = db.DB_PATH
db.DB_PATH = Path("/proc/nonexistent/trading.db")
try:
    bad = startup_check._check_db()
    check("an unwritable database is reported, not raised", bad["ok"] is False,
          bad["detail"][:70])
finally:
    db.DB_PATH = _saved

report("resilience")
