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

# --- bucket_for_conviction (Phase 2 of Portfolio Advisor reaches this
# publicly; pin the rename behaves identically at every BUCKETS boundary) ---
check_eq("bucket_for_conviction: low end of the 1-4 bucket", sc.bucket_for_conviction(1), "1-4")
check_eq("bucket_for_conviction: high end of the 1-4 bucket", sc.bucket_for_conviction(4), "1-4")
check_eq("bucket_for_conviction: low end of the 5-6 bucket", sc.bucket_for_conviction(5), "5-6")
check_eq("bucket_for_conviction: high end of the 5-6 bucket", sc.bucket_for_conviction(6), "5-6")
check_eq("bucket_for_conviction: low end of the 7-10 bucket", sc.bucket_for_conviction(7), "7-10")
check_eq("bucket_for_conviction: high end of the 7-10 bucket", sc.bucket_for_conviction(10), "7-10")
check_eq("bucket_for_conviction: below every bucket falls through",
        sc.bucket_for_conviction(0), "?")

# --- run_scoring: the per-horizon selection -----------------------------
# THE defect this file exists to stop recurring. The old selector anti-joined
# per SETUP (`LEFT JOIN setup_scores ... WHERE sc.id IS NULL`), not per
# (setup, horizon). A thesis written at 21:00 and scored an hour later has
# exactly ONE forward bar, so horizon 1 is the only row it can produce — and
# that row then read as "this setup is done". On the live corpus 2,688 of
# 2,691 scored theses sat frozen at horizon 1, and 5/10/20 were empty against
# bars that could have answered 2,005 and 892 of them.
#
# `get_cached_bars` is replaced rather than driven through a fake gateway, so
# this stays offline AND sidesteps the module-level kline cache's TTL, which
# would otherwise serve run two the bars run one saw.
_frames: dict[str, pd.DataFrame] = {}
sc.market_data.get_cached_bars = lambda gw, code, *a, **k: _frames.get(code)

_TODAY = db.now_iso()[:10]


def seed_unscored(sid, code, direction="Bullish", conviction=5, spot=100.0,
                  last_bar="2026-06-01 00:00:00",
                  created_at="2026-06-01T21:00:00+00:00"):
    """A thesis in the DB with a real snapshot and NO score rows."""
    snap = json.dumps({"spot": spot, "last_bar_time": last_bar,
                       "indicators": {"close": spot}})
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
               VALUES (?, NULL, ?, 'US', ?, ?, 0, ?, '[]', ?, ?,
                       'One. Two. Three.', '[]')""",
            (sid, code, created_at, created_at, snap, direction, conviction))


def stored(sid):
    with db.get_connection() as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT horizon_days, scored_at FROM setup_scores WHERE setup_id = ?",
            (sid,))}


def backdate(sid, when="2026-06-02T22:00:00+00:00"):
    """Pretend this setup was last scored on an earlier day.

    The selector skips anything already visited today, so without this a
    second run inside the same day is a no-op — which is the intended
    behaviour, and the reason the test has to move the clock rather than
    just call run_scoring twice.
    """
    with db.get_connection() as conn:
        conn.execute("UPDATE setup_scores SET scored_at = ? WHERE setup_id = ?",
                     (when, sid))


# Night one: the thesis saw the 06-01 bar and exactly one bar has closed since.
seed_unscored(5001, "US.RS")
_frames["US.RS"] = bars([100.0, 101.0])
sc.run_scoring(None)
check_eq("night one, with one forward bar, writes horizon 1 and nothing else",
         sorted(stored(5001)), [1])

# Ten more bars arrive. Under the old selector this setup was already invisible
# and 3/5/10 could never be written, however many bars turned up.
backdate(5001)
# Read AFTER backdating, so the run is the only thing that could move it.
first_scored_at = stored(5001)[1]
_frames["US.RS"] = bars([100.0] + [100.0 + i for i in range(1, 12)])
result = sc.run_scoring(None)
check_eq("later, the SAME setup gains every horizon the bars now answer",
         sorted(stored(5001)), [1, 3, 5, 10])
check("...and horizon 20 still is not written — 11 forward bars cannot answer it",
      20 not in stored(5001),
      "'not yet knowable' and 'knowable and wrong' must never share a spelling")
check("the horizon 1 row is left exactly as it was",
      stored(5001)[1] == first_scored_at,
      "fill-missing, never rewrite: a forward-adjusted feed re-writes history "
      "after a split, and re-scoring would pair a frozen entry against "
      "re-adjusted exits")
check("the run reports what it wrote, per horizon",
      result["by_horizon"][3] == 1 and result["by_horizon"][5] == 1
      and result["by_horizon"][20] == 0,
      str(result["by_horizon"]))

# A setup holding all of HORIZONS drops out of the working set for good.
seed_unscored(5002, "US.RT")
_frames["US.RT"] = bars([100.0] + [100.0 + i for i in range(1, 25)])
sc.run_scoring(None)
check_eq("24 forward bars answer every horizon at once",
         sorted(stored(5002)), list(sc.HORIZONS))
backdate(5002)
page, _ = sc._setups_needing_scores(10_000, _TODAY)
check("a setup with every horizon stored is never selected again",
      all(s["id"] != 5002 for s, _ in page),
      "otherwise the job re-walks the whole corpus forever")

# The page carries the horizons each setup already holds, so run_scoring can
# skip them without a second query per setup.
backdate(5001)
page, _ = sc._setups_needing_scores(10_000, _TODAY)
carried = [done for s, done in page if s["id"] == 5001]
check_eq("each candidate arrives with the horizons it already holds",
         carried, [{1, 3, 5, 10}])

# `remaining` is the signal that would have caught this in a day. Two more
# candidates, because `seed()` stamps scored_at with now_iso() and the
# visited-today guard had left exactly one setup for a limit of 1 to reach.
seed_unscored(5003, "US.RU")
seed_unscored(5004, "US.RV")
page, remaining = sc._setups_needing_scores(1, _TODAY)
check("a truncated page reports the candidates it did not reach",
      len(page) == 1 and remaining > 0,
      f"page={len(page)} remaining={remaining} — non-zero on a scheduled run "
      "means scoring is falling behind")
page, remaining = sc._setups_needing_scores(10_000, _TODAY)
check_eq("a page that reached everything reports nothing remaining", remaining, 0)

# Coverage, so the UI can tell "not knowable yet" from "scoring is behind".
card = sc.scorecard()
check("the response reports how many theses each horizon has scored",
      card["setups_by_horizon"][20] == 1 and card["setups_by_horizon"][1] > 1,
      str(card["setups_by_horizon"]))


# --- a Neutral bucket can be sufficient ---------------------------------
# `sufficient` used to count only rows with a non-null directional_hit, which
# is all of a directional bucket and NONE of a Neutral one — so Neutral was
# permanently insufficient however large it grew, and both UIs printed
# "below 20/20" beside a sample count in the hundreds.
for d in range(sc.MIN_DISTINCT_DAYS + 2):
    for t in range(2):
        seed(3000 + d * 10 + t, f"US.NN{t}", "Neutral", 3,
             f"2026-09-{d + 1:02d}T10:00:00+00:00", None, 2.0)
card = sc.scorecard()
wide = [b for b in card["buckets"]
        if b["direction"] == "Neutral" and b["conviction_bucket"] == "1-4"][0]
check("a Neutral bucket with the breadth to back it reads as sufficient",
      wide["sufficient"] is True,
      f"n={wide['samples']} days={wide['distinct_days']} — the shortfall copy "
      "must not contradict the sample count printed beside it")
check("...while still carrying no hit rate",
      wide["hit_rate"] is None,
      "sufficiency is about the evidence, not about inventing a direction")


# --- guard #4: a bar is not an exit until its session has closed --------
# Learned in production. The cloud provider adapts Twelve Data's exclusive
# `end_date` so that `end = today` INCLUDES today -- correct for a scan
# reading the current price, wrong for a scorecard reading a settled exit.
# A backfill run at 12:05 ET wrote 899 rows whose window closed on that day's
# forming bar: the "close" was the price at the moment of asking, and
# `_resolution` scanned a high/low that was still moving, so a target touched
# at 15:00 read as never touched. Every one of them wrong in the same
# direction, because they shared one half-day of market.
US_CLOSE_UTC = 20                      # 16:00 America/New_York in June (EDT)
day = "2026-06-01"                     # a Monday
frame_today = bars([100.0, 110.0], start=day)   # 06-01, 06-02

mid = datetime(2026, 6, 2, US_CLOSE_UTC - 3, 0, tzinfo=timezone.utc)
check_eq("mid-session, the forming bar is not an exit",
         len(sc._settled_bars(frame_today, "US.A", now=mid)), 1)
after = datetime(2026, 6, 2, US_CLOSE_UTC + 1, 0, tzinfo=timezone.utc)
check_eq("once the session closes, the same bar counts",
         len(sc._settled_bars(frame_today, "US.A", now=after)), 2)
at_close = datetime(2026, 6, 2, US_CLOSE_UTC, 0, tzinfo=timezone.utc)
check_eq("the close itself settles the bar, not a minute later",
         len(sc._settled_bars(frame_today, "US.A", now=at_close)), 2)
check_eq("bars from earlier days are never trimmed",
         len(sc._settled_bars(bars([1.0, 2.0, 3.0], start="2026-05-01"),
                              "US.A", now=mid)), 3)
check("an unmodelled market is left alone rather than guessed at",
      len(sc._settled_bars(frame_today, "ZZ.A", now=mid)) == 2,
      "refusing to invent a close beats inventing one")

# And the wiring: run_scoring must never hand score_setup an unsettled bar.
# Dated far enough ahead that this holds whenever the suite runs, rather than
# depending on whether the market happens to be open right now.
seed_unscored(5010, "US.RW", last_bar="2026-06-01 00:00:00",
              created_at="2026-06-01T21:00:00+00:00")
_frames["US.RW"] = pd.DataFrame({
    "time_key": ["2026-06-01 00:00:00", "2026-06-02 00:00:00", "2099-01-01 00:00:00"],
    "open": [100.0, 110.0, 999.0], "high": [101.0, 111.0, 999.0],
    "low": [99.0, 109.0, 999.0], "close": [100.0, 110.0, 999.0],
    "volume": [1000, 1000, 1000],
})
sc.run_scoring(None)
with db.get_connection() as conn:
    exits = [r[0] for r in conn.execute(
        "SELECT exit_price FROM setup_scores WHERE setup_id = 5010", ())]
check("run_scoring never scores against a session that has not closed",
      exits and 999.0 not in exits,
      f"exits={exits} — 999.0 would mean the unsettled bar became an exit")


report("thesis scorecard")
