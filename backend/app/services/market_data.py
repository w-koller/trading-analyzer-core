"""Shared OHLCV access: the kline cache, and chart-shaped indicator series.

Two callers need history klines for the same codes: the scan cycle
(`scanner.py`) and the chart endpoint (`routers/market.py`). History klines
draw on a per-account Moomoo quota, so the cache lives here rather than in
either caller — two independent caches would double-spend the quota fetching
identical bars, and viewing a chart for a ticker the scanner just scanned
would pay for data already sitting in memory.

`get_klines_with_overlays` differs from `indicators.compute()` in shape, not
in maths: `compute()` collapses to the latest bar for the AI prompt and the
feature vector, while a chart needs every bar of every series. Both call the
same `indicators` functions, so a number plotted here is the same number the
thesis was reasoned from.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from app.config import settings
from app.services import indicators
from app.services.gateway_errors import GatewayError
from app.utils import market_hours

logger = logging.getLogger(__name__)

KLINE_LOOKBACK_DAYS = 400      # enough for a 200-period SMA plus warm-up

# OpenD's wording when the account has no market-data right for a market. It
# is the same string for klines and for snapshots, and it is not a failure:
# it is a complete, permanent answer that this account cannot see this market.
_NOT_ENTITLED_MARKERS = (
    "unsupported quote market",
    "no quote right",
    "no market data right",
)


class NotEntitledError(RuntimeError):
    """This account has no market-data entitlement for `market`.

    Separated from `GatewayError` because the two need opposite handling. A
    GatewayError is a fault — OpenD is down, timed out, or dropped the
    context — and it should surface as a 502 the user can retry. This is not
    a fault and will never succeed: the account holds AU.CSL but has no ASX
    quote entitlement, so there are no ASX bars to be had at any time. Raising
    it as a server error made a permanent product limitation render as
    "502 Bad Gateway on /market/AU.CSL/klines", which sends you debugging
    OpenD for something OpenD is answering correctly.
    """

    def __init__(self, code: str, market: str, detail: str):
        self.code = code
        self.market = market
        self.detail = detail
        super().__init__(
            f"This account has no {market} market-data entitlement, so there "
            f"are no {code} bars to chart."
        )


def _is_not_entitled(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NOT_ENTITLED_MARKERS)

_kline_cache: dict[str, tuple[float, Any]] = {}


def cache_ttl() -> float:
    """How long a cached bar set stays fresh, in seconds.

    Read from settings on every call rather than captured in a module
    constant, and there is deliberately no second copy of the default here:
    `settings.kline_cache_ttl_seconds` is the only owner of this number. Two
    owners for one value is how a config line gets set on a box and changes
    nothing, which is the failure `main.py` already warns about for the corpus
    path.

    Self-hosted keeps the historical 300s. Cloud raises it, because there a
    fetch spends one of 800 shared daily credits and the bars are daily
    anyway — see the setting's own note.
    """
    return float(settings.kline_cache_ttl_seconds)


def get_cached_bars(
    gateway,
    code: str,
    days: int = KLINE_LOOKBACK_DAYS,
    use_cache: bool = True,
):
    """History klines, cached to stay inside the per-account quota.

    Keyed on (code, days) rather than code alone: a 250-day chart request and
    a 400-day scan request are different windows, and returning the shorter
    one to the scanner would silently starve the 200-period SMA.

    ALSO keyed on the gateway's own `cache_namespace`, when it declares one.
    Two providers can serve the same code and disagree about the numbers: the
    cloud deployment's Twelve Data bars are unadjusted while a broker gateway's
    are forward-adjusted (decisions #7), so one shared key would mean a thesis
    computed on bars from a different adjustment convention than the one it
    reports. Where a provider is per-USER it is a second problem as well — one
    account's entitled bars must not be served to another account out of a
    shared cache.

    Read as an ATTRIBUTE rather than switched on `deployment_mode`, so each
    provider declares its own identity and this function needs to know about
    none of them. `MoomooGateway` declares nothing, so the self-hosted key is
    unchanged and its behaviour is byte-identical.
    """
    cache_key = f"{getattr(gateway, 'cache_namespace', '')}|{code}:{days}"
    now = time.monotonic()
    if use_cache and cache_key in _kline_cache:
        cached_at, bars = _kline_cache[cache_key]
        if now - cached_at < cache_ttl():
            return bars

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    bars = gateway.get_history_kline(
        code, start=start.isoformat(), end=end.isoformat()
    )
    _kline_cache[cache_key] = (now, bars)
    return bars


def peek_cached_bars(
    gateway,
    code: str,
    days: int = KLINE_LOOKBACK_DAYS,
):
    """Bars from the cache if they are there and still fresh, else None.

    `get_cached_bars` FETCHES on a miss, which is right for every caller that
    needs bars and wrong for one that only wants the bars somebody else has
    already paid for. Bookkeeping that reads through to the wire is how a
    background job quietly spends an interactive budget.

    Public rather than a private name reached across a package boundary: the
    cloud deployment records each ticker's last two closes straight after a
    scan, and importing `_kline_cache` to do it would be the worst of both
    worlds — the same call decisions #63 made for `db.now_iso` and cloud #28e
    for `hydrate_setup`. Deployment-agnostic: it knows nothing about who is
    asking or why, and self-hosted behaviour is untouched because nothing
    there calls it.

    Keyed identically to `get_cached_bars`, `days` included, so a caller that
    passes a different window gets None rather than a differently-sized frame.
    """
    cache_key = f"{getattr(gateway, 'cache_namespace', '')}|{code}:{days}"
    entry = _kline_cache.get(cache_key)
    if entry is None:
        return None
    cached_at, bars = entry
    return bars if time.monotonic() - cached_at < cache_ttl() else None


def clear_kline_cache() -> None:
    _kline_cache.clear()


# --- chart series ------------------------------------------------------

def _time_column(df: pd.DataFrame) -> str | None:
    """Whichever column carries the bar timestamp.

    `request_history_kline` returns `time_key` (that is what the gateway
    de-duplicates on), but the column has been named differently across SDK
    builds, so fall back rather than KeyError-ing a whole chart.
    """
    for candidate in ("time_key", "time", "date"):
        if candidate in df.columns:
            return candidate
    return None


def _times(df: pd.DataFrame) -> list[str]:
    col = _time_column(df)
    if col is None:
        return []
    return [str(v) for v in df[col].tolist()]


def _series(times: list[str], values: pd.Series) -> list[dict[str, Any]]:
    """One overlay line, NaN mapped to None so it is JSON-safe."""
    return [
        {"time": t, "value": indicators._f(v)}
        for t, v in zip(times, values.tolist())
    ]


def _cross_events(times: list[str], crosses: pd.Series) -> list[dict[str, str]]:
    """Only the bars where a cross actually happened."""
    return [
        {"time": t, "type": str(c)}
        for t, c in zip(times, crosses.tolist())
        if c and str(c) != "none"
    ]


def get_klines_with_overlays(
    gateway,
    code: str,
    days: int = KLINE_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """OHLCV bars plus every indicator as a full series, for charting.

    `days` is CALENDAR days, not bars — roughly 70% of them are trading days.
    The default matches the scanner's window for two reasons: 250 calendar
    days yields only ~171 bars, which silently drops the 200-period SMA off
    every chart, and sharing the window means a chart request reuses the
    scanner's cached bars instead of spending more of the kline quota.

    Daily bars only. The indicator periods are daily-shaped (a 200-period SMA
    over 5-minute bars describes about three days, not a trend), so intraday
    intervals are deliberately not offered rather than silently plotted as if
    they meant the same thing.

    Degrades the same way `indicators.compute` does: an indicator that needs
    more bars than exist is omitted rather than raising, and
    `min_rows_available` lets the caller say why a line is missing.
    """
    market = market_hours.market_of(code)

    # An entitlement gap is a permanent answer, not a fault — translate it
    # before it can be mistaken for one. See NotEntitledError.
    try:
        bars = get_cached_bars(gateway, code, days=days)
    except GatewayError as exc:
        if _is_not_entitled(exc):
            raise NotEntitledError(code, market, str(exc)) from exc
        raise

    if bars is None or bars.empty:
        raise ValueError(f"no history klines returned for {code}")

    time_col = _time_column(bars)
    if time_col is not None:
        bars = bars.sort_values(time_col).reset_index(drop=True)

    times = _times(bars)

    ohlcv = [
        {
            "time": t,
            "open": indicators._f(row.open),
            "high": indicators._f(row.high),
            "low": indicators._f(row.low),
            "close": indicators._f(row.close),
            "volume": indicators._f(getattr(row, "volume", None)),
        }
        for t, row in zip(times, bars.itertuples(index=False))
    ]

    overlays: dict[str, Any] = {
        "sma_fast": [],
        "sma_slow": [],
        "sma_cross_events": [],
        "bollinger": {"upper": [], "mid": [], "lower": []},
        "macd": {"macd": [], "signal": [], "hist": [], "cross_events": []},
    }
    warnings: list[str] = []

    if len(bars) >= 200:
        sma = indicators.sma_cross(bars)
        overlays["sma_fast"] = _series(times, sma["sma_fast"])
        overlays["sma_slow"] = _series(times, sma["sma_slow"])
        overlays["sma_cross_events"] = _cross_events(times, sma["cross"])
    else:
        warnings.append(f"sma_cross needs 200 bars, got {len(bars)}")

    if len(bars) >= 26:
        m = indicators.macd(bars)
        overlays["macd"] = {
            "macd": _series(times, m["macd"]),
            "signal": _series(times, m["macd_signal"]),
            "hist": _series(times, m["macd_hist"]),
            "cross_events": _cross_events(times, m["cross"]),
        }
    else:
        warnings.append(f"macd needs 26 bars, got {len(bars)}")

    if len(bars) >= 20:
        b = indicators.bollinger(bars)
        overlays["bollinger"] = {
            "upper": _series(times, b["bb_upper"]),
            "mid": _series(times, b["bb_mid"]),
            "lower": _series(times, b["bb_lower"]),
        }
    else:
        warnings.append(f"bollinger needs 20 bars, got {len(bars)}")

    # From the newest bar, not the clock — the chart had the same freshness
    # lie as the scanner: it reported "as of now" while serving bars that
    # could be days old (or up to the kline cache TTL stale from the cache).
    last_bar_time = bars[time_col].iloc[-1] if time_col else None

    return {
        "code": code,
        "market": market,
        # Always present, in both the success and the not-entitled shape, so
        # the client branches on a field rather than on which keys exist.
        "available": True,
        "reason": None,
        "is_delayed_data": market_hours.is_delayed_data(market),
        "data_as_of": market_hours.bars_as_of(last_bar_time, market).isoformat(),
        "last_bar_time": str(last_bar_time) if last_bar_time is not None else None,
        "bar_age_days": (
            round(a, 2) if (a := market_hours.bar_age_days(last_bar_time)) is not None else None
        ),
        "bars_stale": market_hours.bars_are_stale(last_bar_time),
        "bars": ohlcv,
        "overlays": overlays,
        "min_rows_available": int(len(bars)),
        "warnings": warnings,
    }


# --- day movers --------------------------------------------------------

def _change_pct(last: float | None, prev: float | None) -> float | None:
    if last is None or prev in (None, 0):
        return None
    return (last - prev) / prev * 100


def _ext_session(row: dict[str, Any], prefix: str) -> tuple[float | None, float | None]:
    """One extended-hours session as (price, change_pct), or (None, None).

    **A price of 0.0 means the session never traded, not that the stock is
    worthless.** OpenD encodes "no pre/post/overnight session for this
    ticker" as a zero price with a zero rate, and `indicators._f` only maps
    None/NaN to None — a raw 0.0 sails straight through. Rendered, that is a
    $0.00 quote and a "0.00%" badge that reads as *flat* when it means
    *absent*, which is the unsafe direction to be wrong: flat is a fact
    about the market, absent is a fact about the data. Observed live on
    US.IXHL's overnight session, and it will recur on any thinly-traded name.

    The rate is dropped alongside the price rather than kept, because a
    percentage with no price behind it is the same lie in a smaller font.
    """
    price = indicators._f(row.get(f"{prefix}_price"))
    if not price:                       # None or 0.0 — the session did not run
        return None, None
    return price, indicators._f(row.get(f"{prefix}_change_rate"))


def get_movers(gateway, tickers: list[dict[str, Any]]) -> dict[str, Any]:
    """Day change for each ticker, computed from a live snapshot.

    Day change is not derivable from anything stored — `watchlist_cache`
    holds no prices — so this is a live call, and the only reason the
    gainers/losers view needs the network at all.

    Snapshots are batched **per market**, not in one call across the whole
    watchlist, because an unentitled market fails the entire batch rather
    than the offending rows: a single AU code makes `get_market_snapshot`
    return "Unsupported quote market" and take every US ticker down with it.
    Grouping means a market the account cannot quote is reported as a skip
    while the rest still return.

    `change_rate` comes back as None from this OpenD build even though the
    field exists, so the percentage is computed here from `last_price` and
    `prev_close_price` (both confirmed populated).

    **Mind the unit asymmetry, and do not "fix" it.** `_change_pct()`
    multiplies by 100 because it divides two prices. The extended-hours
    `*_change_rate` fields ARE populated on this build and arrive **already
    in percent**, so they pass through raw. Verified 2026-08-25 against
    US.AMD: `pre_change_rate` 4.165 sitting beside a computed day change of
    4.73. A fraction would make that a 416% pre-market gap.
    """
    by_market: dict[str, list[dict[str, Any]]] = {}
    for t in tickers:
        by_market.setdefault(str(t.get("market") or "").upper(), []).append(t)

    movers: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}

    for market, group in by_market.items():
        codes = [t["code"] for t in group]
        names = {t["code"]: t.get("name") or "" for t in group}
        try:
            rows = gateway.get_snapshot(codes)
        except Exception as exc:
            logger.info("movers: skipping %s (%s)", market, exc)
            skipped[market] = str(exc)
            continue

        is_delayed = market_hours.is_delayed_data(market)
        as_of = market_hours.data_as_of(market).isoformat()
        for row in rows:
            code = str(row.get("code") or "")
            last = indicators._f(row.get("last_price"))
            prev = indicators._f(row.get("prev_close_price"))
            pre_price, pre_pct = _ext_session(row, "pre")
            after_price, after_pct = _ext_session(row, "after")
            overnight_price, overnight_pct = _ext_session(row, "overnight")
            movers.append({
                "code": code,
                "name": row.get("name") or names.get(code, ""),
                "market": market,
                "last_price": last,
                "prev_close": prev,
                "change_pct": _change_pct(last, prev),
                # Extended-hours moves; present for US, absent elsewhere.
                # These are what "sudden overnight volatility" means for a
                # watchlist looked at before the open.
                #
                # All three are populated simultaneously during regular
                # trading, so they are NOT a "which session is it" signal —
                # they are a record of the last three off-hours sessions.
                # Anything rendering them decides by nullity, never by the
                # clock (see the UI note in market/indicators.tsx).
                #
                # The rate says how far it moved; the price says from what.
                # Only these three of the SDK's 24 extended-hours fields are
                # surfaced — high/low/turnover/amplitude have no reader, and
                # a payload where two thirds of the keys are unused is how
                # the next person stops trusting the shape.
                #
                # NOTE the three rates do NOT share a base. Verified across
                # all 48 US tickers: pre and overnight are measured against
                # `prev_close_price`, but after-hours is measured against
                # `last_price` — the regular session's own close. So they
                # are not directly comparable to each other, and the UI must
                # not invite the reader to subtract one from another.
                "pre_price": pre_price,
                "pre_change_pct": pre_pct,
                "after_price": after_price,
                "after_change_pct": after_pct,
                "overnight_price": overnight_price,
                "overnight_change_pct": overnight_pct,
                "volume": indicators._f(row.get("volume")),
                "is_delayed_data": is_delayed,
                "data_as_of": as_of,
            })

    movers.sort(key=lambda m: m["code"])
    return {"movers": movers, "count": len(movers), "skipped_markets": skipped}
