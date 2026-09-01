"""Checks for the thesis scorecard.

Run from backend/:  .venv/bin/python -m tests.test_scorecard

Three of these guard failures that produce plausible-looking numbers rather
than an error, which is the worst thing a measurement can do: the lookahead
window, the same-bar tie-break, and the sample deduplication. Each is
constructed deliberately here rather than hoped for.

Offline: temp database, no gateway, no network.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app import db

_tmp = tempfile.mkdtemp(prefix="scorecard-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import thesis_scorecard as sc               # noqa: E402

from tests.harness import check, check_eq, report              # noqa: E402


def bars(closes, highs=None, lows=None, start="2026-06-01"):
    """A daily OHLC frame with time_key in the gateway's own format."""
    day = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    return pd.DataFrame({
        "time_key": [(day + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
                     for i in range(len(closes))],
        "open": closes,
        "high": highs if highs is not None else [c * 1.01 for c in closes],
        "low": lows if lows is not None else [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


def setup(sid, direction="Bullish", conviction=5, spot=100.0,
          last_bar="2026-06-01 00:00:00", stop=None, target=None,
          code="US.A", created_at="2026-06-01T20:00:00+00:00"):
    return {
        "id": sid, "code": code, "trade_direction": direction,
        "conviction_score": conviction, "created_at": created_at,
        "suggested_stop": stop, "suggested_target": target,
        "indicator_snapshot": json.dumps({
            "spot": spot, "last_bar_time": last_bar,
            "indicators": {"close": spot},
        }),
    }


# --- the lookahead guard ------------------------------------------------
# The single most dangerous bug available here. A thesis written at 20:00 on
# the 1st may have been reasoning about the 1st's bar; scoring "the bars
# after created_at" is only correct by accident when the two coincide. When
# the newest bar the thesis SAW is older than the day it was written — which
# `bar_age_days` exists because it is routine — the naive version silently
# scores against a bar the model had already read.
frame = bars([100.0, 110.0, 120.0, 130.0])          # 06-01 .. 06-04
# This thesis was written on the 3rd but only saw the 06-01 bar.
late = setup(1, spot=100.0, last_bar="2026-06-01 00:00:00",
             created_at="2026-06-03T20:00:00+00:00")
scores = {s.horizon_days: s for s in sc.score_setup(late, frame)}
check("the 1-day score uses the bar AFTER the one the thesis saw",
      scores[1].exit_price == 110.0,
      f"exit {scores[1].exit_price} — 120.0 would mean it skipped to created_at")
check("...and 3 forward bars exist, so the 3-day horizon resolves",
      scores[3].exit_price == 130.0, str(scores.get(3) and scores[3].exit_price))

# A horizon whose future has not happened yet emits NOTHING, rather than a
# null row: "not yet knowable" and "knowable and wrong" must never share a
# representation.
check("horizons beyond the available bars are omitted, not nulled",
      set(scores) == {1, 3}, str(sorted(scores)))

# --- directional hit ----------------------------------------------------
up = bars([100.0, 105.0])
check("a Bullish thesis followed by an up move is a hit",
      sc.score_setup(setup(2, "Bullish", spot=100.0), up)[0].directional_hit == 1)
check("a Bearish thesis followed by an up move is a miss",
      sc.score_setup(setup(3, "Bearish", spot=100.0), up)[0].directional_hit == 0)
down = bars([100.0, 95.0])
check("a Bearish thesis followed by a down move is a hit",
      sc.score_setup(setup(4, "Bearish", spot=100.0), down)[0].directional_hit == 1)
check("a Neutral thesis is never scored for direction",
      sc.score_setup(setup(5, "Neutral", spot=100.0), up)[0].directional_hit is None,
      "Neutral makes no directional claim; scoring it invents one")

# --- the same-bar tie-break --------------------------------------------
# One daily bar whose range spans BOTH the stop and the target. Daily bars
# cannot order two intraday touches, so the conservative answer is the only
# honest one — and a backtest that guesses the kind way flatters itself.
both = bars([100.0, 100.0], highs=[101.0, 120.0], lows=[99.0, 80.0])
s = sc.score_setup(setup(6, "Bullish", spot=100.0, stop=90.0, target=115.0), both)[0]
check_eq("a bar touching stop AND target resolves stop_first",
         s.resolution, "stop_first")

only_target = bars([100.0, 100.0], highs=[101.0, 120.0], lows=[99.0, 95.0])
check_eq("a bar touching only the target resolves target_first",
         sc.score_setup(setup(7, "Bullish", spot=100.0, stop=90.0,
                              target=115.0), only_target)[0].resolution,
         "target_first")

neither = bars([100.0, 100.0], highs=[101.0, 102.0], lows=[99.0, 98.0])
check_eq("a bar touching neither stays unresolved",
         sc.score_setup(setup(8, "Bullish", spot=100.0, stop=90.0,
                              target=115.0), neither)[0].resolution,
         "unresolved")

check("a thesis with no stop/target has no resolution at all",
      sc.score_setup(setup(9, "Bullish", spot=100.0), both)[0].resolution is None,
      "folding 'gave no levels' into 'unresolved' would let theses that "
      "committed to nothing dilute the record of those that did")

# Bearish mirrors the whole thing.
bear = bars([100.0, 100.0], highs=[101.0, 112.0], lows=[99.0, 85.0])
check_eq("the tie-break is conservative for Bearish too",
         sc.score_setup(setup(10, "Bearish", spot=100.0, stop=110.0,
                              target=90.0), bear)[0].resolution,
         "stop_first")

# --- degradation --------------------------------------------------------
check("a setup with no stored spot is skipped, not scored from nothing",
      sc.score_setup(setup(11, spot=None), up) == [])
check("an unparseable indicator_snapshot is skipped",
      sc.score_setup({**setup(12), "indicator_snapshot": "{not json"}, up) == [])
check("a thesis whose last bar is the newest bar has no future to score",
      sc.score_setup(setup(13, last_bar="2026-06-02 00:00:00"), up) == [])

# --- the aggregate, and its two independence guards ---------------------
def seed(sid, code, direction, conviction, created_at, hit, ret):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_cache "
            "(code, name, market, enabled, last_synced_at, updated_at) "
            "VALUES (?, ?, 'US', 1, ?, ?)", (code, code, created_at, created_at))
        conn.execute(
            """INSERT INTO trade_setups
               (id, scanner_run_id, code, market, created_at, data_as_of,
                is_delayed_data, indicator_snapshot, feature_vector,
                trade_direction, conviction_score, reasoning, similar_setup_ids)
               VALUES (?, NULL, ?, 'US', ?, ?, 0, '{}', '[]', ?, ?,
                       'One. Two. Three.', '[]')""",
            (sid, code, created_at, created_at, direction, conviction))
    sc.save_scores([sc.SetupScore(sid, 1, 100.0, 100.0 + ret, ret, hit, None, 1)])


# Thirty theses for ONE ticker on ONE day. This is the real shape: the
# rotation writes 30-45 a day per ticker against DAILY bars that do not move
# intraday, so they are near-copies of a single read.
for i in range(30):
    seed(100 + i, "US.DUP", "Bullish", 5, f"2026-06-05T{i % 24:02d}:00:00+00:00", 1, 1.0)
card = sc.scorecard()
bullish = [b for b in card["buckets"] if b["direction"] == "Bullish"]
check("30 theses for one ticker on one day count as ONE sample",
      bullish and bullish[0]["samples"] == 1,
      f"{bullish[0]['samples'] if bullish else None} — an inflated denominator "
      "manufactures confidence intervals out of nothing")

# Breadth in TIME, not just count. 48 tickers on one day is one market
# observation wearing 48 hats: they share that day's move.
for i in range(30):
    seed(200 + i, f"US.T{i}", "Bullish", 5, "2026-06-06T10:00:00+00:00", 1, 1.0)
card = sc.scorecard()
bullish = [b for b in card["buckets"] if b["direction"] == "Bullish"][0]
check("30 tickers on one day DO count as 30 samples",
      bullish["samples"] == 31, str(bullish["samples"]))
check("...but they span only 2 distinct days",
      bullish["distinct_days"] == 2, str(bullish["distinct_days"]))
check("so the bucket is NOT sufficient despite clearing MIN_SAMPLES",
      bullish["sufficient"] is False,
      "cross-sectional correlation within a day is exactly what this guards")
check("and the scorecard reports itself uncalibrated",
      card["calibrated"] is False)

# Spread the same number of samples across enough days and it qualifies.
for d in range(sc.MIN_DISTINCT_DAYS + 2):
    for t in range(2):
        seed(1000 + d * 10 + t, f"US.W{t}", "Bearish", 8,
             f"2026-07-{d + 1:02d}T10:00:00+00:00", 1, -1.0)
card = sc.scorecard()
bear = [b for b in card["buckets"]
        if b["direction"] == "Bearish" and b["conviction_bucket"] == "7-10"][0]
check("breadth across days plus enough samples IS sufficient",
      bear["sufficient"] is True,
      f"n={bear['samples']} days={bear['distinct_days']}")
check("a bucket of all-correct calls reports a 100% hit rate",
      bear["hit_rate"] == 1.0, str(bear["hit_rate"]))

# Neutral is absent from every denominator.
for i in range(5):
    seed(2000 + i, f"US.N{i}", "Neutral", 5, f"2026-08-{i + 1:02d}T10:00:00+00:00",
         None, 2.0)
card = sc.scorecard()
neutral = [b for b in card["buckets"] if b["direction"] == "Neutral"][0]
check("a Neutral bucket exists and reports a mean return",
      neutral["mean_return_pct"] is not None)
check("...but carries no hit rate at all",
      neutral["hit_rate"] is None,
      "a directionless thesis cannot be right or wrong about direction")

# --- idempotency --------------------------------------------------------
before = sc.scorecard()["total_samples"]
sc.save_scores([sc.SetupScore(100, 1, 100.0, 105.0, 5.0, 1, None, 1)])
check("re-scoring a setup updates in place rather than double-counting",
      sc.scorecard()["total_samples"] == before, "UNIQUE(setup_id, horizon_days)")

report("thesis scorecard")
