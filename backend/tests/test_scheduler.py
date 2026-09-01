"""Checks for the scheduler's job wiring — no live scans, no model calls.

Run from backend/:  .venv/bin/python -m tests.test_scheduler

What matters here is scheduling arithmetic and mutual exclusion: that each
premarket job fires the right local time before its own market's open in
its own timezone (both hemispheres' DST), and that two jobs can never scan
concurrently.
"""

import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

from app import db

# Redirect the DB BEFORE importing the scheduler, which reads persisted
# rotation state at start_scheduler(). Without this the test read the live
# trading.db and asserted "rotation starts paused" against whatever the user
# had last chosen — so pressing Resume in the product made the test fail
# while the code was behaving exactly as designed.
_tmp = tempfile.mkdtemp(prefix="scheduler-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app import scheduler                                        # noqa: E402
from app.config import settings                                  # noqa: E402
from app.utils import market_hours
from app.utils.market_hours import MARKET_TZ                     # noqa: E402

from tests.harness import check, report  # noqa: E402


# --- premarket offset arithmetic --------------------------------------
h, m = scheduler._premarket_time("US", 45)
check("US premarket is 45m before 09:30 ET", (h, m) == (8, 45), f"{h:02d}:{m:02d}")
h, m = scheduler._premarket_time("AU", 45)
check("AU premarket is 45m before 10:00 AEST", (h, m) == (9, 15), f"{h:02d}:{m:02d}")
h, m = scheduler._premarket_time("HK", 45)
check("HK premarket is 45m before 09:30 HKT", (h, m) == (8, 45), f"{h:02d}:{m:02d}")
h, m = scheduler._premarket_time("US", 60)
check("a 60m lead crosses the hour correctly", (h, m) == (8, 30), f"{h:02d}:{m:02d}")

# --- jobs are registered, one per session per market, on the right tz ---
sched = scheduler.start_scheduler(interval_seconds=3600)
try:
    jobs = {j.id: j for j in sched.get_jobs()}
    check("gap-filler job exists", scheduler.JOB_ID in jobs)
    for market in scheduler.SESSION_MARKETS:
        for window in market_hours.session_starts(market):
            jid = scheduler._session_job_id(market, window)
            check(f"{market}/{window.session.value} session job registered",
                  jid in jobs, jid)
            if jid in jobs:
                tz = jobs[jid].trigger.timezone
                check(f"{market}/{window.session.value} uses its own timezone",
                      str(tz) == str(MARKET_TZ[market]), f"{tz}")

    # HK has two OPEN windows either side of the recess. They must be two
    # distinct jobs — a shared id would let the afternoon one silently
    # replace the morning one at registration.
    hk_open = [scheduler._session_job_id("HK", w)
               for w in market_hours.session_starts("HK")
               if w.session is market_hours.Session.OPEN]
    check("HK's two OPEN sessions get distinct job ids",
          len(hk_open) == 2 and len(set(hk_open)) == 2, str(hk_open))

    # --- the news job is separate from the scan machinery -------------
    # It makes outbound HTTPS calls and touches neither OpenD nor the GPU, so
    # decisions #16's paused-on-boot default does not apply to it.
    check("news refresh job is registered", scheduler.NEWS_JOB_ID in jobs)
    news_job = jobs.get(scheduler.NEWS_JOB_ID)
    check("the news job starts ACTIVE, unlike the rotation",
          news_job is not None and news_job.next_run_time is not None)
    check("its first run is delayed, so a crash loop cannot hammer 16 feeds",
          news_job is not None and news_job.next_run_time
          > datetime.now(ZoneInfo("UTC")),
          str(news_job.next_run_time) if news_job else "no job")
    check("the news lock is NOT the scan lock",
          scheduler._news_lock is not scheduler._scan_lock,
          "sharing them would let a news refresh delay a pre-market scan")

    # --- rotation starts paused; premarket jobs do NOT ----------------
    # Automatic scanning now starts ACTIVE unless explicitly paused. This is
    # a deliberate change from decisions #16's paused default: that protected
    # against a 60-second rotation seizing OpenD and the GPU on an unattended
    # boot, and there is no longer a 60-second rotation to protect against.
    # The persisted-pause branch is covered below.
    status = scheduler.scheduler_status()
    check("scanning starts active with no state on record",
          status["paused"] is False, str(status["paused"]))
    check("with no persisted state, the source is the default",
          status.get("rotation_state_source") == "default",
          str(status.get("rotation_state_source")))
    check("session scans are active on boot",
          all(not j["paused"] for j in status["session_scans"]),
          f"{[j['id'] for j in status['session_scans'] if j['paused']]} paused")

    # One job per scannable session per market, not one per market: four for
    # the US (pre/open/post/overnight), three for HK (the afternoon reopen
    # after the recess is its own session start), two for AU.
    expected = sum(len(market_hours.session_starts(m))
                   for m in scheduler.SESSION_MARKETS)
    check("one job per (market, session start)",
          len(status["session_scans"]) == expected,
          f"{len(status['session_scans'])} jobs, expected {expected}")
    check("the US contributes four", len(market_hours.session_starts("US")) == 4)
    check("HK contributes three, skipping the recess",
          len(market_hours.session_starts("HK")) == 3)
    check("status names the soonest upcoming session scan",
          status["next_session_scan"] is not None)
    check("the legacy `premarket` key still carries them",
          len(status["premarket"]) == expected)

    # --- every session job fires exactly `lead` before its own session ---
    for market in scheduler.SESSION_MARKETS:
        for window in market_hours.session_starts(market):
            job = jobs[scheduler._session_job_id(market, window)]
            nxt = job.next_run_time.astimezone(MARKET_TZ[market])
            local_start = nxt.replace(hour=window.start.hour,
                                      minute=window.start.minute,
                                      second=0, microsecond=0)
            delta = (local_start - nxt).total_seconds() / 60
            # A scan that fires before midnight for a session starting after
            # it lands a day early on the clock; normalise the wrap.
            if delta < 0:
                delta += 24 * 60
            check(f"{market}/{window.session.value} fires "
                  f"{settings.session_lead_minutes}m before its session",
                  abs(delta - settings.session_lead_minutes) < 1,
                  f"next {nxt.strftime('%Y-%m-%d %H:%M %Z')}, "
                  f"session {window.start}, delta {delta:.0f}m")

    # The overnight scan is the one that may legitimately land on a Sunday.
    for market in scheduler.SESSION_MARKETS:
        for window in market_hours.session_starts(market):
            if window.session is market_hours.Session.OVERNIGHT:
                continue
            job = jobs[scheduler._session_job_id(market, window)]
            nxt = job.next_run_time.astimezone(MARKET_TZ[market])
            check(f"{market}/{window.session.value} next run is a weekday",
                  nxt.weekday() < 5, nxt.strftime("%a"))

    # --- DST: the local wall-clock time must hold across the year -----
    # US and AU shift DST in opposite directions; a fixed UTC offset would
    # drift by an hour for half the year on each.
    for market, tzname in (("US", "America/New_York"), ("AU", "Australia/Sydney")):
        for window in market_hours.session_starts(market):
            job = jobs[scheduler._session_job_id(market, window)]
            want_h, want_m = scheduler._lead_time(
                window.start, settings.session_lead_minutes)
            for probe in ("2026-01-15 00:00", "2026-07-15 00:00"):
                after = datetime.strptime(probe, "%Y-%m-%d %H:%M").replace(
                    tzinfo=ZoneInfo(tzname))
                fire = job.trigger.get_next_fire_time(None, after).astimezone(
                    ZoneInfo(tzname))
                check(f"{market}/{window.session.value} holds "
                      f"{want_h:02d}:{want_m:02d} local at {probe[:7]}",
                      (fire.hour, fire.minute) == (want_h, want_m),
                      f"{fire.strftime('%H:%M %Z')}")
finally:
    scheduler.shutdown_scheduler()

# --- mutual exclusion: two scans must never run concurrently ----------
# The premarket job now skips a market with nothing enabled (it would sync
# the watchlist and then scan zero tickers), so give it something to find —
# otherwise it short-circuits before ever taking the lock.
with db.get_connection() as _conn:
    _conn.execute(
        "INSERT OR REPLACE INTO watchlist_cache (code, name, market, enabled,"
        " last_synced_at, updated_at) VALUES ('US.TEST','Test','US',1,?,?)",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
check("a session scan skips a market with nothing enabled",
      (scheduler._run_session_scan("HK", "open") is None)
      and "no enabled tickers" in str(scheduler._last_premarket.get("HK")),
      str(scheduler._last_premarket.get("HK")))

scheduler._scan_lock = threading.Lock()
entered: list[str] = []


def fake_cycle(*a, **kw):
    entered.append("start")
    time.sleep(0.4)
    raise RuntimeError("stop before touching OpenD")


_real = scheduler.scanner.run_cycle
_real_uncovered = scheduler.uncovered_tickers
scheduler.scanner.run_cycle = fake_cycle
scheduler.get_gateway = lambda: None
# Pin the gap-filler's work list. Otherwise this test asserts lock behaviour
# but silently depends on the wall clock: `uncovered_tickers` returns nothing
# when the market is shut, so the whole block would pass vacuously every
# weekend and only ever fail on a Monday.
scheduler.uncovered_tickers = lambda *a, **kw: ["US.TEST"]
try:
    t = threading.Thread(target=scheduler._run_session_scan, args=("US", "open"))
    t.start()
    time.sleep(0.1)                       # the session scan now holds the lock
    check("lock is reported held while scanning", scheduler._scan_lock.locked())
    scheduler._run_gap_filler()           # must skip, not block or run
    check("the gap-filler skips while a session scan holds the lock",
          len(entered) == 1, f"{len(entered)} scans entered")
    t.join()
    check("lock is released after the scan", not scheduler._scan_lock.locked())
    scheduler._run_gap_filler()
    check("the gap-filler runs once the lock is free", len(entered) == 2)

    # And it does nothing at all when every ticker is already covered —
    # which is the normal case, and the whole point of it being a repair
    # path rather than a rotation.
    scheduler.uncovered_tickers = lambda *a, **kw: []
    scheduler._run_gap_filler()
    check("the gap-filler scans nothing when everything is covered",
          len(entered) == 2, f"{len(entered)} scans entered")
    check("...and says so rather than reporting an empty run",
          "covered" in str(scheduler._last_result),
          str(scheduler._last_result))
finally:
    scheduler.scanner.run_cycle = _real
    scheduler.uncovered_tickers = _real_uncovered

# --- news and scans do not block each other ---------------------------
scheduler._scan_lock = threading.Lock()
scheduler._news_lock = threading.Lock()
scheduler._scan_lock.acquire()
check("a news refresh can start while a scan holds the scan lock",
      scheduler.acquire_news_lock() is True)
check("a second news refresh is refused, not queued",
      scheduler.acquire_news_lock() is False)
scheduler.release_news_lock()
scheduler._scan_lock.release()
scheduler.release_news_lock()   # must warn, not raise
check("double release of the news lock is survivable",
      not scheduler._news_lock.locked())

# --- the manual scan path shares the same lock ------------------------
# routers/scan.py used to call run_cycle directly, so a manual scan could
# run alongside a rotation cycle and both would contend on the gateway lock.
scheduler._scan_lock = threading.Lock()
check("manual path can claim the lock", scheduler.acquire_scan_lock() is True)
check("a second claim is refused, not queued",
      scheduler.acquire_scan_lock() is False)
check("scheduled jobs see the manual scan as in progress",
      scheduler._scan_lock.locked())
scheduler.release_scan_lock()
check("lock frees after the manual scan", not scheduler._scan_lock.locked())
scheduler.release_scan_lock()   # must warn, not raise
check("double release is survivable", not scheduler._scan_lock.locked())

# --- persisted rotation state survives a restart ----------------------
# The whole point: a supervised service that restarts at 04:00 must not
# silently forget that the user turned scanning on.
for desired, expect_paused in (("1", False), ("0", True)):
    db.set_app_state(scheduler.ROTATION_STATE_KEY, desired)
    scheduler._scheduler = None
    sched2 = scheduler.start_scheduler(interval_seconds=3600)
    try:
        st = scheduler.scheduler_status()
        check(f"rotation_enabled={desired!r} -> paused={expect_paused}",
              st["paused"] is expect_paused,
              f"paused={st['paused']} source={st.get('rotation_state_source')}")
        check(f"rotation_enabled={desired!r} reports a persisted source",
              st.get("rotation_state_source") == "persisted")
        if not expect_paused:
            # Delayed so a crash loop cannot fire a scan on every restart.
            delay = (sched2.get_job(scheduler.JOB_ID).next_run_time
                     - datetime.now(ZoneInfo("UTC"))).total_seconds()
            check("a resumed rotation delays its first cycle",
                  delay >= scheduler.RESUME_GRACE_SECONDS - 5, f"{delay:.0f}s")
    finally:
        scheduler.shutdown_scheduler()

# pause()/resume() must write through, or the state cannot survive anything.
scheduler._scheduler = None
scheduler.start_scheduler(interval_seconds=3600)
try:
    scheduler.resume()
    check("resume() persists", db.get_app_state(scheduler.ROTATION_STATE_KEY) == "1")
    scheduler.pause()
    check("pause() persists", db.get_app_state(scheduler.ROTATION_STATE_KEY) == "0")

    # Pause now covers the SESSION SCANS too, not just the gap-filler.
    # Before session scans existed, pause() named one job, so a "paused"
    # scanner still ran a full watchlist scan before the open. Leaving that
    # in place would make the sidebar's "theses will not refresh until you
    # resume" a lie four times a day — and that component exists precisely
    # because scanning state being invisible was a real outage.
    paused_status = scheduler.scheduler_status()
    check("pause() stops every session scan, not just the gap-filler",
          all(j["paused"] for j in paused_status["session_scans"]),
          f"{[j['id'] for j in paused_status['session_scans'] if not j['paused']]} "
          "still scheduled")
    check("pause() stops the gap-filler as well", paused_status["paused"] is True)

    scheduler.resume()
    resumed = scheduler.scheduler_status()
    check("resume() brings the session scans back",
          all(not j["paused"] for j in resumed["session_scans"]),
          f"{[j['id'] for j in resumed['session_scans'] if j['paused']]} still held")
    check("resume() brings the gap-filler back", resumed["paused"] is False)

    # The nightly scorecard job was invisible in every status surface until
    # it got a view of its own.
    check("the thesis-scoring job is reported", resumed["scorecard"]["running"] is True)
    check("...with its next run", resumed["scorecard"]["next_run"] is not None)

    # An explicit interval override used to be swallowed: status reported
    # settings.scan_interval_seconds regardless of what the job actually ran.
    check("status reports the LIVE gap-filler interval, not the setting",
          resumed["interval_seconds"] == 3600,
          f"{resumed['interval_seconds']}s")

    # --- the sector rotation job ---------------------------------------
    check("the sector job is registered", resumed["sector"]["running"] is True)
    check("...with its next run", resumed["sector"]["next_run"] is not None)

    sector_job = scheduler._scheduler.get_job(scheduler.SECTOR_JOB_ID)
    check("the sector job cannot overlap itself", sector_job.max_instances == 1)
    check("...and coalesces a backlog into one run", sector_job.coalesce is True)
    check("...with a full day of misfire grace",
          sector_job.misfire_grace_time == 86400,
          "the kline call backfills, so running late is as good as on time")
    check("the sector job runs on the US market clock, not UTC",
          str(sector_job.trigger.timezone) == "America/New_York",
          f"{sector_job.trigger.timezone} — a UTC schedule would drift an hour "
          "twice a year relative to the session it follows")

    # The Pause button means "stop generating theses". A bookkeeping job that
    # spends no GPU must not be caught by it — #68 already had to widen this
    # list once and widening it again would make the button mean a third thing.
    check("Pause does NOT govern the sector job",
          scheduler.SECTOR_JOB_ID not in scheduler._scan_job_ids(),
          f"governed: {scheduler._scan_job_ids()}")
    scheduler.pause()
    paused_status = scheduler.scheduler_status()
    check("...so it keeps its next run through a pause",
          paused_status["sector"]["next_run"] is not None,
          "unlike the session scans, which are held")
    check("...while the session scans ARE held",
          all(j["paused"] for j in paused_status["session_scans"]))
    scheduler.resume()

    # --- the sector narrative job --------------------------------------
    check("the sector narrative job is registered",
          resumed["sector_narrative"]["running"] is True)
    check("...with its next run", resumed["sector_narrative"]["next_run"] is not None)

    narr = scheduler._scheduler.get_job(scheduler.SECTOR_NARRATIVE_JOB_ID)
    flow = scheduler._scheduler.get_job(scheduler.SECTOR_JOB_ID)
    # Compared on the actual computed fire times, not on trigger internals:
    # an `if ... else True` guard here would let the check pass vacuously,
    # which is the failure mode this suite exists to avoid.
    check("...scheduled AFTER the sector refresh it narrates",
          narr.next_run_time > flow.next_run_time,
          f"flow {flow.next_run_time} then narrative {narr.next_run_time} — "
          "so it reads tonight's scores rather than yesterday's")
    check("...by less than an hour, not a day",
          (narr.next_run_time - flow.next_run_time).total_seconds() == 45 * 60,
          "far enough for the refresh to finish, close enough that the board "
          "it describes is still the current one")
    check("...on the US market clock",
          str(narr.trigger.timezone) == "America/New_York")
    check("...with a SHORTER misfire grace than the scorecard's day",
          narr.misfire_grace_time == 6 * 3600,
          "a narrative is about the move on a given date, so running "
          "tomorrow afternoon would describe a stale board")
    check("Pause does not govern it either",
          scheduler.SECTOR_NARRATIVE_JOB_ID not in scheduler._scan_job_ids())

    # It makes ZERO OpenD calls, so unlike the flow job it must NOT yield to a
    # scan — waiting would delay it for nothing (decisions #51).
    scheduler._scan_lock.acquire()
    try:
        before = scheduler._last_sector_narrative
        scheduler._run_sector_narratives()
        check("the narrative job RUNS while a scan holds the gateway",
              scheduler._last_sector_narrative is not before,
              "it reads stored rows only — taking the quote mutex would "
              "delay a pre-market scan for nothing")
    finally:
        scheduler._scan_lock.release()

    # It must yield to a scan rather than contend for the OpenD context.
    scheduler._scan_lock.acquire()
    try:
        before = scheduler._last_sector
        scheduler._run_sector_flow()
        check("the sector job SKIPS while a scan holds the gateway",
              scheduler._last_sector is before,
              "non-blocking acquire, like the scorecard and earnings refresh")
    finally:
        scheduler._scan_lock.release()

    # --- the shared job wrapper's three paths -------------------------
    # Every periodic job runs through `_guarded_job`, so these are the
    # contract for all seven rather than for the one exercised here.
    from app.services import news_service

    # A skipped tick must LEAVE the previous result standing. Overwriting it
    # with a skip marker would make a held lock indistinguishable from a run
    # that produced nothing, in exactly the surface (/scan/status) that
    # exists to tell those apart.
    scheduler._last_news = {"sentinel": "previous run"}
    scheduler._news_lock.acquire()
    try:
        scheduler._run_news_refresh()
    finally:
        scheduler._news_lock.release()
    check("a skipped job leaves the previous result standing",
          scheduler._last_news == {"sentinel": "previous run"},
          str(scheduler._last_news))
    check("...and releases nothing it did not take",
          not scheduler._news_lock.locked())

    _real_refresh = news_service.refresh
    try:
        def _boom():
            raise RuntimeError("boom")
        news_service.refresh = _boom
        scheduler._run_news_refresh()
        check("a raising job records the error instead of escaping",
              scheduler._last_news == {"error": "boom"}, str(scheduler._last_news))
        check("...and still releases the lock",
              not scheduler._news_lock.locked(),
              "a leaked lock here would silently stop every later tick")

        news_service.refresh = lambda: {"ok": 1}
        scheduler._run_news_refresh()
        check("a successful job records its result",
              scheduler._last_news == {"ok": 1}, str(scheduler._last_news))
    finally:
        news_service.refresh = _real_refresh
finally:
    scheduler.shutdown_scheduler()

report("scheduler")
