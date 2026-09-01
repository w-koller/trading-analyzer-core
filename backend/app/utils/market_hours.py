"""Market session + data-freshness logic.

Implements CLAUDE.md rule #7: ASX quotes reach us 15 minutes delayed, US
quotes are real-time, and every stored setup has to record which of the two
it was built from (`is_delayed_data`, `data_as_of`) so neither the UI nor
the AI prompt can mistake stale AU data for live data.

Holiday calendars are deliberately NOT hardcoded here — see the module-level
note on `session_of()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

# Quote delay per market, in minutes. Anything not listed is treated as
# delayed by DEFAULT_DELAY_MINUTES, because assuming "real-time" for an
# unknown market is the unsafe direction to be wrong in.
DELAY_MINUTES: dict[str, int] = {
    "US": 0,
    "AU": 15,
    "HK": 15,
}
DEFAULT_DELAY_MINUTES = 15

MARKET_TZ: dict[str, ZoneInfo] = {
    "US": ZoneInfo("America/New_York"),
    "AU": ZoneInfo("Australia/Sydney"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}

class Session(str, Enum):
    """Which part of the trading day a market is in.

    The two members beyond the obvious four exist because the scheduler now
    fires a scan before each session, and a session the model cannot name is
    a session it cannot schedule around. Both match a state OpenD already
    reports in its own `MarketState` enum, so the vocabularies line up:

      BREAK      OpenD `REST`      — HKEX's 12:00-13:00 midday recess
      OVERNIGHT  OpenD `OVERNIGHT` — US 20:00-04:00 ET

    Historical setups stored before these existed carry the old vocabulary:
    the US overnight window was written as `closed` (about 40% of the corpus
    at the time of the change) and HK's lunch as `open`. Nothing branches on
    the value — it is stored on the setup and interpolated into a prompt —
    so old rows stay readable, they are simply less precise. There is no
    migration and none is warranted.
    """

    CLOSED = "closed"
    PRE_MARKET = "pre_market"
    OPEN = "open"
    BREAK = "break"
    POST_MARKET = "post_market"
    OVERNIGHT = "overnight"


@dataclass(frozen=True)
class Window:
    """One session window in exchange-local time.

    `end` is exclusive. `end <= start` means the window wraps past midnight —
    only US overnight does this today, and it is the reason a flat tuple of
    four times could not express the model: 20:00-04:00 is not orderable
    against the rest of the day on a single clock face.

    `scan_before` marks a window whose START is worth a scan: the scheduler
    builds one cron per (market, window) for these. HKEX's lunch break is a
    real session for LABELLING but not one to scan ahead of — the afternoon
    resumption at 13:00 is, which is why the two HK OPEN windows are separate
    rows rather than one block with a hole in it.

    `days` is the cron day-of-week for that scan, not for the window itself.
    """

    session: Session
    start: time
    end: time
    scan_before: bool = False
    days: str = "mon-fri"


# Ordered session windows per market, in exchange-local time. Gaps between
# windows are CLOSED; there is deliberately no explicit CLOSED row.
MARKET_SESSIONS: dict[str, tuple[Window, ...]] = {
    # NYSE/Nasdaq. The four starts are the four scan triggers, and together
    # the windows tile the whole weekday — a US equity is in *some* session
    # 24 hours a day now that overnight is modelled.
    "US": (
        Window(Session.PRE_MARKET, time(4, 0), time(9, 30), scan_before=True),
        Window(Session.OPEN, time(9, 30), time(16, 0), scan_before=True),
        Window(Session.POST_MARKET, time(16, 0), time(20, 0), scan_before=True),
        # Wraps midnight. Moomoo's 24/5 overnight sessions START on Sunday
        # through Thursday evenings, so the scan that precedes this one skips
        # Friday — a Friday evening scan would be for a session that never
        # opens.
        #
        # Spelled as a list, not the range "sun-thu": APScheduler numbers
        # days mon=0..sun=6, so that range is descending and raises
        # "minimum value in a range must not be higher than the maximum".
        Window(Session.OVERNIGHT, time(20, 0), time(4, 0),
               scan_before=True, days="sun,mon,tue,wed,thu"),
    ),
    # ASX: pre-open auction from 07:00, continuous 10:00-16:00, closing
    # auction a few minutes past 16:00 (modelled as post). No overnight
    # equity session.
    "AU": (
        Window(Session.PRE_MARKET, time(7, 0), time(10, 0), scan_before=True),
        Window(Session.OPEN, time(10, 0), time(16, 0), scan_before=True),
        Window(Session.POST_MARKET, time(16, 0), time(16, 12)),
    ),
    # HKEX: pre-open auction 09:00, morning 09:30-12:00, midday recess to
    # 13:00, afternoon 13:00-16:00, closing auction to ~16:10. The recess is
    # the gap this model used to paper over — `session_of` reported OPEN
    # straight across it, which is why CLAUDE.md forbade gating on this
    # function until it was fixed.
    "HK": (
        Window(Session.PRE_MARKET, time(9, 0), time(9, 30), scan_before=True),
        Window(Session.OPEN, time(9, 30), time(12, 0), scan_before=True),
        Window(Session.BREAK, time(12, 0), time(13, 0)),
        Window(Session.OPEN, time(13, 0), time(16, 0), scan_before=True),
        Window(Session.POST_MARKET, time(16, 0), time(16, 10)),
    ),
}


def _window_times(market: str) -> tuple[time, time, time, time] | None:
    """(pre_open, regular_open, regular_close, post_close), or None.

    The legacy 4-tuple shape, derived rather than stored, for the callers
    that only ever wanted "when does regular trading start". Kept because
    two of them index it positionally and a derived accessor is safer than
    a second hand-maintained table that can drift from MARKET_SESSIONS.
    """
    windows = MARKET_SESSIONS.get(market.upper())
    if not windows:
        return None
    regular = [w for w in windows if w.session is Session.OPEN]
    pre = [w for w in windows if w.session is Session.PRE_MARKET]
    post = [w for w in windows if w.session is Session.POST_MARKET]
    if not regular:
        return None
    return (
        pre[0].start if pre else regular[0].start,
        regular[0].start,
        regular[-1].end,
        post[-1].end if post else regular[-1].end,
    )


MARKET_HOURS: dict[str, tuple[time, time, time, time]] = {
    m: t for m in MARKET_SESSIONS if (t := _window_times(m)) is not None
}


def market_of(code: str) -> str:
    """Extract the Moomoo market prefix from a security code.

    >>> market_of("US.AAPL")
    'US'
    >>> market_of("AU.BHP")
    'AU'

    A bare code with no prefix is treated as unknown ("") rather than guessed
    at, so callers fall back to the conservative delayed-data default.
    """
    if not code or "." not in code:
        return ""
    return code.split(".", 1)[0].strip().upper()


def delay_minutes(market: str) -> int:
    """Quote delay for a market, in minutes."""
    return DELAY_MINUTES.get(market.upper(), DEFAULT_DELAY_MINUTES)


def is_delayed_data(market: str) -> bool:
    """True when this market's quotes are not real-time."""
    return delay_minutes(market) > 0


def data_as_of(market: str, now: datetime | None = None) -> datetime:
    """Freshness of a LIVE QUOTE, in UTC: `now` minus the market's delay.

    Correct for snapshot-derived data (the movers endpoint). **Not** correct
    for anything derived from history bars — use `bars_as_of` for that. A
    daily bar from Friday does not become fresh because it is now Monday, and
    stamping a setup with the clock told the model that 72-hour-old bars were
    real-time (rule #7 exists to prevent exactly that).
    """
    now = _as_utc(now)
    return now - timedelta(minutes=delay_minutes(market))


# A Friday close read on Monday morning is ~72h old and entirely normal, so
# age alone cannot mean "stale". Five days clears a weekend plus a holiday
# Monday without needing the exchange holiday calendars this project
# deliberately does not hardcode (decisions log #9). It is a heuristic for
# flagging, never for gating order flow — there is none.
STALE_AFTER_DAYS = 5.0


def parse_bar_time(raw: Any) -> datetime | None:
    """Parse a kline `time_key` ("2026-08-21 00:00:00") to aware UTC.

    Returns None rather than raising: a setup should still be produced if the
    timestamp is unparseable, with the freshness reported as unknown.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _as_utc(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace(" ", "T")))
    except ValueError:
        return None


def bars_as_of(last_bar_time: Any, market: str = "", now: datetime | None = None) -> datetime:
    """When the newest bar is actually from — the honest `data_as_of`.

    Falls back to the clock-based value only when the bar timestamp cannot be
    read, which is the conservative direction: it is better to claim data is
    older than it is than newer.
    """
    parsed = parse_bar_time(last_bar_time)
    if parsed is not None:
        return parsed
    return data_as_of(market, now)


def bar_age_days(last_bar_time: Any, now: datetime | None = None) -> float | None:
    """How old the newest bar is, in days. None if it can't be parsed."""
    parsed = parse_bar_time(last_bar_time)
    if parsed is None:
        return None
    return (_as_utc(now) - parsed).total_seconds() / 86400.0


def bars_are_stale(last_bar_time: Any, now: datetime | None = None) -> bool:
    """True when the newest bar is old enough to be suspicious."""
    age = bar_age_days(last_bar_time, now)
    return age is not None and age > STALE_AFTER_DAYS


def _in_window(t: time, window: Window) -> bool:
    """Is exchange-local time `t` inside `window`? Handles the midnight wrap.

    A window with `end <= start` spans midnight (US overnight, 20:00-04:00),
    so the test inverts: inside means at-or-after the start OR before the
    end, rather than both.
    """
    if window.end <= window.start:
        return t >= window.start or t < window.end
    return window.start <= t < window.end


def session_of(market: str, now: datetime | None = None) -> Session:
    """Which trading session `market` is in, from exchange-local wall clock.

    Walks `MARKET_SESSIONS` in order, so HKEX's midday recess and the US
    overnight window are both reported correctly. That was not always true:
    this function used to test a single 09:30-16:00 block and reported OPEN
    straight through HK's lunch, and had no name at all for US 20:00-04:00.
    Both were fixed before anything was allowed to schedule off it.

    NOTE: this is still a calendar/clock heuristic. It knows about weekends
    but NOT about public holidays or half-days, which differ per exchange
    and drift year to year (decisions #9 — they are deliberately not
    hardcoded). When OpenD is connected the authoritative answer is its own
    `get_global_state`, surfaced as `GatewayStatus.market_states` via
    `moomoo_gateway.MoomooGateway.health()` — note that reports only the
    CURRENT state, so it can corroborate a label but cannot generate future
    session boundaries, which is why the scheduler crons off this table.
    """
    market = market.upper()
    tz = MARKET_TZ.get(market)
    windows = MARKET_SESSIONS.get(market)
    if tz is None or not windows:
        return Session.CLOSED

    local = _as_utc(now).astimezone(tz)
    if local.weekday() >= 5:  # Saturday/Sunday
        return Session.CLOSED

    t = local.time()
    for window in windows:
        if _in_window(t, window):
            return window.session
    return Session.CLOSED


def session_starts(market: str) -> tuple[Window, ...]:
    """The windows whose START is worth scanning ahead of.

    One cron job per entry, per market. HKEX yields three (pre-open, the
    morning open and the afternoon resumption after lunch); the US yields
    four; AU two.
    """
    return tuple(w for w in MARKET_SESSIONS.get(market.upper(), ()) if w.scan_before)


def last_session_start(market: str, now: datetime | None = None) -> datetime | None:
    """When the current session began, in UTC. None if the market is shut.

    This is what "covered" means to the gap-filler: a ticker whose newest
    thesis predates this instant was not successfully scanned for the
    session now in progress, whatever the reason.

    Yesterday's starts are candidates too, because the US overnight window
    begins at 20:00 and runs past midnight — at 02:00 ET the session that is
    running started the previous calendar day.
    """
    market = market.upper()
    tz = MARKET_TZ.get(market)
    windows = session_starts(market)
    if tz is None or not windows:
        return None

    # A shut market has no current session, so nothing can be "behind" one.
    # Without this the function happily reports Saturday 04:00 ET as a
    # session start, and the gap-filler would then treat every ticker as
    # uncovered all weekend and re-scan the whole watchlist every 30 minutes
    # for two days. `session_of` carries the weekend rule (and, when a
    # market is between windows, the CLOSED gaps), so defer to it rather
    # than re-deriving it here.
    if session_of(market, now) is Session.CLOSED:
        return None

    local = _as_utc(now).astimezone(tz)
    best: datetime | None = None
    # Two days back is enough for any window that wraps a single midnight,
    # and covers a Monday morning whose overnight session opened on Sunday.
    for days_back in (0, 1, 2):
        day = (local - timedelta(days=days_back)).date()
        for window in windows:
            start = datetime.combine(day, window.start, tzinfo=tz)
            if start <= local and (best is None or start > best):
                best = start
    return best.astimezone(timezone.utc) if best else None


def is_open(market: str, now: datetime | None = None) -> bool:
    """True only during continuous/regular trading (not pre/post)."""
    return session_of(market, now) is Session.OPEN


def describe(code: str, now: datetime | None = None) -> dict:
    """Everything a setup row / AI prompt needs to know about freshness."""
    market = market_of(code)
    return {
        "market": market,
        "session": session_of(market, now).value,
        "is_delayed_data": is_delayed_data(market),
        "delay_minutes": delay_minutes(market),
        "data_as_of": data_as_of(market, now),
    }


def _as_utc(now: datetime | None) -> datetime:
    """Normalise to an aware UTC datetime; naive input is assumed UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
