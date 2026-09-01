"""Checks for the deterministic opportunity ranking.

Run from backend/:  .venv/bin/python -m tests.test_signals

Following test_alerts.py's convention, every threshold is checked at, just
below and just above: an off-by-one on a boundary is the failure mode, and a
ranking that silently admits a losing trade is the one that costs money.

Offline: temp database, no gateway, no model, no network.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="signals-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.config import settings                                # noqa: E402
from app.services import signals                               # noqa: E402

from tests.harness import check, check_eq, report               # noqa: E402


def iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def bar_time(days_ago=0.0):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def seed(code, direction="Bullish", conviction=6, n=10, close=100.0,
         stop=95.0, target=115.0, entry=None, sma_fast=98.0, bb_mid=97.0,
         macd_hist=0.5, percent_b=0.5, bandwidth=0.12, sma_trend="bullish",
         sma_cross="none", walls=None, bars_stale=False, age_days=0.0,
         bar_age=0.0, market="US"):
    """n theses for one ticker, newest last-written so id order matches time."""
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_cache "
            "(code, name, market, enabled, last_synced_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)", (code, code, market, iso(), iso()))
    for i in range(n):
        snapshot = {
            "spot": close, "last_bar_time": bar_time(bar_age),
            "bars_stale": bars_stale,
            "indicators": {
                "close": close, "sma_fast": sma_fast, "sma_slow": sma_fast * 0.9,
                "sma_trend": sma_trend, "sma_cross": sma_cross,
                "sma_gap_pct": 8.0, "macd_hist": macd_hist,
                "macd_state": "bullish", "macd_cross": "none",
                "bb_mid": bb_mid, "bb_percent_b": percent_b,
                "bb_bandwidth": bandwidth,
            },
            "walls": walls,
        }
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO trade_setups
                   (scanner_run_id, code, market, created_at, data_as_of,
                    is_delayed_data, indicator_snapshot, feature_vector,
                    trade_direction, conviction_score, reasoning,
                    suggested_entry, suggested_stop, suggested_target,
                    similar_setup_ids)
                   VALUES (NULL, ?, ?, ?, ?, 0, ?, '[]', ?, ?,
                           'One. Two. Three.', ?, ?, ?, '[]')""",
                (code, market, iso(age_days + (n - i - 1) * 0.01), iso(age_days),
                 json.dumps(snapshot), direction, conviction, entry, stop, target))


def rank(codes, horizon=signals.SHORT, held=None, movers=None, top_n=5):
    tickers = [t for t in db.get_enabled_tickers() if t["code"] in codes]
    return signals.build_opportunities(tickers, movers, held or set(), horizon, top_n)


# --- nothing in, nothing out -------------------------------------------
check("no tickers yields an empty list, never a placeholder row",
      rank([]) == [], "an always-present empty state becomes furniture")

# --- the risk/reward filter, at the boundary ---------------------------
# entry falls back to spot when no level sits below it, so these are exact.
seed("US.RR1", close=100.0, stop=90.0, target=110.0, sma_fast=101.0, bb_mid=102.0)
check_eq("R:R of exactly 1.0 is admitted (the filter is >=, not >)",
         [r["code"] for r in rank(["US.RR1"])], ["US.RR1"])
check("...and its ratio is reported as 1.0",
      abs(rank(["US.RR1"])[0]["risk_reward"] - 1.0) < 1e-9)

seed("US.RR2", close=100.0, stop=90.0, target=109.0, sma_fast=101.0, bb_mid=102.0)
check("R:R just below 1.0 is rejected",
      rank(["US.RR2"]) == [],
      "a 'top opportunity' whose own stop and target imply a loss is not one")

seed("US.RR3", close=100.0, stop=90.0, target=111.0, sma_fast=101.0, bb_mid=102.0)
check("R:R just above 1.0 is admitted", len(rank(["US.RR3"])) == 1)

# --- levels come from stored numbers, never invented -------------------
seed("US.LVL", close=100.0, stop=90.0, target=130.0, sma_fast=96.0, bb_mid=93.0)
lvl = rank(["US.LVL"])[0]
check_eq("entry is the NEAREST stored support below spot", lvl["entry"], 96.0)
check_eq("stop is the thesis's own suggested_stop", lvl["stop"], 90.0)
check_eq("target is the thesis's own suggested_target", lvl["target"], 130.0)
# Reported rounded to 2dp — that rounding IS the contract, so assert it
# rather than the raw quotient.
check("the ratio is computed from those three and nothing else",
      lvl["risk_reward"] == round((130.0 - 96.0) / (96.0 - 90.0), 2),
      f"{lvl['risk_reward']} vs {round((130.0 - 96.0) / (96.0 - 90.0), 2)}")
check("both legs of the ratio are reported so it is interpretable",
      lvl["stop_distance_pct"] is not None and lvl["target_distance_pct"] is not None,
      "a big ratio can mean a good trade OR a stop too tight to survive noise")

# The thesis's own entry outranks the derivation. The derived level is a
# PULLBACK assumption — right for some setups, wrong for a breakout — so
# whenever the model named an entry, that is the number the ratio uses.
seed("US.ENT", close=100.0, entry=105.0, stop=90.0, target=130.0,
     sma_fast=96.0, bb_mid=93.0)
ent = rank(["US.ENT"])[0]
check_eq("a stored suggested_entry beats the derived support", ent["entry"], 105.0)
check("the ratio is recomputed from the stated entry, not the derived one",
      ent["risk_reward"] == round((130.0 - 105.0) / (105.0 - 90.0), 2),
      f"{ent['risk_reward']}")
check("an entry ABOVE spot is honoured — a breakout is not a pullback",
      ent["entry"] > 100.0,
      "the derivation could only ever return a level below spot here")

# A NULL entry is what every row written before the column existed carries,
# so the fallback is not legacy support — it is the steady state for them.
seed("US.NOENT", close=100.0, entry=None, stop=90.0, target=130.0,
     sma_fast=96.0, bb_mid=93.0)
check_eq("a NULL suggested_entry falls back to the derived support",
         rank(["US.NOENT"])[0]["entry"], 96.0)

# The entry-aware gate still applies to the fallback path, which the
# validator never saw.
seed("US.BADENT", close=100.0, entry=None, stop=97.0, target=130.0,
     sma_fast=96.0, bb_mid=93.0)
check("a derived entry below the thesis's own stop drops the candidate",
      rank(["US.BADENT"]) == [],
      "stop 97 > derived entry 96 — the ratio would be negative")

# A wall between entry and target caps the target at the wall.
seed("US.WALL", close=100.0, stop=90.0, target=130.0, sma_fast=96.0, bb_mid=93.0,
     walls={"has_walls": True, "call_wall": 120.0, "put_wall": 80.0,
            "call_wall_distance_pct": 20.0, "put_call_oi_ratio": 0.6})
check_eq("an option wall between entry and target becomes the target",
         rank(["US.WALL"])[0]["target"], 120.0)

# --- a thesis with no levels cannot be ranked --------------------------
seed("US.NOLVL", close=100.0, stop=None, target=None)
check("a thesis naming no stop/target is excluded",
      rank(["US.NOLVL"]) == [], "there is nothing to compute a ratio from")

# --- Neutral is not an opportunity -------------------------------------
seed("US.NEU", direction="Neutral", close=100.0, stop=90.0, target=130.0,
     sma_fast=96.0)
check("a Neutral thesis is never ranked", rank(["US.NEU"]) == [],
      "no direction means nothing to act on")

# --- staleness, at the boundary ----------------------------------------
stale_days = settings.alerts_setup_stale_days
seed("US.OLD", close=100.0, stop=90.0, target=130.0, sma_fast=96.0,
     age_days=stale_days + 1)
check("a thesis past the staleness budget is excluded",
      rank(["US.OLD"]) == [], f"older than {stale_days}d")
seed("US.NEW", close=100.0, stop=90.0, target=130.0, sma_fast=96.0,
     age_days=stale_days - 1)
check("a thesis just inside the budget is kept", len(rank(["US.NEW"])) == 1)

seed("US.STALEBAR", close=100.0, stop=90.0, target=130.0, sma_fast=96.0,
     bars_stale=True)
check("a thesis built on stale BARS is excluded even when itself fresh",
      rank(["US.STALEBAR"]) == [], "rule #7 — the data, not just the thesis")

# --- held positions ----------------------------------------------------
seed("US.HELD", direction="Bearish", close=100.0, stop=110.0, target=85.0,
     sma_fast=104.0, bb_mid=106.0, sma_trend="bearish")
check("a Bearish read on a HELD position is suppressed",
      rank(["US.HELD"], held={"US.HELD"}) == [],
      "alerts.py's thesis_contradicts_position already reports that fact")
check("...but the same Bearish read is ranked when NOT held",
      len(rank(["US.HELD"])) == 1)

seed("US.HELDBULL", close=100.0, stop=90.0, target=130.0, sma_fast=96.0)
held_bull = rank(["US.HELDBULL"], held={"US.HELDBULL"})
check("a Bullish read on a held position is kept, and flagged",
      len(held_bull) == 1 and held_bull[0]["held"] is True,
      "adding to a winner is a different decision from opening one")

# --- agreement across the stored history -------------------------------
seed("US.AGREE", direction="Bullish", n=10, close=100.0, stop=90.0,
     target=130.0, sma_fast=96.0)
a = rank(["US.AGREE"])[0]
# Bound to the constant, not the literal: the depth changed from 10 to 8
# when scans moved to session boundaries, and a hardcoded number here just
# breaks on the next tuning pass without saying anything useful.
check_eq("agreement counts the stored history, not just the latest thesis",
         (a["agreeing"], a["of_last"]),
         (signals.HISTORY_DEPTH, signals.HISTORY_DEPTH))
check("...and the window is capped at HISTORY_DEPTH, not the whole corpus",
      a["of_last"] == signals.HISTORY_DEPTH and a["of_last"] < 10,
      f"{a['of_last']} of 10 seeded theses")
check("the components are shipped with the row",
      set(a["components"]) == set(signals.WEIGHTS[signals.SHORT]),
      "a ranking whose ranking cannot be inspected is a black box")
check("persistence scores 1.0 when every recent thesis agrees",
      a["components"]["persistence"] == 1.0)

# --- the two horizons read different evidence --------------------------
check_eq("the short model's components are the short weights",
         set(rank(["US.AGREE"], signals.SHORT)[0]["components"]),
         set(signals.WEIGHTS[signals.SHORT]))
check_eq("the medium model's are different, not merely reweighted",
         set(rank(["US.AGREE"], signals.MEDIUM)[0]["components"]),
         set(signals.WEIGHTS[signals.MEDIUM]))
check("extended-hours movement is short-horizon only",
      "extended_move" in signals.WEIGHTS[signals.SHORT]
      and "extended_move" not in signals.WEIGHTS[signals.MEDIUM],
      "a pre-market pop is not evidence about next quarter")
for h in signals.HORIZONS:
    check(f"the {h} weights sum to 1.0",
          abs(sum(signals.WEIGHTS[h].values()) - 1.0) < 1e-9)

# --- ordering and the cap ----------------------------------------------
for i in range(8):
    seed(f"US.R{i}", conviction=3 + (i % 8), close=100.0, stop=90.0,
         target=130.0, sma_fast=96.0)
many = rank([f"US.R{i}" for i in range(8)], top_n=5)
check("the list is capped", len(many) == 5, f"{len(many)} rows")
check("and is ordered by score, descending",
      [r["score"] for r in many] == sorted((r["score"] for r in many), reverse=True))
check("every score lies in [0, 1]",
      all(0.0 <= r["score"] <= 1.0 for r in many),
      str([r["score"] for r in many]))

# --- scores respond to the evidence, in the right direction ------------
seed("US.WEAK", conviction=3, close=100.0, stop=90.0, target=130.0,
     sma_fast=96.0, macd_hist=-2.0, sma_trend="bearish")
seed("US.STRONG", conviction=9, close=100.0, stop=90.0, target=130.0,
     sma_fast=96.0, macd_hist=2.0, sma_trend="bullish")
weak, strong = rank(["US.WEAK"]), rank(["US.STRONG"])
check("a Bullish thesis with bullish momentum outscores one fighting it",
      (strong[0]["score"] if strong else 0) > (weak[0]["score"] if weak else 1),
      f"strong={strong and strong[0]['score']} weak={weak and weak[0]['score']}")

# --- extended hours feeds the short model only -------------------------
movers = {"US.AGREE": {"last_price": 100.0, "pre_change_pct": 3.0,
                       "after_change_pct": None, "overnight_change_pct": None}}
with_ext = rank(["US.AGREE"], signals.SHORT, movers=movers)[0]
without = rank(["US.AGREE"], signals.SHORT)[0]
check("an aligned pre-market move raises the short score",
      with_ext["components"]["extended_move"] > without["components"]["extended_move"],
      f"{with_ext['components']['extended_move']} vs "
      f"{without['components']['extended_move']}")
against = {"US.AGREE": {"last_price": 100.0, "pre_change_pct": -3.0,
                        "after_change_pct": None, "overnight_change_pct": None}}
check("a move AGAINST the thesis lowers it",
      rank(["US.AGREE"], signals.SHORT, movers=against)[0]
      ["components"]["extended_move"] < without["components"]["extended_move"])

# --- freshness is carried through, per rule #7 -------------------------
check("every row carries its own freshness stamps",
      all(k in a for k in ("is_delayed_data", "data_as_of", "thesis_created_at")),
      "a ranking that hides how old its evidence is repeats the bug rule #7 exists for")

report("signals")
