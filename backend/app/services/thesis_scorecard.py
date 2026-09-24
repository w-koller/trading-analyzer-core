"""Did the model's theses actually turn out to be right?

`trade_outcomes` cannot answer that yet and, by decisions #36, may not be
able to for a long time: an outcome there means "how did this thesis turn out
for a real trade someone made", it requires a matching Moomoo deal, and the
corpus is currently n=1. Waiting for it means shipping a ranked
"opportunities" list whose quality nobody can ever check.

But the question is answerable today without a single trade. Every setup
stores the price at thesis time (`indicator_snapshot.spot`) and the daily
klines the scanner already caches contain what happened next. So the model's
DIRECTIONAL record — did a Bullish call precede an up move — is measurable
right now, retroactively, over the whole corpus.

Four things here are easy to get wrong in ways that produce
plausible-looking numbers instead of an error, which is the worst failure
mode a measurement can have. Each is pinned by a test:

1. **Forward bars start after `last_bar_time`, never after `created_at`.**
   A thesis written at 19:00 UTC may have been reasoning about the previous
   trading day's bar — `bar_age_days` is stored precisely because that gap is
   routine. Counting "the next N bars after created_at" would include a bar
   the thesis had already read, leaking known information into the measured
   future and inflating every hit rate.

2. **A bar that touches both stop and target counts as stop-first.** Daily
   bars cannot say which came first intraday. Assuming the favourable order
   is the direction that flatters the result, and a backtest that flatters
   itself is worse than no backtest.

3. **Samples are deduplicated per (code, trading day).** The rotation writes
   30-45 theses per ticker per day against DAILY bars that do not change
   intraday, so they are near-copies of one read, not independent
   observations. Counting them individually inflates the denominator ~30x
   and manufactures confidence intervals out of nothing.

4. **A bar is not an exit until its session has closed.** Asked mid-session
   the provider returns today's FORMING bar, whose "close" is merely the
   price at the moment of asking and whose high/low are still moving. The
   nightly job runs after the close and never saw this; "Score now" is a
   button, and a click at 12:05 ET wrote 899 such rows before the guard
   existed. See `_settled_bars`.

Neutral theses are excluded from hit rate entirely. They make no directional
claim, so scoring them either way would invent a prediction the thesis did
not make.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app import db
from app.services import market_data
from app.utils import market_hours

logger = logging.getLogger(__name__)

# Trading days, not calendar days. Short enough to say something about the
# 1-3 day horizon the dashboard offers, long enough to cover a swing.
HORIZONS = (1, 3, 5, 10, 20)

# Conviction buckets for the aggregate. The corpus is overwhelmingly 4s and
# 5s (measured: 83% of rows), so per-score buckets would be three populated
# rows and six empty ones.
BUCKETS = ((1, 4, "1-4"), (5, 6, "5-6"), (7, 10, "7-10"))

# Below this many deduplicated samples, a hit rate is noise wearing a
# percentage sign. The UI says "not enough data" rather than showing it.
MIN_SAMPLES = 20

# ...and a sample count alone is not enough, which the first real run made
# obvious. Guard #3 dedups per (code, day) and turned 1,501 scored rows into
# 96 "samples" — but those 96 were 48 tickers across just 2 trading days, and
# tickers on the same day share the market's move. Every bucket showed a
# positive mean return, Bearish included, because the market rose on both
# days: that is one market observation wearing 96 hats, and at MIN_SAMPLES
# alone the scorecard reported itself calibrated on it.
#
# So calibration additionally requires breadth in TIME. A month of trading
# days is a floor, not a sufficiency claim — cross-sectional correlation
# within a day is the thing being defended against, and it does not go away,
# it only gets diluted.
MIN_DISTINCT_DAYS = 20


@dataclass
class SetupScore:
    setup_id: int
    horizon_days: int
    entry_price: float
    exit_price: float | None
    forward_return_pct: float | None
    directional_hit: int | None
    resolution: str | None
    bars_used: int


def parse_bar_times(bars: pd.DataFrame) -> pd.Series | None:
    """The `time_key` column parsed once, or None if there is no such column.

    Hoisted out of `_future_bars` because `run_scoring` scores every thesis
    for one ticker against the SAME frame: parsing ~400 timestamps per setup
    when one pass per ticker will do was the bulk of the job's CPU.
    """
    if "time_key" not in bars.columns:
        return None
    return bars["time_key"].map(market_hours.parse_bar_time)


def _future_bars(
    bars: pd.DataFrame, last_bar_time: Any, times: pd.Series | None = None,
) -> pd.DataFrame:
    """Bars strictly after the newest bar the thesis actually saw.

    This is guard #1 in the module docstring. `last_bar_time` is what the
    thesis read; anything at or before it is not the future.

    `times` is `parse_bar_times(bars)` when the caller has already computed
    it for this frame; omitted, it is computed here as before.
    """
    cutoff = market_hours.parse_bar_time(last_bar_time)
    if cutoff is None:
        return bars.iloc[0:0]
    if times is None:
        times = parse_bar_times(bars)
    if times is None:
        return bars.iloc[0:0]
    mask = times.map(lambda t: t is not None and t > cutoff)
    return bars[mask]


def _settled_bars(
    bars: pd.DataFrame, code: str, now: datetime | None = None,
) -> pd.DataFrame:
    """Bars whose own session has actually closed.

    Guard #4, and it was learned the hard way on 2026-09-23. A daily bar is
    not a fact until its session ends: asked mid-session, the cloud provider
    returns today's FORMING bar, and it does so deliberately — it adapts
    Twelve Data's exclusive `end_date` precisely so that `end = today`
    includes today, which is right for a scan reading the current price and
    wrong for a scorecard reading a settled exit.

    Scoring against a forming bar takes the price at the moment you asked and
    records it as the close. `_resolution` is worse: it scans a high/low that
    is still moving, so a stop or target touched after you looked is recorded
    as never touched. Both produce a plausible number rather than an error —
    the failure this module's docstring calls the worst available — and both
    are CORRELATED, because every ticker scored in one mid-session run shares
    the same half-day of market.

    Measured: a manual backfill run at 12:05 ET wrote 5,585 rows of which 899
    closed on that day's forming bar. They were deleted and re-scored after
    the close rather than kept.

    The nightly job never hit this, running an hour after the post-close scan.
    The "Score now" button hits it on any click during market hours, which is
    a supported action on a page whose entire job is not to over-claim.

    Trailing-only, because bars arrive chronologically: the walk stops at the
    first settled bar. Returns the frame untouched for a market this project
    does not model, which is the same choice `next_regular_close` makes —
    refusing to guess beats inventing a close.
    """
    market = market_hours.market_of(code)
    tz = market_hours.MARKET_TZ.get(market)
    hours = market_hours.MARKET_HOURS.get(market)
    if tz is None or hours is None or "time_key" not in bars.columns:
        return bars
    now = now or datetime.now(timezone.utc)

    drop = 0
    for i in range(len(bars) - 1, -1, -1):
        stamp = market_hours.parse_bar_time(bars["time_key"].iloc[i])
        if stamp is None:
            break
        # The time_key names a TRADING DATE and parses to UTC midnight of it;
        # converting that to exchange-local time would move it to the previous
        # evening and ask about the wrong day. Take the date as given and pair
        # it with the market's own regular close (MARKET_HOURS[m][2], the same
        # element cloud's scheduler derives its post-close time from).
        closes_at = datetime.combine(
            stamp.date(), hours[2], tzinfo=tz).astimezone(timezone.utc)
        if closes_at <= now:
            break                         # settled, and so is everything before
        drop += 1
    return bars.iloc[:len(bars) - drop] if drop else bars


def _resolution(
    future: pd.DataFrame, direction: str, stop: float | None, target: float | None,
) -> str | None:
    """Which of the thesis's own levels the price reached first.

    Returns None when the thesis named no levels to test — that is "not
    applicable", and folding it into 'unresolved' would let theses that
    committed to nothing dilute the record of those that did.
    """
    if stop is None or target is None or direction == "Neutral":
        return None
    for bar in future.itertuples(index=False):
        high, low = float(bar.high), float(bar.low)
        if direction == "Bullish":
            hit_stop, hit_target = low <= stop, high >= target
        else:
            hit_stop, hit_target = high >= stop, low <= target
        # Guard #2: both in one bar resolves against the thesis. Daily bars
        # cannot order two intraday touches, and guessing the kind one is
        # how a backtest quietly starts flattering itself.
        if hit_stop:
            return "stop_first"
        if hit_target:
            return "target_first"
    return "unresolved"


def score_setup(
    setup: dict[str, Any], bars: pd.DataFrame, times: pd.Series | None = None,
    skip_horizons: Collection[int] = (),
) -> list[SetupScore]:
    """Score one stored thesis against the bars that followed it.

    Pure: no database, no gateway. Returns one row per horizon that has
    enough future bars to be answerable — a horizon still in the future
    yields nothing rather than a null row, so "not yet knowable" and
    "knowable and wrong" never share a representation.

    `times` is an optional `parse_bar_times(bars)`, for a caller scoring many
    setups against one frame. Omitted, the parse happens here as before.

    `skip_horizons` are horizons this setup already has a stored row for, and
    they are not recomputed. FILL-MISSING, never rewrite: `entry_price` is the
    spot the thesis saw, frozen at thesis time, while `exit_price` would come
    from bars fetched now — and on a forward-adjusted feed (decisions #7) a
    split rewrites history, so recomputing an old horizon would quietly pair
    a stale entry against re-adjusted exits and change a measured result that
    nothing asked to change. `save_scores` stays an upsert, so a deliberate
    re-score is still one call away; it just is not what the nightly job does.
    """
    wanted = [h for h in HORIZONS if h not in skip_horizons]
    if not wanted:
        return []                         # nothing left to measure

    snapshot = setup.get("indicator_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            return []
    if not isinstance(snapshot, dict):
        return []

    entry = snapshot.get("spot") or (snapshot.get("indicators") or {}).get("close")
    if not entry:
        return []
    entry = float(entry)

    future = _future_bars(bars, snapshot.get("last_bar_time"), times)
    if future.empty:
        return []

    direction = setup["trade_direction"]
    stop, target = setup.get("suggested_stop"), setup.get("suggested_target")

    out: list[SetupScore] = []
    for horizon in wanted:
        if len(future) < horizon:
            continue                      # not yet knowable — emit nothing
        window = future.iloc[:horizon]
        exit_price = float(window["close"].iloc[-1])
        ret = (exit_price - entry) / entry * 100

        if direction == "Bullish":
            hit: int | None = int(ret > 0)
        elif direction == "Bearish":
            hit = int(ret < 0)
        else:
            hit = None                    # Neutral claims no direction

        out.append(SetupScore(
            setup_id=int(setup["id"]),
            horizon_days=horizon,
            entry_price=entry,
            exit_price=exit_price,
            forward_return_pct=ret,
            directional_hit=hit,
            resolution=_resolution(window, direction, stop, target),
            bars_used=len(window),
        ))
    return out


def save_scores(scores: list[SetupScore]) -> int:
    """Upsert scored rows. Idempotent on (setup_id, horizon_days)."""
    if not scores:
        return 0
    now = db.now_iso()
    with db.get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO setup_scores
                (setup_id, horizon_days, entry_price, exit_price,
                 forward_return_pct, directional_hit, resolution,
                 bars_used, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(setup_id, horizon_days) DO UPDATE SET
                exit_price = excluded.exit_price,
                forward_return_pct = excluded.forward_return_pct,
                directional_hit = excluded.directional_hit,
                resolution = excluded.resolution,
                bars_used = excluded.bars_used,
                scored_at = excluded.scored_at
            """,
            [(s.setup_id, s.horizon_days, s.entry_price, s.exit_price,
              s.forward_return_pct, s.directional_hit, s.resolution,
              s.bars_used, now) for s in scores],
        )
    return len(scores)


#: HORIZONS as a SQL list literal, derived rather than typed out so the query
#: below can never drift from the tuple it is testing against.
_HORIZON_LIST_SQL = ", ".join(str(h) for h in HORIZONS)

#: Per-setup scoring state: which of HORIZONS it holds, how many, and when it
#: was last touched. Written once here because both the page and its COUNT
#: need exactly the same predicate, and two spellings of one rule is one
#: spelling too many.
_SCORE_STATE_SQL = f"""
    FROM trade_setups s
    LEFT JOIN (
        SELECT setup_id,
               COUNT(DISTINCT CASE WHEN horizon_days IN ({_HORIZON_LIST_SQL})
                                   THEN horizon_days END) AS n_horizons,
               MAX(scored_at)             AS last_scored_at,
               GROUP_CONCAT(horizon_days) AS horizons
        FROM setup_scores
        GROUP BY setup_id
    ) sc ON sc.setup_id = s.id
    WHERE COALESCE(sc.n_horizons, 0) < {len(HORIZONS)}
      AND (sc.last_scored_at IS NULL OR sc.last_scored_at < ?)
"""


def _setups_needing_scores(
    limit: int, today: str,
) -> tuple[list[tuple[dict[str, Any], set[int]]], int]:
    """Theses still missing at least one horizon, oldest first, each paired
    with the horizons it already holds.

    THE BUG THIS REPLACED, because it is worth stating in full. The old query
    anti-joined per SETUP — `LEFT JOIN setup_scores ... WHERE sc.id IS NULL` —
    not per (setup, horizon). A thesis written at 21:00 and scored an hour
    later has exactly ONE forward bar, so `score_setup` correctly emits
    horizon 1 and nothing else; that single row then read as "this setup is
    done" and the setup was never looked at again. Its 3/5/10/20 rows could
    not be written no matter how many bars arrived afterwards.

    Measured on the live corpus the day this was found: 2,688 of 2,691 scored
    theses were frozen at horizon 1, horizon 3 held 3 rows — all written on
    the first run the job ever did — and 5/10/20 held none, against a corpus
    with enough bars for 2,470 / 2,005 / 892 of them. Horizon 5 being empty
    also meant `advisor.calibration_for_setup`, which reads exactly that
    horizon, had returned None for every setup since the day it shipped.

    It survived review because it looks right and because the symptom has an
    innocent explanation that was true when it was written: a young corpus
    genuinely cannot answer a 20-bar question. `save_scores` has always been
    an upsert on (setup_id, horizon_days) and `_run_thesis_scoring`'s
    docstring has always promised the missed rows get picked up later — the
    rest of the module was built for this behaviour and only the selection
    disagreed.

    Oldest first, unchanged: those are the ones whose future has actually
    happened. A setup already scored today is skipped, so hammering
    `POST /signals/scorecard/run` re-walks nothing.

    Deliberately NOT gated on elapsed time. Gating on `created_at` would be
    wrong (forward bars start at `last_bar_time`, which trails it by up to
    four days on a weekend thesis) and gating on `last_bar_time` would put
    `json_extract` — the one JSON1 dependency in this codebase — on the path
    of the whole job. A setup whose next horizon has not arrived yet costs one
    slice and returns nothing, and the real cost of a run is the per-TICKER
    bar fetch, which the caller's grouping already pays only once.

    Returns the page and the number of candidates it did not reach. A nightly
    run leaving anything behind is the signal that would have caught this in
    a day rather than three weeks.
    """
    with db.get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) " + _SCORE_STATE_SQL, (today,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT s.*, COALESCE(sc.horizons, '') AS _scored_horizons "
            + _SCORE_STATE_SQL
            + " ORDER BY s.created_at ASC LIMIT ?",
            (today, limit),
        ).fetchall()

    page: list[tuple[dict[str, Any], set[int]]] = []
    for row in rows:
        setup = dict(row)
        raw = setup.pop("_scored_horizons", "") or ""
        page.append((setup, {int(h) for h in raw.split(",") if h}))
    return page, max(total - len(page), 0)


#: A whole pass in one run, which is the point. The old 500 was set when a
#: setup was visited exactly once ever; now that a setup is revisited until
#: all five horizons are filled, the working set is every thesis younger than
#: the longest horizon — about 20 trading days of rotation output. Truncating
#: that silently is how the horizons stalled in the first place, so the budget
#: is generous and `remaining` says out loud when it was not enough. The two
#: routers keep their own 1..5000 interactive bound; this is the JOB's.
SCHEDULED_LIMIT = 10_000


def run_scoring(gateway, limit: int = SCHEDULED_LIMIT) -> dict[str, Any]:
    """Fill in every horizon that has become answerable. Never raises per ticker.

    Fill-missing, not re-score: a setup comes back on later runs until all of
    HORIZONS is stored, and each run writes only the horizons it does not
    already have. See `_setups_needing_scores` for why the previous
    once-per-setup selection meant 3/5/10/20 could never be written at all.
    """
    today = db.now_iso()[:10]
    pending, remaining = _setups_needing_scores(limit, today)
    by_horizon = {h: 0 for h in HORIZONS}
    if not pending:
        return {"considered": 0, "scored": 0, "rows": 0, "skipped": 0,
                "remaining": remaining, "by_horizon": by_horizon}

    by_code: dict[str, list[tuple[dict[str, Any], set[int]]]] = {}
    for setup, done in pending:
        by_code.setdefault(setup["code"], []).append((setup, done))

    scored = rows = skipped = 0
    for code, entries in by_code.items():
        try:
            bars = market_data.get_cached_bars(gateway, code)
        except Exception as exc:
            logger.info("scorecard: no bars for %s (%s)", code, exc)
            skipped += len(entries)
            continue
        if bars is None or bars.empty:
            skipped += len(entries)
            continue
        # Guard #4, per ticker rather than inside `score_setup` — that one is
        # documented pure, and giving it a wall clock would make every test
        # frame's meaning depend on the day the suite runs.
        bars = _settled_bars(bars, code)
        if bars.empty:
            skipped += len(entries)
            continue
        # Parsed once for the ticker, not once per thesis.
        times = parse_bar_times(bars)
        # Written once for the ticker, not once per thesis: get_connection()
        # sets PRAGMA synchronous = FULL, so a per-setup save was up to 500
        # connections and 500 fsyncs a run (decisions #39's argument). Still
        # per-ticker rather than per-run, so the partial progress an early
        # failure leaves behind is unchanged.
        batch: list[SetupScore] = []
        for setup, done in entries:
            produced = score_setup(setup, bars, times, skip_horizons=done)
            if produced:
                batch.extend(produced)
                scored += 1
        rows += save_scores(batch)
        # Counted after the write, so a failed save is not reported as rows
        # that exist. Per-horizon because a total cannot distinguish "the
        # backfill worked" from "horizon 1 ran again, five times over".
        for s in batch:
            by_horizon[s.horizon_days] = by_horizon.get(s.horizon_days, 0) + 1
    logger.info("scorecard: %d/%d setups scored, %d rows %s, %d skipped, "
                "%d candidate(s) not reached",
                scored, len(pending), rows,
                {h: n for h, n in by_horizon.items() if n}, skipped, remaining)
    return {"considered": len(pending), "scored": scored, "rows": rows,
            "skipped": skipped,
            # Candidates the `limit` did not reach. Non-zero on a scheduled
            # run means scoring is falling behind, which is exactly the state
            # that went unnoticed for three weeks.
            "remaining": remaining,
            "by_horizon": by_horizon}


def bucket_for_conviction(score: int) -> str:
    for low, high, label in BUCKETS:
        if low <= score <= high:
            return label
    return "?"


def scorecard(horizon: int | None = None) -> dict[str, Any]:
    """Aggregate hit rate by direction and conviction bucket.

    Guard #3 lives here: one sample per (code, trading day), taking that
    day's LAST thesis because it saw the most complete bar. Without it the
    denominator counts 30-45 near-identical reads of one ticker-day.
    """
    query = """
        SELECT * FROM (
            SELECT sc.horizon_days, sc.forward_return_pct, sc.directional_hit,
                   sc.resolution, s.code, s.trade_direction, s.conviction_score,
                   substr(s.created_at, 1, 10) AS thesis_day,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.code, substr(s.created_at, 1, 10),
                                    sc.horizon_days
                       ORDER BY s.created_at DESC, s.id DESC
                   ) AS _rn
            FROM setup_scores sc
            JOIN trade_setups s ON s.id = sc.setup_id
        ) WHERE _rn = 1
    """
    params: list[Any] = []
    if horizon is not None:
        query += " AND horizon_days = ?"
        params.append(horizon)

    with db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        # Coverage, deliberately outside both the dedup above and the
        # `horizon` filter: how many distinct THESES carry a row at each
        # horizon. A horizon sitting at zero beside a `distinct_days` in the
        # twenties is scoring falling behind, not a future that has not
        # happened yet, and nothing in this response could previously tell a
        # reader which of the two they were looking at.
        coverage = {h: 0 for h in HORIZONS}
        for h, n in conn.execute(
            "SELECT horizon_days, COUNT(DISTINCT setup_id) FROM setup_scores "
            "GROUP BY horizon_days"
        ).fetchall():
            if h in coverage:
                coverage[h] = n

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["horizon_days"], r["trade_direction"],
               bucket_for_conviction(r["conviction_score"]))
        groups.setdefault(key, []).append(r)

    buckets = []
    for (h, direction, bucket), items in sorted(groups.items(), key=lambda kv: kv[0][:1]):
        # Neutral rows carry directional_hit NULL by construction; they still
        # get a mean return, which is the only honest thing to say about them.
        scored = [i for i in items if i["directional_hit"] is not None]
        returns = [i["forward_return_pct"] for i in items
                   if i["forward_return_pct"] is not None]
        resolutions = [i["resolution"] for i in items if i["resolution"]]
        days = {i["thesis_day"] for i in items}
        buckets.append({
            "horizon_days": h,
            "direction": direction,
            "conviction_bucket": bucket,
            # The count is the DEDUPLICATED one, and it is reported beside
            # every figure so a 3-sample bucket cannot read like a 300.
            "samples": len(items),
            # Reported beside `samples` because the two say different things
            # and only the pair is informative: 96 samples over 2 days is 2
            # market observations, not 96.
            "distinct_days": len(days),
            "hit_rate": (
                round(sum(i["directional_hit"] for i in scored) / len(scored), 4)
                if scored else None
            ),
            "mean_return_pct": round(sum(returns) / len(returns), 3) if returns else None,
            "target_first": resolutions.count("target_first"),
            "stop_first": resolutions.count("stop_first"),
            "unresolved": resolutions.count("unresolved"),
            # Deliberately `items`, not `scored`. `scored` drops rows whose
            # directional_hit is NULL, and since buckets are partitioned BY
            # direction that is all of a Neutral bucket and none of a
            # directional one. Gating on it made every Neutral bucket
            # permanently insufficient however large it grew, so both UIs
            # printed "below 20/20" against a sample count in the hundreds —
            # the shortfall copy contradicting the number beside it. The
            # figure a Neutral bucket publishes is its mean return, and this
            # is the count that figure rests on. Directional buckets are
            # untouched: their two counts are equal by construction.
            "sufficient": len(items) >= MIN_SAMPLES and len(days) >= MIN_DISTINCT_DAYS,
        })

    total = sum(b["samples"] for b in buckets)
    all_days = {r["thesis_day"] for r in rows}
    return {
        "buckets": buckets,
        "total_samples": total,
        "distinct_days": len(all_days),
        "min_samples": MIN_SAMPLES,
        "min_distinct_days": MIN_DISTINCT_DAYS,
        # One flag the UI can branch on rather than re-deriving the rule.
        "calibrated": any(b["sufficient"] for b in buckets),
        "horizons": list(HORIZONS),
        # Theses scored at each horizon, un-deduplicated. Lets the UI say
        # "not knowable yet" only when that is actually true.
        "setups_by_horizon": coverage,
    }
