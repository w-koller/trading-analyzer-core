"""APScheduler wiring: one full scan per trading session, plus a repair path.

**1. Session scans** (`session_scan_<MARKET>_<SESSION>`, cron, one per
scannable session per market). Fires `session_lead_minutes` before that
session opens, in the market's own timezone, and scans the *entire* enabled
watchlist for it. The US contributes four (pre-market, regular, post-market,
overnight), HK three (pre-open, the morning open and the afternoon
resumption after the midday recess), AU two. Session windows and their
timezones come from `market_hours`, so DST on both hemispheres is handled by
the same code the rest of the app uses.

This replaced a 60-second continuous rotation, and the corpus is why.
Indicators are computed from DAILY klines, which do not change intraday, so
re-analysing every ticker hourly mostly resampled the model's own
uncertainty: measured over 2,565 consecutive thesis pairs computed on the
same bar, 24% came back with a different direction — 24% still when spot had
also moved less than 0.25%. It cost 968 theses a day, 7.3 GPU-hours on
qwen3.8 and a 90% duty cycle on deepseek-r1:32b, to produce that. Four
session scans produce ~192/day, and each one lands when something has
actually changed.

**2. Gap-filler** (`gap_filler`, interval). Scans only tickers whose newest
thesis predates the current session's start — i.e. the ones a session scan
did not successfully cover. Normally that is none and it does nothing. It
exists because 8.9% of ticker-scans were failing, and without it a failure
would leave that ticker with no fresh thesis for four to eight hours. It is
a repair path, not a scanning strategy: if it is routinely saturating, the
session scans are failing and that is the thing to fix.

Sizing, because it is not obvious: a full US watchlist is ~48 tickers at
27-90s of inference each, so a session scan runs 21-64 minutes. The lead
time is therefore a *start* offset, not a promise that it finishes before
the session opens — tickers are processed least-recently-analysed first so
the stalest are covered even if the run is still going.

Both job types take `_scan_lock`. They are separate APScheduler jobs, so
`max_instances` cannot stop them overlapping each other — only a shared
lock can. A job that finds the lock held logs and skips rather than
queueing, since a queued scan would just run against staler data later.

No holiday calendar (decisions log #9): these crons fire on exchange
holidays too. The scan simply produces a thesis on unchanged data, which is
wasteful but not wrong; `market_hours` deliberately does not hardcode
calendars that drift every year. OpenD's `get_global_state` IS
holiday-correct and names both the HK recess (`REST`) and the US overnight
session (`OVERNIGHT`), but it reports only the CURRENT state and cannot
generate future fire times, so it can corroborate a label and not drive a
cron.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import settings
from app.services import (earnings_service, news_service, push_service,
                          sector_flow, sector_narrative, thesis_scorecard,
                          scanner)
from app.services.moomoo_gateway import get_gateway
from app.utils import market_hours
from app.utils.market_hours import MARKET_HOURS, MARKET_TZ

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_last_result: dict[str, Any] | None = None
_last_premarket: dict[str, dict[str, Any]] = {}

# Only one scan may touch OpenD at a time, across every job.
_scan_lock = threading.Lock()

# Separate from _scan_lock, deliberately. That lock is strictly OpenD's
# single-threaded-context mutex; a news refresh makes outbound HTTPS calls and
# touches neither OpenD nor the GPU. Sharing them would let a 15-minute news
# job delay a pre-market scan, or be starved by an hour-long one.
_news_lock = threading.Lock()
_last_news: dict[str, Any] | None = None
_last_earnings: dict[str, Any] | None = None
_last_outlooks: dict[str, Any] | None = None
_last_scoring: dict[str, Any] | None = None
NEWS_JOB_ID = "news_refresh"
# A fresh box should have news without a 15-minute wait, but not fire 16
# outbound requests on every restart of a crash loop either.
NEWS_FIRST_RUN_SECONDS = 60

# Its own lock, for the same reason _news_lock is separate — and for one
# more. A push cycle DOES touch OpenD, but through the trade context
# (moomoo_trade_gateway, decisions #21), which is a different context from the
# quote-side one _scan_lock guards and has its own bounded lock. The dashboard
# already proves this is safe: routers/alerts.py, routers/positions.py and
# routers/health.py make the same call with no lock every 30 seconds, right
# through a pre-market scan.
#
# Taking _scan_lock here would be actively harmful, not merely unnecessary: a
# pre-market scan holds it for over an hour (decisions #20), so every alert in
# the hour before the open — precisely the ones worth having — would be
# silently withheld. Silence is the one failure a safety feature cannot have.
_push_lock = threading.Lock()
_last_push: dict[str, Any] | None = None
_last_sector: dict[str, Any] | None = None
_last_sector_narrative: dict[str, Any] | None = None
PUSH_JOB_ID = "push_alerts"
# After news (60s) and earnings (90s), so a cold box staggers its wake-ups.
PUSH_FIRST_RUN_SECONDS = 120

EARNINGS_JOB_ID = "earnings_refresh"
EARNINGS_OUTLOOK_JOB_ID = "earnings_outlooks"
SCORECARD_JOB_ID = "thesis_scoring"
SECTOR_JOB_ID = "sector_flow"
SECTOR_NARRATIVE_JOB_ID = "sector_narratives"
# After news' 60s, so a cold box does not fire both at once.
EARNINGS_FIRST_RUN_SECONDS = 90
EARNINGS_REFRESH_HOURS = 6

# Persisted rotation state. "1" = the user wants the rotation running; "0" =
# they paused it; absent = never expressed, so decisions #16's paused default
# applies. See start_scheduler for the full reasoning.
ROTATION_STATE_KEY = "rotation_enabled"
# How long after boot the first resumed cycle may fire, so a crash loop
# cannot kick off a scan on every restart.
RESUME_GRACE_SECONDS = 120
_rotation_state_source: str = "default"

JOB_ID = "gap_filler"
# How often to look for tickers the session scan missed, and how many to
# retry per tick. Small: this is a repair path, not a scanning strategy —
# if it is routinely saturating, the session scans are failing and that is
# the thing to fix.
GAP_FILLER_MAX_TICKERS = 5
# The old ids. `premarket_scan_*` was one job per market firing before the
# regular open; there are now up to four per market, one per session.
PREMARKET_JOB_PREFIX = "session_scan_"
SESSION_JOB_PREFIX = "session_scan_"
PREMARKET_MARKETS = ("US", "HK", "AU")
SESSION_MARKETS = PREMARKET_MARKETS


def _session_job_id(market: str, window) -> str:
    """`session_scan_US_open`. Stable across restarts; unique per window.

    HK has two OPEN windows (either side of the recess), so the start time
    is folded in — `session_scan_HK_open_1300` — rather than letting the
    afternoon job silently overwrite the morning one under a shared id.
    """
    base = f"{SESSION_JOB_PREFIX}{market}_{window.session.value}"
    duplicates = [
        w for w in market_hours.session_starts(market)
        if w.session is window.session
    ]
    if len(duplicates) > 1:
        return f"{base}_{window.start.strftime('%H%M')}"
    return base


def _lead_time(start, lead_minutes: int) -> tuple[int, int]:
    """Local (hour, minute) `lead_minutes` before a session start `time`.

    The dummy date exists only so timedelta can borrow across the hour (and,
    for the 04:00 pre-market start at a 45-minute lead, across midnight —
    03:15 is fine, but a 300-minute lead would wrap to the previous day and
    the cron would fire at 23:00, which is why leads are bounded in config).
    """
    opening = datetime(2000, 1, 2, start.hour, start.minute)
    fires = opening - timedelta(minutes=lead_minutes)
    return fires.hour, fires.minute


def _premarket_time(market: str, lead_minutes: int) -> tuple[int, int]:
    """Local (hour, minute) `lead_minutes` before `market`'s regular open.

    Retained because it is the pure function the scheduler tests pin, and it
    still answers a real question — "when does the scan before the opening
    bell fire" — even though there are now three or four scans per market.
    """
    return _lead_time(MARKET_HOURS[market][1], lead_minutes)


def uncovered_tickers(now: datetime | None = None) -> list[str]:
    """Enabled tickers with no thesis since their market's session began.

    "Covered" is defined by `market_hours.last_session_start`: if a ticker's
    newest setup predates the instant the current session opened, that
    session's scan did not successfully produce one for it — whether it
    errored, the run was interrupted, or the ticker was added to the
    watchlist afterwards.

    Normally returns nothing. 8.9% of ticker-scans were failing when this was
    written, and under session-only scanning a failure would otherwise leave
    that ticker with no fresh thesis for four to eight hours.
    """
    tickers = db.get_enabled_tickers()
    # One query for the whole watchlist rather than one per ticker — which is
    # what `get_setup_history` exists for, and what its own docstring says.
    newest = db.get_setup_history([t["code"] for t in tickers], per_code=1)

    out: list[str] = []
    for ticker in tickers:
        code, market = ticker["code"], str(ticker.get("market") or "")
        since = market_hours.last_session_start(market, now)
        if since is None:
            continue                      # market shut: nothing to be behind
        latest = (newest.get(code) or [None])[0]
        if latest is None:
            out.append(code)
            continue
        created = db.parse_iso(latest["created_at"])
        if created is None or created < since:
            out.append(code)
    return out


def _run_gap_filler() -> None:
    """Re-scan only what the current session's scan missed.

    This is the old 60-second rotation, repurposed. As a rotation it
    re-analysed all 48 tickers roughly hourly against DAILY bars that do not
    change intraday — measured at 968 theses/day, and 24% of consecutive
    pairs flipped direction on identical bars at identical price, i.e. it was
    mostly resampling the model's own uncertainty. As a gap-filler it
    normally scans zero tickers and costs nothing.

    Note `sync_first=False`: the rotation used to run a full rate-limited
    Moomoo watchlist sync on EVERY 60-second tick, ~60s of paced group reads
    inside the lock hold. The session scans still sync, so the watchlist is
    still picked up several times a day.
    """
    global _last_result
    if not _scan_lock.acquire(blocking=False):
        logger.info("gap-filler: a scan is already running, skipping this tick")
        return
    try:
        codes = uncovered_tickers()
        if not codes:
            _last_result = {"skipped": "every enabled ticker is covered "
                                       "for the current session"}
            return
        batch = codes[:GAP_FILLER_MAX_TICKERS]
        logger.info("gap-filler: %d ticker(s) uncovered, scanning %d: %s",
                    len(codes), len(batch), ", ".join(batch))
        result = scanner.run_cycle(
            get_gateway(),
            codes=batch,
            sync_first=False,
        )
        _last_result = {"gap_filled": len(batch), "uncovered": len(codes),
                        **result.to_dict()}
        logger.info(
            "gap-filler cycle %d: %d scanned, %d ok, %d failed in %.0fs",
            result.run_id, result.scanned, result.succeeded,
            result.failed, result.elapsed,
        )
    except Exception as exc:
        _last_result = {"error": str(exc)}
        logger.error("gap-filler cycle failed: %s", exc, exc_info=True)
    finally:
        _scan_lock.release()


# Back-compat alias: the manual scan router and the tests both reach for the
# rotation entry point by this name.
_run_scan_cycle = _run_gap_filler


def _run_session_scan(market: str, session: str = "open") -> None:
    """Full-watchlist scan for one market, shortly before a session opens.

    One of these per (market, scannable session): four for the US, three for
    HK, two for AU. The body is unchanged from the single pre-market job it
    replaces — the empty-market short-circuit BEFORE the lock, the
    non-blocking acquire that skips rather than queues, `max_tickers=None`
    for the whole enabled watchlist, and `sync_first=True` so watchlist
    edits are picked up. Only the trigger multiplied.
    """
    # Nothing enabled for this market means the run would sync the watchlist
    # (a rate-limited ~60s of Moomoo calls) and then scan zero tickers, while
    # holding the scan lock. Currently true of HK and AU every weekday.
    enabled = db.get_enabled_tickers(market)
    if not enabled:
        logger.info(
            "session scan %s/%s: no enabled tickers, skipping this market entirely",
            market, session,
        )
        _last_premarket[market] = {"session": session,
                                   "skipped": "no enabled tickers for this market"}
        return

    if not _scan_lock.acquire(blocking=False):
        logger.warning(
            "session scan %s/%s: another scan is running, skipping (it would "
            "only run later against staler data)", market, session,
        )
        _last_premarket[market] = {"session": session,
                                   "skipped": "another scan was running"}
        return
    try:
        logger.info("session scan %s/%s: full-watchlist scan starting",
                    market, session)
        result = scanner.run_cycle(
            get_gateway(),
            max_tickers=None,      # the whole enabled watchlist for this market
            sync_first=True,       # pick up overnight watchlist edits
            market=market,
        )
        _last_premarket[market] = {"session": session, **result.to_dict()}
        logger.info(
            "session scan %s/%s: %d scanned, %d ok, %d failed in %.0fs",
            market, session, result.scanned, result.succeeded,
            result.failed, result.elapsed,
        )
    except Exception as exc:
        _last_premarket[market] = {"session": session, "error": str(exc)}
        logger.error("session scan %s/%s failed: %s", market, session, exc,
                     exc_info=True)
    finally:
        _scan_lock.release()


def start_scheduler(interval_seconds: int | None = None) -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    # Seconds, so an explicit override stays fine-grained. Defaults to the
    # gap-filler cadence now that there is no 60-second rotation; the tests
    # pass a large value here to keep the job from firing mid-suite.
    interval = interval_seconds or settings.gap_filler_minutes * 60
    lead = settings.premarket_lead_minutes
    _scheduler = BackgroundScheduler(timezone="UTC")

    _scheduler.add_job(
        _run_gap_filler,
        trigger="interval",
        seconds=interval,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=interval,
    )

    for market in SESSION_MARKETS:
        for window in market_hours.session_starts(market):
            hour, minute = _lead_time(window.start, lead)
            _scheduler.add_job(
                _run_session_scan,
                trigger=CronTrigger(
                    # Per-window, because the US overnight session opens
                    # Sunday through Thursday while everything else is a
                    # weekday session.
                    day_of_week=window.days,
                    hour=hour,
                    minute=minute,
                    timezone=MARKET_TZ[market],
                ),
                args=[market, window.session.value],
                id=_session_job_id(market, window),
                max_instances=1,
                coalesce=True,
                # A full scan is long and its value decays; if the service was
                # down over the open, don't start one an hour late.
                misfire_grace_time=1800,
            )
            logger.info(
                "session scan scheduled: %s %s at %02d:%02d %s [%s] "
                "(%d min before the session opens)",
                market, window.session.value, hour, minute,
                MARKET_TZ[market].key, window.days, lead,
            )

    _scheduler.add_job(
        _run_news_refresh,
        trigger="interval",
        minutes=settings.news_refresh_minutes,
        id=NEWS_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.news_refresh_minutes * 60,
        next_run_time=datetime.now(timezone.utc)
        + timedelta(seconds=NEWS_FIRST_RUN_SECONDS),
    )

    # Starts ACTIVE. Same reasoning as the news job: it does not touch the
    # quote gateway or the GPU, so decisions #16's paused-on-boot default does
    # not apply — and a notifier that has to be switched on by hand after every
    # restart is a notifier nobody can rely on, which is worse than none.
    #
    # coalesce=True so a suspended laptop does not deliver a burst of stale
    # notifications on wake; one catch-up cycle re-derives current truth anyway.
    _scheduler.add_job(
        _run_push_cycle,
        trigger="interval",
        minutes=settings.push_check_minutes,
        id=PUSH_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=int(settings.push_check_minutes * 60),
        next_run_time=datetime.now(timezone.utc)
        + timedelta(seconds=PUSH_FIRST_RUN_SECONDS),
    )

    # Starts ACTIVE, like the news refresh and the pre-market jobs. Decisions
    # #16's paused-on-boot default protects an unattended cold start from
    # seizing OpenD and the GPU; two calendar calls of at most 45s each are
    # not that, and the whole value of a calendar is having already fetched it
    # before anyone looks.
    _scheduler.add_job(
        _run_earnings_refresh,
        trigger="interval",
        hours=EARNINGS_REFRESH_HOURS,
        id=EARNINGS_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=EARNINGS_REFRESH_HOURS * 3600,
        next_run_time=datetime.now(timezone.utc)
        + timedelta(seconds=EARNINGS_FIRST_RUN_SECONDS),
    )

    # Daily at 18:00 US Eastern — after the close, so the day's own bar is
    # final and a 1-day horizon opened yesterday can be scored today. In the
    # market's timezone for the same reason the pre-market jobs are: a UTC
    # schedule drifts an hour twice a year relative to the session it is
    # meant to follow.
    _scheduler.add_job(
        _run_thesis_scoring,
        trigger=CronTrigger(hour=18, minute=30, timezone=MARKET_TZ["US"]),
        id=SCORECARD_JOB_ID,
        max_instances=1,
        coalesce=True,
        # A whole day of grace: this is bookkeeping over stored rows, and
        # running it late is entirely as good as running it on time.
        misfire_grace_time=86400,
    )

    # Daily at 21:00 US Eastern. Chosen against the live schedule rather than
    # by taste: the post-market session scan fires 15:15 and runs 21-64
    # minutes, the scorecard runs 18:30, and the overnight session scan fires
    # 19:15 and runs to roughly 20:20. 21:00 is the first clean slot, and
    # nothing else fires until the pre-market lead at 03:15.
    #
    # Correctness does not depend on that, though: `as_of_date` comes from the
    # newest BAR rather than the clock (decisions #32), and the scores are
    # keyed UNIQUE(plate, date, window), so a run at an unexpected hour
    # relabels nothing and a re-run overwrites in place.
    _scheduler.add_job(
        _run_sector_flow,
        trigger=CronTrigger(hour=21, minute=0, timezone=MARKET_TZ["US"]),
        id=SECTOR_JOB_ID,
        max_instances=1,
        coalesce=True,
        # A whole day of grace, like the scorecard: the kline call backfills,
        # so running this late is as good as running it on time.
        misfire_grace_time=86400,
    )

    # 45 minutes after the sector refresh, so the scores it narrates are the
    # ones written tonight. Three to six generations at 30-120s each.
    _scheduler.add_job(
        _run_sector_narratives,
        trigger=CronTrigger(hour=21, minute=45, timezone=MARKET_TZ["US"]),
        id=SECTOR_NARRATIVE_JOB_ID,
        max_instances=1,
        coalesce=True,
        # Six hours rather than a day: a narrative is about "the move on this
        # date", and running it late is fine, but running tonight's job
        # tomorrow afternoon would write a story about a stale board.
        misfire_grace_time=6 * 3600,
    )

    # Weekly, in the US market's own timezone like the pre-market jobs, so it
    # lands before the week opens rather than drifting with UTC.
    _scheduler.add_job(
        _run_earnings_outlooks,
        trigger=CronTrigger(day_of_week="sun", hour=18, minute=0,
                            timezone=MARKET_TZ["US"]),
        id=EARNINGS_OUTLOOK_JOB_ID,
        max_instances=1,
        coalesce=True,
        # Up to eight generations at 60-120s each. If the box was down over
        # Sunday evening, running it late on Monday is still useful — the
        # reports it briefs have not happened yet.
        misfire_grace_time=6 * 3600,
    )

    _scheduler.start()

    # The rotation's paused/running state survives a restart.
    #
    # Decisions #16 pauses the rotation on a fresh boot so it can't seize
    # OpenD and the GPU before anyone has looked at the dashboard. That
    # reasoning is about an unattended cold start *with no expressed intent*,
    # and it still holds — that is the `default` branch below.
    #
    # What it did not anticipate is a supervised service restarting by itself
    # at 04:00. There the user HAS expressed intent (they pressed Resume), and
    # silently discarding it gives you a backend that restarts cleanly,
    # reports healthy, and then quietly never scans again — which is worse
    # than one that is visibly dead, because nothing ever escalates.
    #
    # What changed with session scans: automatic scanning now starts ACTIVE
    # unless the user explicitly paused it. #16's default protected against a
    # 60-second rotation seizing OpenD and the GPU on an unattended boot; four
    # bounded full scans a day is the same shape as the pre-market job, which
    # #20 already starts active on exactly this reasoning. Only an explicit
    # "0" pauses now.
    desired = db.get_app_state(ROTATION_STATE_KEY)
    global _rotation_state_source
    _rotation_state_source = "persisted" if desired is not None else "default"
    sessions = len([j for j in _scheduler.get_jobs()
                    if j.id.startswith(SESSION_JOB_PREFIX)])

    if desired == "0":
        for job_id in _scan_job_ids():
            _scheduler.pause_job(job_id)
        logger.info(
            "scheduler started: scanning PAUSED from persisted state "
            "(gap-filler + %d session scans held)", sessions,
        )
    else:
        # Delay the gap-filler's first tick so a crash loop cannot fire a
        # scan on every restart. The session scans are cron-driven and can
        # only fire at their own boundaries, so they need no such guard.
        resume_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(interval, RESUME_GRACE_SECONDS)
        )
        _scheduler.modify_job(JOB_ID, next_run_time=resume_at)
        logger.info(
            "scheduler started: %d session scans active, gap-filler first "
            "tick at %s (%ss interval, %s)",
            sessions, resume_at.isoformat(timespec="seconds"), interval,
            _rotation_state_source,
        )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
    _scheduler = None


def _next_run(job) -> str | None:
    """A job's next fire time as ISO, or None when it has none or is absent.

    A paused APScheduler job keeps its row but drops `next_run_time`, so the
    two cases collapse here on purpose: both mean "nothing is scheduled".
    """
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def _simple_job_view(job_id: str, last_result: Any,
                     lock: threading.Lock | None = None) -> dict[str, Any]:
    """The status block for a job that is simply on or off.

    Every job needs one of these. A job with NO view is invisible in
    `/scan/status`, `/readyz` and `/health/detail` — the nightly scorecard sat
    in exactly that state until decisions #69 noticed, so the news, push,
    scorecard, sector and narrative jobs all report through here.

    `lock` adds `in_progress` for the jobs that own one, in the key position
    the response has always had it.
    """
    job = _scheduler.get_job(job_id) if _scheduler else None
    view: dict[str, Any] = {"running": job is not None}
    if lock is not None:
        view["in_progress"] = lock.locked()
    view["next_run"] = _next_run(job)
    view["last_result"] = last_result
    return view


def _scorecard_view() -> dict[str, Any]:
    """The nightly thesis-scoring job."""
    return _simple_job_view(SCORECARD_JOB_ID, _last_scoring)


def _sector_view() -> dict[str, Any]:
    """The nightly sector-rotation job."""
    return _simple_job_view(SECTOR_JOB_ID, _last_sector)


def _sector_narrative_view() -> dict[str, Any]:
    """The nightly sector-narrative job."""
    return _simple_job_view(SECTOR_NARRATIVE_JOB_ID, _last_sector_narrative)


def _job_view(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "next_run": _next_run(job),
        "paused": job.next_run_time is None,
    }


def _news_view() -> dict[str, Any]:
    return _simple_job_view(NEWS_JOB_ID, _last_news, _news_lock)


def scheduler_status() -> dict[str, Any]:
    if _scheduler is None or not _scheduler.running:
        return {"running": False, "paused": True, "next_run": None,
                "last_result": _last_result, "premarket": [],
                "last_premarket": _last_premarket,
                "rotation_state_source": _rotation_state_source,
                "news": {"running": False, "in_progress": False,
                         "next_run": None, "last_result": _last_news}}
    rotation = _scheduler.get_job(JOB_ID)
    premarket = [
        _job_view(j) for j in _scheduler.get_jobs()
        if j.id.startswith(SESSION_JOB_PREFIX)
    ]
    # The soonest upcoming session scan. This, not the gap-filler's next
    # tick, is what "when will my theses refresh" actually means now.
    upcoming = sorted(j["next_run"] for j in premarket if j["next_run"])
    return {
        "running": True,
        # The live trigger, not settings — an explicit interval override used
        # to be invisible here, so a job running hourly reported 60.
        "interval_seconds": (
            int(rotation.trigger.interval.total_seconds())
            if rotation is not None and hasattr(rotation.trigger, "interval")
            else settings.gap_filler_minutes * 60
        ),
        "next_session_scan": upcoming[0] if upcoming else None,
        "session_scans": premarket,
        "next_run": _next_run(rotation),
        "paused": bool(rotation and rotation.next_run_time is None),
        # "persisted" = the user chose this and it survived a restart;
        # "default" = never chosen, so decisions #16's paused default applies.
        "rotation_state_source": _rotation_state_source,
        "scan_in_progress": _scan_lock.locked(),
        "premarket_lead_minutes": settings.premarket_lead_minutes,
        "premarket": premarket,
        "last_result": _last_result,
        "last_premarket": _last_premarket,
        "news": _news_view(),
        "earnings": _earnings_view(),
        "push": _push_view(),
        "scorecard": _scorecard_view(),
        "sector": _sector_view(),
        "sector_narrative": _sector_narrative_view(),
    }


def _guarded_job(
    run, *, fail_label: str,
    lock: threading.Lock | None = None, skip_message: str | None = None,
):
    """Run one scheduled job body, returning its result dict.

    Every periodic job in this module shares the same three requirements, and
    each had its own copy: take a lock without blocking and SKIP if it is
    held, never let an exception escape into APScheduler, and always release.

    What is NOT shared, and stays with each job, is WHICH lock it takes and
    why — that is the judgement (`_scan_lock` is strictly OpenD's quote-context
    mutex per decisions #44/#51/#71, and three jobs deliberately take nothing
    at all). Each caller passes its own lock and its own log wording.

    Returns None when the lock was held, so a skipped tick leaves the caller's
    `_last_*` result standing rather than overwriting it with a skip.
    """
    if lock is not None and not lock.acquire(blocking=False):
        logger.info("%s", skip_message)
        return None
    try:
        return run()
    except Exception as exc:
        logger.error("%s failed: %s", fail_label, exc, exc_info=True)
        return {"error": str(exc)}
    finally:
        if lock is not None:
            lock.release()


def _run_news_refresh() -> None:
    """Refresh the news corpus. Swallows errors — a bad fetch must not kill
    the job, and a dead feed is recorded in news_feed_health regardless."""
    global _last_news
    result = _guarded_job(
        news_service.refresh, fail_label="news refresh", lock=_news_lock,
        skip_message="news: a refresh is already running, skipping this tick")
    if result is not None:
        _last_news = result


def _push_view() -> dict[str, Any]:
    return _simple_job_view(PUSH_JOB_ID, _last_push, _push_lock)


def _run_push_cycle() -> None:
    """Push any newly-justified alerts. Swallows errors — a failed cycle must
    not kill the job, and the fingerprint is only recorded on a successful
    send, so anything missed here is retried on the next tick."""
    global _last_push

    def run():
        from app.services.moomoo_trade_gateway import get_trade_gateway
        return push_service.run_push_cycle(get_trade_gateway())

    result = _guarded_job(
        run, fail_label="push cycle", lock=_push_lock,
        skip_message="push: a cycle is already running, skipping this tick")
    if result is not None:
        _last_push = result


def _earnings_view() -> dict[str, Any]:
    job = _scheduler.get_job(EARNINGS_JOB_ID) if _scheduler else None
    outlook_job = _scheduler.get_job(EARNINGS_OUTLOOK_JOB_ID) if _scheduler else None
    return {
        "running": bool(job and job.next_run_time),
        "next_run": _next_run(job),
        "last_result": _last_earnings,
        "outlooks_next_run": _next_run(outlook_job),
        "last_outlooks": _last_outlooks,
    }


def _run_earnings_refresh() -> None:
    """Refresh the earnings calendar. Swallows errors — a failed fetch must
    not kill the job, and the stored rows stay usable meanwhile.

    Takes `_scan_lock` non-blocking and SKIPS if held. It touches OpenD, and
    #44 defines that lock as strictly OpenD's mutex; a queued refresh would
    just run later against a calendar that has not changed, so skipping is
    strictly better than waiting.
    """
    global _last_earnings
    result = _guarded_job(
        lambda: earnings_service.refresh(get_gateway()),
        fail_label="earnings refresh", lock=_scan_lock,
        skip_message="earnings: a scan holds the gateway, skipping this tick")
    if result is not None:
        _last_earnings = result


def _run_thesis_scoring() -> None:
    """Score theses whose future has now happened, against the bars it made.

    Takes `_scan_lock` non-blocking and SKIPS if held, for exactly the reason
    the earnings refresh does: it can touch OpenD (only on a cache miss —
    it reads the same `market_data` kline cache the scanner keeps warm), and
    #44 defines that lock as strictly OpenD's mutex. Daily, so a skipped tick
    costs nothing: a thesis whose 5-day horizon resolved today is just as
    scoreable tomorrow, and `_unscored_setups` picks up whatever was missed.
    """
    global _last_scoring
    result = _guarded_job(
        lambda: thesis_scorecard.run_scoring(get_gateway()),
        fail_label="thesis scoring", lock=_scan_lock,
        skip_message="scorecard: a scan holds the gateway, skipping this tick")
    if result is not None:
        _last_scoring = result


def _run_sector_flow() -> None:
    """Refresh plate bars and breadth, then score sector rotation.

    Takes `_scan_lock` non-blocking and SKIPS if held, the same choice
    `_run_thesis_scoring` and `_run_earnings_refresh` make: this makes ~300
    bounded quote calls and #44 defines that lock as strictly OpenD's mutex.

    The case for skipping rather than waiting is STRONGER here than in either
    of those, and worth stating because it is the reason no queueing is
    needed: the kline call BACKFILLS. A skipped day is silently repaired by
    tomorrow's run at no cost, because tomorrow's 180-day window still
    contains today's bar. The scorecard leaves rows unscored when it skips;
    this leaves nothing behind at all.

    Makes zero GPU calls, so it does not take an `llm_slots` slot and is not
    governed by the Pause button — see `_scan_job_ids`.
    """
    global _last_sector
    result = _guarded_job(
        lambda: sector_flow.ingest(get_gateway()),
        fail_label="sector flow", lock=_scan_lock,
        skip_message="sector flow: a scan holds the gateway, skipping this tick")
    if result is not None:
        _last_sector = result


def _run_sector_narratives() -> None:
    """Ask the model what the news says about the day's biggest sector moves.

    Does NOT take `_scan_lock`, and the reason is decisions #51's outlook
    argument verbatim: this makes ZERO OpenD calls — it reads stored plate
    scores and stored news — so it has no claim on the quote context's mutex,
    and holding it through several minutes of inference would delay a
    pre-market scan for nothing.

    GPU contention is handled inside `refresh_narratives`, which takes one
    `llm_slots` slot per narrative rather than one for the batch, so an
    interactive chat can always get the other of the two (decisions #50).

    Runs 45 minutes after the sector flow job so it reads a fresh score. If
    that job skipped, `select_sectors` finds nothing for today and this does
    nothing — narrating yesterday's numbers as today's is the failure worth
    avoiding, and it is avoided by the date key rather than by a check.
    """
    global _last_sector_narrative
    _last_sector_narrative = _guarded_job(
        sector_narrative.refresh_narratives, fail_label="sector narratives")


def _run_earnings_outlooks() -> None:
    """Write AI briefings for upcoming reports that need one.

    Does NOT take `_scan_lock`: one OpenD call per ticker, already serialised
    by the gateway's own bounded lock, and holding the scan mutex for sixteen
    minutes of inference would delay a pre-market scan for nothing. GPU
    contention is handled by `llm_slots` inside `refresh_outlooks`.
    """
    global _last_outlooks
    _last_outlooks = _guarded_job(
        lambda: earnings_service.refresh_outlooks(get_gateway()),
        fail_label="earnings outlooks")


def acquire_news_lock() -> bool:
    return _news_lock.acquire(blocking=False)


def release_news_lock() -> None:
    try:
        _news_lock.release()
    except RuntimeError:
        logger.warning("release_news_lock called while the lock was not held")


def acquire_scan_lock() -> bool:
    """Claim the single scan slot. False if a scan is already running.

    Exposed so the manual `POST /scan/run` path takes the same lock the
    scheduled jobs take — only one scan may touch OpenD at a time, whichever
    entry point it came in through.
    """
    return _scan_lock.acquire(blocking=False)


def release_scan_lock() -> None:
    try:
        _scan_lock.release()
    except RuntimeError:
        # Already released — releasing twice is a bug, not a crash.
        logger.warning("release_scan_lock called while the lock was not held")


def _scan_job_ids() -> list[str]:
    """Every job the Pause button governs: the gap-filler and every session scan.

    Deliberately NOT the sector job, for the same reason the news and
    earnings refreshes are absent: Pause means "stop generating theses", and
    the sector job spends ~60s of bounded quote calls and zero GPU. #68
    already had to widen this list once, from the rotation to the session
    scans, because a Pause that left four full scans a day running would be
    a lie. Widening it again to cover a bookkeeping job would make the button
    mean a third thing.
    """
    if not (_scheduler and _scheduler.running):
        return []
    return [JOB_ID] + [
        j.id for j in _scheduler.get_jobs() if j.id.startswith(SESSION_JOB_PREFIX)
    ]


def pause() -> None:
    """Stop all automatic scanning, and remember that the user asked for it.

    **This now covers the session scans, not just the gap-filler**, which is
    a deliberate change from when this only named the rotation. It refines
    decisions #16/#27 rather than reversing them: #16 paused a 60-second
    rotation on a cold boot so it could not seize OpenD and the GPU with no
    expressed intent, and four bounded scans a day is not that. But once
    session scans are the primary path, a button labelled "Pause" that left
    four full-watchlist scans a day running would be a lie — and the UI
    component next to it exists precisely because scanning state being
    invisible was a real outage nobody noticed.
    """
    global _rotation_state_source
    for job_id in _scan_job_ids():
        _scheduler.pause_job(job_id)
    db.set_app_state(ROTATION_STATE_KEY, "0")
    _rotation_state_source = "persisted"


def resume() -> None:
    """Resume all automatic scanning, and remember it across restarts."""
    global _rotation_state_source
    for job_id in _scan_job_ids():
        _scheduler.resume_job(job_id)
    db.set_app_state(ROTATION_STATE_KEY, "1")
    _rotation_state_source = "persisted"
