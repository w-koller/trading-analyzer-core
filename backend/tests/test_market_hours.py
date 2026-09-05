"""Functional checks for market_hours. Run: .venv/bin/python -m tests.test_market_hours"""

from datetime import datetime, time, timedelta, timezone

from app.utils.market_hours import (
    MARKET_HOURS,
    last_session_start,
    next_regular_close,
    next_start_of,
    next_transition,
    session_outlook,
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

# --- the forward direction ---
#
# These pin `next_transition` / `next_start_of` / `next_regular_close`, which
# are defined as "the next instant `session_of` changes its answer" and are
# implemented by PROBING `session_of` rather than walking the window table.
# Each check below is one that fails silently if the probe's candidate set is
# built wrong, so the labels say what is actually at stake.

# Mid-session: the next thing that happens is the close, not tomorrow.
check("next_transition mid-session", next_transition("US", utc(2026, 8, 20, 14, 0)),
      (utc(2026, 8, 20, 20, 0), Session.POST_MARKET))

# Strictly forward. At the instant a session opens, the answer is its END —
# never itself, or a caller counting down would restart at zero.
check("next_transition at the open is the close",
      next_transition("US", utc(2026, 8, 20, 13, 30)),
      (utc(2026, 8, 20, 20, 0), Session.POST_MARKET))

# The US overnight window wraps midnight: 20:00 -> 04:00 ET.
check("next_transition across the wrap", next_transition("US", utc(2026, 8, 21, 2, 0)),
      (utc(2026, 8, 21, 8, 0), Session.PRE_MARKET))

# THE MIDNIGHT-ONLY TRANSITION. Friday 23:00 ET is inside the overnight
# window, and what ends it is Saturday 00:00 ET — the weekend guard, which is
# NOT a window boundary. Without local midnights in the candidate set this
# function is wrong every Friday night and nothing else notices.
check("Friday night ends at local midnight, not at a window edge",
      next_transition("US", utc(2026, 8, 22, 3, 0)),
      (utc(2026, 8, 22, 4, 0), Session.CLOSED))

# THE TABLE/session_of DISAGREEMENT. `Window(OVERNIGHT, days="sun,...")` says
# the overnight session starts Sunday evening; `session_of` says CLOSED,
# because the weekend guard fires first. A walk over the table would answer
# Sunday 20:00 ET. The probe follows session_of, so it answers Monday 00:00.
check("from Saturday the next change is MONDAY midnight, not Sunday evening",
      next_transition("US", utc(2026, 8, 22, 18, 0)),
      (utc(2026, 8, 24, 4, 0), Session.OVERNIGHT))

# next_start_of is strictly forward: asked while OPEN it gives the NEXT open,
# which is what stops a display saying "opens in 23h" during trading.
check("next_start_of skips the session already running",
      next_start_of("US", Session.OPEN, utc(2026, 8, 20, 15, 0)),
      utc(2026, 8, 21, 13, 30))
check("next_start_of from a Saturday lands on Monday",
      next_start_of("US", Session.OPEN, utc(2026, 8, 22, 18, 0)),
      utc(2026, 8, 24, 13, 30))

# LUNCH IS NOT THE CLOSE. HKEX breaks 12:00-13:00; a countdown to the next
# TRANSITION would be four hours early every morning, so next_regular_close
# reads the derived close instead.
check("HK next_transition in the morning is the lunch break",
      next_transition("HK", utc(2026, 8, 20, 2, 0)),
      (utc(2026, 8, 20, 4, 0), Session.BREAK))
check("HK next_regular_close spans the recess",
      next_regular_close("HK", utc(2026, 8, 20, 2, 0)), utc(2026, 8, 20, 8, 0))

# AU's 16:12 post-close ends a window and begins none, so an END has to be a
# candidate in its own right.
check("AU window ENDS are candidates too",
      next_transition("AU", utc(2026, 8, 20, 6, 5)),
      (utc(2026, 8, 20, 6, 12), Session.CLOSED))

# DST, both hemispheres. The answer is an INSTANT, so it shifts against UTC
# when the exchange's offset changes while the wall clock does not.
check("US open before spring-forward", next_start_of("US", Session.OPEN, utc(2026, 3, 5, 22, 0)),
      utc(2026, 3, 6, 14, 30))
check("US open after spring-forward", next_start_of("US", Session.OPEN, utc(2026, 3, 9, 3, 0)),
      utc(2026, 3, 9, 13, 30))
check("AU open before AEDT starts", next_start_of("AU", Session.OPEN, utc(2026, 10, 1, 12, 0)),
      utc(2026, 10, 2, 0, 0))
check("AU open after AEDT starts", next_start_of("AU", Session.OPEN, utc(2026, 10, 4, 12, 0)),
      utc(2026, 10, 4, 23, 0))

# An unmodelled market has no answer, and says so rather than falling back.
check("next_transition on an unknown market", next_transition("SG"), None)
check("next_start_of on an unknown market", next_start_of("SG", Session.OPEN), None)
check("next_regular_close on an unknown market", next_regular_close("SG"), None)
check("session_outlook on an unknown market", session_outlook("SG"), None)

# The horizon is a real bound, not decoration. From a Saturday the next change
# is Monday midnight; asked to look only at today, the honest answer is None
# rather than a walk that keeps going until it finds something.
check("a horizon too short to reach the answer returns None",
      next_transition("US", utc(2026, 8, 22, 18, 0), horizon_days=0), None)
check("...and a horizon that reaches it does not",
      next_transition("US", utc(2026, 8, 22, 18, 0), horizon_days=3),
      (utc(2026, 8, 24, 4, 0), Session.OVERNIGHT))

# THE PROPERTY THE WHOLE DESIGN EXISTS FOR: the forward walk can never
# disagree with session_of. Sampled across a full week at 37-minute steps, so
# every window edge, both midnights and the weekend are crossed.
_disagreements = []
_t = utc(2026, 8, 17, 0, 0)
for _ in range(280):
    _nxt = next_transition("US", _t)
    if _nxt is None:
        _disagreements.append(("no transition", _t))
    else:
        _at, _to = _nxt
        if session_of("US", _at) is not _to:
            _disagreements.append(("lands on a different session", _t))
        if session_of("US", _at - timedelta(seconds=1)) is not session_of("US", _t):
            _disagreements.append(("changes before it says it does", _t))
    _t += timedelta(minutes=37)
check("the forward walk never disagrees with session_of", _disagreements, [])

# session_outlook is the bundle a display reads; the shape is the contract.
_out = session_outlook("US", utc(2026, 8, 20, 14, 0))
check("outlook reports the running session", _out["session"], "open")
check("outlook agrees with is_open", _out["is_open"], True)
check("outlook names the exchange zone", _out["market_tz"], "America/New_York")
check("outlook `since` matches last_session_start",
      _out["since"], last_session_start("US", utc(2026, 8, 20, 14, 0)))
check("outlook states the calendar's limit rather than leaving it to callers",
      _out["holidays_modelled"], False)
check("outlook `since` is None when the market is shut",
      session_outlook("US", utc(2026, 8, 22, 18, 0))["since"], None)


report("market_hours")
