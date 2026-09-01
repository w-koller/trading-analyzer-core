"""Functional checks for market_hours. Run: .venv/bin/python -m tests.test_market_hours"""

from datetime import datetime, time, timezone

from app.utils.market_hours import (
    MARKET_HOURS,
    last_session_start,
    session_starts,
    Session,
    data_as_of,
    delay_minutes,
    describe,
    is_delayed_data,
    is_open,
    market_of,
    session_of,
)

from tests.harness import check_eq, report


def check(label: str, got, want) -> None:
    """Quiet equality check — failures only.

    These 30 assertions are a dense table of session/DST boundaries; a PASS
    line each would bury the one line that matters. `quiet=True` keeps the
    got/want detail by folding it into the recorded failure instead of
    printing it.
    """
    check_eq(label, got, want, quiet=True)


def utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# --- code parsing ---
check("market_of US", market_of("US.AAPL"), "US")
check("market_of AU", market_of("AU.BHP"), "AU")
check("market_of lowercase", market_of("au.bhp"), "AU")
check("market_of bare", market_of("AAPL"), "")
check("market_of empty", market_of(""), "")

# --- delay semantics (rule #7) ---
check("US realtime", delay_minutes("US"), 0)
check("AU delayed 15m", delay_minutes("AU"), 15)
check("unknown market defaults delayed", delay_minutes("XX"), 15)
check("US not delayed", is_delayed_data("US"), False)
check("AU is delayed", is_delayed_data("AU"), True)

now = utc(2026, 8, 20, 3, 30)  # Thursday
check("US data_as_of == now", data_as_of("US", now), now)
check("AU data_as_of == now-15m", data_as_of("AU", now), utc(2026, 8, 20, 3, 15))

# --- US sessions (ET = UTC-4 in August) ---
check("US regular 10:00 ET", session_of("US", utc(2026, 8, 20, 14, 0)), Session.OPEN)
check("US pre 05:00 ET", session_of("US", utc(2026, 8, 20, 9, 0)), Session.PRE_MARKET)
check("US post 17:00 ET", session_of("US", utc(2026, 8, 20, 21, 0)), Session.POST_MARKET)
# 22:00 ET used to be CLOSED. It is the US overnight session, and calling it
# closed told the AI prompt the market was shut during a session that trades.
check("US overnight 22:00 ET", session_of("US", utc(2026, 8, 21, 2, 0)), Session.OVERNIGHT)
check("US open bell 09:30 ET", session_of("US", utc(2026, 8, 20, 13, 30)), Session.OPEN)
check("US close 16:00 ET is post", session_of("US", utc(2026, 8, 20, 20, 0)), Session.POST_MARKET)

# --- AU sessions (AEST = UTC+10 in August) ---
check("AU regular 11:00 AEST", session_of("AU", utc(2026, 8, 20, 1, 0)), Session.OPEN)
check("AU pre 08:00 AEST", session_of("AU", utc(2026, 8, 19, 22, 0)), Session.PRE_MARKET)
check("AU post 16:05 AEST", session_of("AU", utc(2026, 8, 20, 6, 5)), Session.POST_MARKET)
check("AU closed 18:00 AEST", session_of("AU", utc(2026, 8, 20, 8, 0)), Session.CLOSED)

# --- weekends closed everywhere ---
check("US Saturday closed", session_of("US", utc(2026, 8, 22, 14, 0)), Session.CLOSED)
check("AU Sunday closed", session_of("AU", utc(2026, 8, 23, 1, 0)), Session.CLOSED)

# --- DST correctness: January, EST = UTC-5, AEDT = UTC+11 ---
check("US Jan 10:00 EST", session_of("US", utc(2026, 1, 15, 15, 0)), Session.OPEN)
check("US Jan 14:00 UTC is pre", session_of("US", utc(2026, 1, 15, 14, 0)), Session.PRE_MARKET)
check("AU Jan 11:00 AEDT", session_of("AU", utc(2026, 1, 15, 0, 0)), Session.OPEN)

# --- is_open excludes pre/post ---
check("is_open pre = False", is_open("US", utc(2026, 8, 20, 9, 0)), False)
check("is_open regular = True", is_open("US", utc(2026, 8, 20, 14, 0)), True)

# --- unknown market is closed + delayed, not crash ---
# NB: HK used to stand in for "unknown" here. It is a supported market now
# (the schema always allowed it, and the premarket scheduler covers it), so
# this needs a market we genuinely do not model.
check("unknown session", session_of("SG", utc(2026, 8, 20, 3, 0)), Session.CLOSED)
check("unknown market is treated as delayed", is_delayed_data("SG"), True)

# --- HK is now modelled: 09:30-16:00 HKT (UTC+8), delayed like AU ---
check("HK open at 02:00 UTC (10:00 HKT)", session_of("HK", utc(2026, 8, 20, 2, 0)), Session.OPEN)
check("HK closed at 12:00 UTC (20:00 HKT)", session_of("HK", utc(2026, 8, 20, 12, 0)), Session.CLOSED)
check("HK quotes are delayed", is_delayed_data("HK"), True)

# The midday recess. This is the gap the old single-block model reported as
# OPEN, and the specific reason CLAUDE.md forbade gating on session_of until
# it was fixed. Boundaries checked on both sides, since a half-open interval
# is exactly where this goes wrong.
check("HK morning ends at 12:00 HKT", session_of("HK", utc(2026, 8, 20, 3, 59)), Session.OPEN)
check("HK 12:00 HKT is the recess", session_of("HK", utc(2026, 8, 20, 4, 0)), Session.BREAK)
check("HK 12:30 HKT is the recess", session_of("HK", utc(2026, 8, 20, 4, 30)), Session.BREAK)
check("HK 13:00 HKT reopens", session_of("HK", utc(2026, 8, 20, 5, 0)), Session.OPEN)
check("HK afternoon runs to 16:00", session_of("HK", utc(2026, 8, 20, 7, 59)), Session.OPEN)
check("HK pre-open auction 09:00 HKT", session_of("HK", utc(2026, 8, 20, 1, 0)), Session.PRE_MARKET)
check("HK closing auction 16:05 HKT", session_of("HK", utc(2026, 8, 20, 8, 5)), Session.POST_MARKET)

# --- the US overnight window, including the midnight wrap --------------
# 20:00-04:00 ET is one window spanning two calendar days, which a flat
# tuple of four times could not represent at all.
check("US 20:00 ET starts overnight", session_of("US", utc(2026, 8, 21, 0, 0)), Session.OVERNIGHT)
check("US 23:59 ET still overnight", session_of("US", utc(2026, 8, 21, 3, 59)), Session.OVERNIGHT)
check("US 00:30 ET is still overnight", session_of("US", utc(2026, 8, 21, 4, 30)), Session.OVERNIGHT)
check("US 03:59 ET is the last overnight minute", session_of("US", utc(2026, 8, 21, 7, 59)), Session.OVERNIGHT)
check("US 04:00 ET hands over to pre-market", session_of("US", utc(2026, 8, 21, 8, 0)), Session.PRE_MARKET)
# The weekend guard still wins over a wrapping window: Saturday 01:00 ET is
# inside the overnight window by clock alone, but the market is shut.
check("Saturday is closed even mid-overnight-window",
      session_of("US", utc(2026, 8, 22, 5, 0)), Session.CLOSED)

# --- derived MARKET_HOURS still matches the shape callers index ---------
# scheduler.py and test_scheduler.py both read MARKET_HOURS[market][1]
# positionally. It is derived from MARKET_SESSIONS now, so pin the mapping.
check("US regular open derived correctly", MARKET_HOURS["US"][1], time(9, 30))
check("HK regular open derived correctly", MARKET_HOURS["HK"][1], time(9, 30))
check("HK regular close spans the recess", MARKET_HOURS["HK"][2], time(16, 0))
check("AU regular open derived correctly", MARKET_HOURS["AU"][1], time(10, 0))

# --- the scan triggers the scheduler builds crons from ------------------
check("US has four scannable session starts", len(session_starts("US")), 4)
check("HK has three (pre, morning, post-lunch)", len(session_starts("HK")), 3)
check("AU has two", len(session_starts("AU")), 2)
check("the HK recess is never scanned ahead of",
      [w.session for w in session_starts("HK")].count(Session.BREAK), 0)
# Spelled as a list because APScheduler numbers days mon=0..sun=6, so the
# range "sun-thu" is descending and raises at registration.
check("US overnight scan skips Friday evening",
      [w.days for w in session_starts("US") if w.session is Session.OVERNIGHT],
      ["sun,mon,tue,wed,thu"])
check("an unmodelled market yields no triggers", session_starts("SG"), ())

# --- last_session_start: what "covered" means to the gap-filler --------
check("mid-session, the session began at its own start",
      last_session_start("US", utc(2026, 8, 20, 14, 0)), utc(2026, 8, 20, 13, 30))
check("during overnight, the start is the previous evening",
      last_session_start("US", utc(2026, 8, 21, 4, 30)), utc(2026, 8, 21, 0, 0))
check("after the afternoon reopen, HK reports 13:00 HKT",
      last_session_start("HK", utc(2026, 8, 20, 6, 0)), utc(2026, 8, 20, 5, 0))
check("an unmodelled market has no session start",
      last_session_start("SG", utc(2026, 8, 20, 6, 0)), None)
# A shut market has no current session. Without this guard the function
# reports Saturday 04:00 ET as a session start, and the gap-filler treats
# every ticker as uncovered all weekend — re-scanning the whole watchlist
# every 30 minutes for two days.
check("a Saturday has no current session",
      last_session_start("US", utc(2026, 8, 22, 16, 0)), None)
check("a Sunday has no current session",
      last_session_start("US", utc(2026, 8, 23, 16, 0)), None)
# ...but the overnight window that OPENS on Sunday evening does count, and
# its start is on a day session_of itself calls closed. Monday 02:00 ET.
check("Monday's small hours belong to Sunday evening's overnight session",
      last_session_start("US", utc(2026, 8, 24, 6, 0)), utc(2026, 8, 24, 0, 0))
check("HK's recess still reports the morning session that covered it",
      last_session_start("HK", utc(2026, 8, 20, 4, 30)), utc(2026, 8, 20, 1, 30))

# --- describe() shape ---
d = describe("AU.BHP", utc(2026, 8, 20, 1, 0))
check("describe market", d["market"], "AU")
check("describe delayed", d["is_delayed_data"], True)
check("describe session", d["session"], "open")
check("describe as_of", d["data_as_of"], utc(2026, 8, 20, 0, 45))

# --- naive datetime treated as UTC, not crash ---
check("naive input ok", data_as_of("US", datetime(2026, 8, 20, 3, 30)), now)

report("market_hours")
