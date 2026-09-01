"""Live checks for moomoo_gateway — requires a running, logged-in OpenD.

Run from backend/:  .venv/bin/python -m tests.test_moomoo_gateway_live

This deliberately hits the real OpenD rather than a mock: the failure modes
worth catching here (session expiry, hung calls, tuple-shape changes between
SDK versions) are exactly the ones a mock would paper over.
"""

import logging
import time
from datetime import date, timedelta

logging.getLogger("moomoo").disabled = True

# Windows END TODAY, never on a pinned date.
#
# These were hardcoded to the day the file was written, which meant the
# cross-check below — the most valuable assertion here — started failing two
# days later for a reason that has nothing to do with the gateway: the last
# bar in the window was Friday's close while the snapshot was that morning's
# live price. A live test that expires silently is worse than no live test,
# because the failure trains you to ignore it.
TODAY = date.today().isoformat()
SHORT_START = (date.today() - timedelta(days=90)).isoformat()
LONG_START = (date.today() - timedelta(days=400)).isoformat()

from app.services.moomoo_gateway import GatewayTimeout, MoomooGateway

from tests.harness import check, report


gw = MoomooGateway(timeout=20.0)

# --- health ---
h = gw.health()
check("health() connects", h.connected, f"qot={h.qot_logined} trd={h.trd_logined}")
check("health() reports healthy", h.healthy)
check("health() returns market states", bool(h.market_states), f"us={h.market_states.get('us')}")

# --- watchlist groups (rule #4) ---
groups = gw.list_groups()
check("list_groups() non-empty", len(groups) > 0, f"{len(groups)} groups")
check("groups flag system vs custom", any(g["is_system"] for g in groups) and
      all("is_system" in g for g in groups),
      f"custom={[g['group_name'] for g in groups if not g['is_system']]}")

# --- members ---
members = gw.list_group_members("US")
check("list_group_members('US') non-empty", len(members) > 0, f"{len(members)} tickers")
check("members carry codes", all(m["code"] for m in members))

# --- history klines: the deterministic-indicator input ---
kl = gw.get_history_kline("US.PLTR", start=SHORT_START, end=TODAY)
check("get_history_kline returns bars", not kl.empty, f"{len(kl)} bars")
check("klines have OHLCV columns",
      {"open", "high", "low", "close", "volume", "time_key"}.issubset(set(kl.columns)),
      f"cols={list(kl.columns)[:6]}")
check("klines are chronologically ordered",
      list(kl["time_key"]) == sorted(kl["time_key"]))

# --- pagination: a long window must not silently return page 1 only ---
# request_history_kline caps at 250 bars/page and hands back a page_req_key.
# Ignoring it returns the OLDEST page with no error, truncating the recent
# weeks the indicators care about most.
long_kl = gw.get_history_kline("US.PLTR", start=LONG_START, end=TODAY)
check("long window paginates past one page", len(long_kl) > 250, f"{len(long_kl)} bars")
check("paginated bars are unique", long_kl["time_key"].is_unique)
check("paginated bars stay chronological",
      list(long_kl["time_key"]) == sorted(long_kl["time_key"]))
# The assertion this whole file exists for. A gateway that drops pages
# returns well-formed data that is simply OLD, so only comparing the last bar
# against the live price can catch it — merely checking the frame is valid
# would have passed while indicators ran on a five-week-old close.
check("last bar matches the live snapshot price",
      abs(float(long_kl["close"].iloc[-1])
          - float(gw.get_snapshot(["US.PLTR"])[0]["last_price"])) < 1e-6,
      f"kline close={long_kl['close'].iloc[-1]} "
      f"at {long_kl['time_key'].iloc[-1]}")

# --- snapshot ---
snap = gw.get_snapshot(["US.PLTR", "US.IBM"])
check("get_snapshot returns rows", len(snap) == 2, f"{len(snap)} rows")
check("snapshot has last_price", all("last_price" in s for s in snap))

# --- empty input short-circuits without a round trip ---
check("get_snapshot([]) returns []", gw.get_snapshot([]) == [])

# --- the safety property: a hung call must not block forever ---
slow_gw = MoomooGateway(timeout=1.0)
t0 = time.time()
try:
    slow_gw._call("sleeper", lambda: (time.sleep(30), None), timeout=1.0)
    check("hung call raises GatewayTimeout", False, "no exception raised")
except GatewayTimeout:
    elapsed = time.time() - t0
    check("hung call raises GatewayTimeout", True, f"after {elapsed:.1f}s")
    check("timeout is enforced promptly", elapsed < 5.0, f"{elapsed:.1f}s")
slow_gw.close()

# --- gateway still usable after a failure (reconnect) ---
check("healthy after prior failure", gw.health().healthy)

gw.close()

report("moomoo_gateway", summary="moomoo_gateway: all live checks passed")
