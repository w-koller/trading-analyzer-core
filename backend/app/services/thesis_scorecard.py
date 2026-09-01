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

Three things here are easy to get wrong in ways that produce
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

Neutral theses are excluded from hit rate entirely. They make no directional
claim, so scoring them either way would invent a prediction the thesis did
not make.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
) -> list[SetupScore]:
    """Score one stored thesis against the bars that followed it.

    Pure: no database, no gateway. Returns one row per horizon that has
    enough future bars to be answerable — a horizon still in the future
    yields nothing rather than a null row, so "not yet knowable" and
    "knowable and wrong" never share a representation.

    `times` is an optional `parse_bar_times(bars)`, for a caller scoring many
    setups against one frame. Omitted, the parse happens here as before.
    """
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
    for horizon in HORIZONS:
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


def _unscored_setups(limit: int) -> list[dict[str, Any]]:
    """Theses with no score row yet, oldest first.

    Oldest first because those are the ones whose future has actually
    happened; a thesis from ten minutes ago has nothing to score against.
    """
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM trade_setups s
            LEFT JOIN setup_scores sc ON sc.setup_id = s.id
            WHERE sc.id IS NULL
            ORDER BY s.created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def run_scoring(gateway, limit: int = 500) -> dict[str, Any]:
    """Score unscored theses against cached bars. Never raises per ticker."""
    pending = _unscored_setups(limit)
    if not pending:
        return {"considered": 0, "scored": 0, "rows": 0, "skipped": 0}

    by_code: dict[str, list[dict[str, Any]]] = {}
    for s in pending:
        by_code.setdefault(s["code"], []).append(s)

    scored = rows = skipped = 0
    for code, setups in by_code.items():
        try:
            bars = market_data.get_cached_bars(gateway, code)
        except Exception as exc:
            logger.info("scorecard: no bars for %s (%s)", code, exc)
            skipped += len(setups)
            continue
        if bars is None or bars.empty:
            skipped += len(setups)
            continue
        # Parsed once for the ticker, not once per thesis.
        times = parse_bar_times(bars)
        # Written once for the ticker, not once per thesis: get_connection()
        # sets PRAGMA synchronous = FULL, so a per-setup save was up to 500
        # connections and 500 fsyncs a run (decisions #39's argument). Still
        # per-ticker rather than per-run, so the partial progress an early
        # failure leaves behind is unchanged.
        batch: list[SetupScore] = []
        for setup in setups:
            produced = score_setup(setup, bars, times)
            if produced:
                batch.extend(produced)
                scored += 1
        rows += save_scores(batch)
    logger.info("scorecard: %d/%d setups scored, %d rows, %d skipped",
                scored, len(pending), rows, skipped)
    return {"considered": len(pending), "scored": scored, "rows": rows,
            "skipped": skipped}


def _bucket(score: int) -> str:
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

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["horizon_days"], r["trade_direction"], _bucket(r["conviction_score"]))
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
            "sufficient": len(scored) >= MIN_SAMPLES and len(days) >= MIN_DISTINCT_DAYS,
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
    }
