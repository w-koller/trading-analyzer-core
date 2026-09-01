"""Checks for market_data: the shared kline cache, chart overlays, movers.

Run from backend/:  .venv/bin/python -m tests.test_market_data

No live gateway and no network — a fake gateway stands in, which also lets
the failure paths (an unentitled market, a missing time column) be exercised
deterministically. Those paths are the point: the per-market batching in
get_movers exists because one unentitled market fails the whole snapshot
batch, and that is not reproducible against a healthy account.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app import db

_tmp = tempfile.mkdtemp(prefix="market-data-")
db.DB_PATH = Path(_tmp) / "test.db"

from app.services import market_data                              # noqa: E402

from tests.harness import check, report  # noqa: E402


def make_bars(n: int, end: datetime | None = None) -> pd.DataFrame:
    end = end or datetime.now(timezone.utc)
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1.5, n))
    times = [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(n)]
    return pd.DataFrame({
        "time_key": times, "open": close, "high": close + 1,
        "low": close - 1, "close": close, "volume": rng.integers(1e5, 9e5, n),
    })


class FakeGateway:
    """Counts calls so cache behaviour is observable."""

    def __init__(self, bars: pd.DataFrame | None = None, snapshots=None, fail_markets=()):
        self._bars = bars if bars is not None else make_bars(300)
        self.kline_calls = 0
        self.snapshot_calls: list[list[str]] = []
        self._snapshots = snapshots or {}
        self._fail_markets = set(fail_markets)

    def get_history_kline(self, code, start=None, end=None, **kw):
        self.kline_calls += 1
        return self._bars

    def get_snapshot(self, codes):
        self.snapshot_calls.append(list(codes))
        markets = {c.split(".")[0] for c in codes}
        bad = markets & self._fail_markets
        if bad:
            # Mirrors the real SDK: the whole batch fails, not the bad rows.
            raise RuntimeError("Unsupported quote market.")
        return [self._snapshots[c] for c in codes if c in self._snapshots]


# --- cache -------------------------------------------------------------
market_data.clear_kline_cache()
gw = FakeGateway()
market_data.get_cached_bars(gw, "US.T")
market_data.get_cached_bars(gw, "US.T")
check("a second call inside the TTL is served from cache", gw.kline_calls == 1,
      f"{gw.kline_calls} fetches")

market_data.get_cached_bars(gw, "US.T", use_cache=False)
check("use_cache=False forces a refetch", gw.kline_calls == 2)

market_data.get_cached_bars(gw, "US.T", days=250)
check("a different window is a different cache entry", gw.kline_calls == 3,
      "otherwise a 250-day chart request would serve the scanner short bars")

market_data.clear_kline_cache()
market_data.get_cached_bars(gw, "US.T")
check("clear_kline_cache forces a refetch", gw.kline_calls == 4)

# --- time column resolution -------------------------------------------
check("_time_column finds time_key", market_data._time_column(make_bars(3)) == "time_key")
alt = make_bars(3).rename(columns={"time_key": "time"})
check("_time_column falls back to 'time'", market_data._time_column(alt) == "time")
none_df = make_bars(3).drop(columns=["time_key"])
check("_time_column returns None rather than raising",
      market_data._time_column(none_df) is None)

# --- overlays: bar-count branches -------------------------------------
market_data.clear_kline_cache()
full = market_data.get_klines_with_overlays(FakeGateway(make_bars(300)), "US.T")
o = full["overlays"]
check("300 bars: no warnings", full["warnings"] == [], str(full["warnings"]))
check("every overlay aligns 1:1 with the bars",
      all(len(o[k]) == len(full["bars"]) for k in ("sma_fast", "sma_slow"))
      and all(len(o["bollinger"][k]) == len(full["bars"]) for k in ("upper", "mid", "lower"))
      and all(len(o["macd"][k]) == len(full["bars"]) for k in ("macd", "signal", "hist")))
check("SMA warm-up is null, not zero", o["sma_slow"][0]["value"] is None,
      "a 0.0 would plot as a real price at the origin")
check("the last SMA value is populated", o["sma_slow"][-1]["value"] is not None)

market_data.clear_kline_cache()
mid = market_data.get_klines_with_overlays(FakeGateway(make_bars(100)), "US.T")
check("100 bars: SMA is skipped and says so",
      mid["overlays"]["sma_fast"] == [] and any("sma" in w for w in mid["warnings"]),
      str(mid["warnings"]))
check("100 bars: MACD and Bollinger still computed",
      len(mid["overlays"]["macd"]["macd"]) == 100
      and len(mid["overlays"]["bollinger"]["mid"]) == 100)

market_data.clear_kline_cache()
tiny = market_data.get_klines_with_overlays(FakeGateway(make_bars(10)), "US.T")
check("10 bars: degrades to bars-only rather than raising",
      len(tiny["bars"]) == 10 and len(tiny["warnings"]) == 3, str(tiny["warnings"]))

market_data.clear_kline_cache()
try:
    market_data.get_klines_with_overlays(FakeGateway(pd.DataFrame()), "US.T")
    check("empty bars raise ValueError", False, "no exception")
except ValueError:
    check("empty bars raise ValueError", True)

# --- freshness comes from the data, not the clock ----------------------
market_data.clear_kline_cache()
old_end = datetime.now(timezone.utc) - timedelta(days=9)
stale = market_data.get_klines_with_overlays(FakeGateway(make_bars(300, end=old_end)), "US.T")
check("data_as_of reflects the newest BAR, not now",
      stale["data_as_of"].startswith(old_end.strftime("%Y-%m-%d")),
      stale["data_as_of"])
check("an old newest-bar is flagged stale", stale["bars_stale"] is True,
      f"age {stale['bar_age_days']}d")

market_data.clear_kline_cache()
fresh = market_data.get_klines_with_overlays(FakeGateway(make_bars(300)), "US.T")
check("recent bars are not flagged stale", fresh["bars_stale"] is False,
      f"age {fresh['bar_age_days']}d")

# --- movers ------------------------------------------------------------
snaps = {
    # US.A carries a full extended-hours block. The rates are the SDK's own
    # units — already percent, NOT fractions — and the three do not share a
    # base: pre and overnight are measured against prev_close (100.0), while
    # after-hours is measured against last_price (110.0). Those are the live
    # values' actual relationships, reproduced here so the mapping is pinned.
    "US.A": {
        "code": "US.A", "name": "A", "last_price": 110.0, "prev_close_price": 100.0,
        "pre_price": 104.0, "pre_change_rate": 4.0,
        "after_price": 111.1, "after_change_rate": 1.0,
        "overnight_price": 102.0, "overnight_change_rate": 2.0,
    },
    # US.B has no extended-hours keys at all — the non-US / no-session shape.
    "US.B": {"code": "US.B", "name": "B", "last_price": 90.0, "prev_close_price": 100.0},
    "AU.C": {"code": "AU.C", "name": "C", "last_price": 50.0, "prev_close_price": 50.0},
}
tickers = [
    {"code": "US.A", "name": "A", "market": "US"},
    {"code": "US.B", "name": "B", "market": "US"},
    {"code": "AU.C", "name": "C", "market": "AU"},
]

gw2 = FakeGateway(snapshots=snaps)
res = market_data.get_movers(gw2, tickers)
check("movers batches per market, not all-in-one",
      len(gw2.snapshot_calls) == 2 and all(
          len({c.split('.')[0] for c in call}) == 1 for call in gw2.snapshot_calls),
      f"{gw2.snapshot_calls}")
by_code = {m["code"]: m for m in res["movers"]}
check("change_pct is computed from last vs prev close",
      abs(by_code["US.A"]["change_pct"] - 10.0) < 1e-9
      and abs(by_code["US.B"]["change_pct"] + 10.0) < 1e-9,
      f"A={by_code['US.A']['change_pct']} B={by_code['US.B']['change_pct']}")
check("an unchanged price is 0%, not None", by_code["AU.C"]["change_pct"] == 0.0)
check("AU is stamped delayed, US is not",
      by_code["AU.C"]["is_delayed_data"] is True
      and by_code["US.A"]["is_delayed_data"] is False)

# --- extended hours ----------------------------------------------------
# Zero coverage before this: the fixture carried no pre/after/overnight keys,
# so every one of them was only ever exercised as None.
a = by_code["US.A"]
check("extended-hours prices are surfaced",
      (a["pre_price"], a["after_price"], a["overnight_price"]) == (104.0, 111.1, 102.0),
      f"{a['pre_price']} / {a['after_price']} / {a['overnight_price']}")

# The asymmetry that a future "consistency" cleanup would break: change_pct
# is computed here and multiplied by 100, while *_change_rate arrives already
# in percent and passes through RAW. Asserted side by side in one payload so
# the difference is visible rather than folklore.
check("extended-hours rates pass through unscaled",
      (a["pre_change_pct"], a["after_change_pct"], a["overnight_change_pct"])
      == (4.0, 1.0, 2.0),
      "a x100 here would read as a 400% pre-market gap")
check("...while change_pct in the same row IS computed and scaled",
      abs(a["change_pct"] - 10.0) < 1e-9, str(a["change_pct"]))

check("a ticker with no extended-hours data reports None, not 0.0",
      all(by_code["US.B"][k] is None for k in
          ("pre_price", "after_price", "overnight_price",
           "pre_change_pct", "after_change_pct", "overnight_change_pct")),
      str({k: by_code["US.B"][k] for k in ("pre_price", "pre_change_pct")}))

# A price of 0.0 means the session never ran — OpenD's own encoding, seen
# live on US.IXHL's overnight. indicators._f only maps None/NaN, so a raw
# 0.0 sails through and renders as a $0.00 quote with a "0.00%" badge that
# reads as FLAT when it means ABSENT. Flat is a fact about the market;
# absent is a fact about the data, and conflating them is the unsafe
# direction to be wrong.
zero = {"US.Z": {"code": "US.Z", "name": "Z", "last_price": 10.0,
                 "prev_close_price": 10.0,
                 "overnight_price": 0.0, "overnight_change_rate": 0.0,
                 "pre_price": 9.5, "pre_change_rate": -5.0}}
zres = market_data.get_movers(FakeGateway(snapshots=zero),
                              [{"code": "US.Z", "name": "Z", "market": "US"}])
z = zres["movers"][0]
check("a zero extended-hours price is reported absent, not as 0.0",
      z["overnight_price"] is None, str(z["overnight_price"]))
check("and its rate is dropped with it, not left reading 0.00% flat",
      z["overnight_change_pct"] is None, str(z["overnight_change_pct"]))
check("a session that DID trade in the same row is untouched",
      (z["pre_price"], z["pre_change_pct"]) == (9.5, -5.0),
      f"{z['pre_price']} / {z['pre_change_pct']}")

# The reason batching exists at all.
gw3 = FakeGateway(snapshots=snaps, fail_markets={"AU"})
res3 = market_data.get_movers(gw3, tickers)
check("an unentitled market is skipped, not fatal",
      len(res3["movers"]) == 2 and "AU" in res3["skipped_markets"],
      f"{len(res3['movers'])} movers, skipped={list(res3['skipped_markets'])}")
check("the US tickers survive the AU failure",
      {m["code"] for m in res3["movers"]} == {"US.A", "US.B"},
      "one bad market used to take the whole batch down")

# --- entitlement vs fault, on the kline path ----------------------------
# The AU.CSL bug: OpenD answers "Unsupported quote market" for a market the
# account cannot see, and that used to surface as a raw 502 — a permanent
# product limitation dressed up as an outage.
from app.services.moomoo_gateway import GatewayError  # noqa: E402


class FailingKlineGateway:
    def __init__(self, exc):
        self._exc = exc

    def get_history_kline(self, code, start=None, end=None, **kw):
        raise self._exc


market_data.clear_kline_cache()
try:
    market_data.get_klines_with_overlays(
        FailingKlineGateway(
            GatewayError("request_history_kline returned error: Unsupported quote market.")
        ),
        "AU.CSL",
    )
    raised = None
except Exception as exc:  # noqa: BLE001
    raised = exc

check("an unentitled market raises NotEntitledError, not GatewayError",
      isinstance(raised, market_data.NotEntitledError), f"{type(raised).__name__}: {raised}")
check("NotEntitledError carries the market and the raw OpenD wording",
      raised.market == "AU" and "Unsupported quote market" in raised.detail,
      f"market={getattr(raised, 'market', None)} detail={getattr(raised, 'detail', None)!r}")

market_data.clear_kline_cache()
try:
    market_data.get_klines_with_overlays(
        FailingKlineGateway(GatewayError("request_history_kline timed out after 45s")),
        "US.PLTR",
    )
    raised2 = None
except Exception as exc:  # noqa: BLE001
    raised2 = exc

check("a real gateway fault stays a GatewayError (still a 502, still retryable)",
      isinstance(raised2, GatewayError) and not isinstance(raised2, market_data.NotEntitledError),
      f"{type(raised2).__name__}: {raised2}")

market_data.clear_kline_cache()
ok_payload = market_data.get_klines_with_overlays(FakeGateway(), "US.T")
check("the success shape carries available/reason too, so callers read one shape",
      ok_payload["available"] is True and ok_payload["reason"] is None,
      f"available={ok_payload.get('available')} reason={ok_payload.get('reason')!r}")


# --- edge cases in the percentage ---------------------------------------
check("a zero prev_close yields None, not a division error",
      market_data._change_pct(10.0, 0.0) is None)
check("a missing last price yields None", market_data._change_pct(None, 100.0) is None)

report("market_data")
