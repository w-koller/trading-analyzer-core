"""Checks for similarity.build_feature_vector.

Run from backend/:  .venv/bin/python -m tests.test_similarity

The properties that matter here are the ones cosine similarity depends on:
bounded components, a neutral 0.0 for missing data, opposite setups pointing
in opposite directions, and price-scale invariance.
"""

import math

from app.db import cosine_similarity
from app.services.indicators import IndicatorSnapshot
from app.services.options_walls import OptionWalls
from app.services.similarity import (
    FEATURE_NAMES,
    build_feature_vector,
    describe_vector,
)

from tests.harness import check_close, report


def check(label, got, want):
    """Float check with a tighter ABSOLUTE tolerance than the harness default.

    Several feature components are legitimately exactly 0.0 (a missing
    indicator scales to the centre of the range), and at 1e-6 absolute any
    small non-zero number would compare equal to them — which is precisely
    the mistake these checks exist to catch. The relative tolerance stays at
    the default; only the absolute one is tightened.
    """
    return check_close(label, got, want, abs_tol=1e-9)


def bullish(close=100.0):
    return IndicatorSnapshot(
        close=close, sma_fast=close * 1.05, sma_slow=close,
        sma_trend="bullish", sma_cross="golden", sma_gap_pct=5.0,
        macd=close * 0.01, macd_signal=close * 0.005, macd_hist=close * 0.005,
        macd_state="bullish", macd_cross="bullish",
        bb_upper=close * 1.1, bb_mid=close, bb_lower=close * 0.9,
        bb_percent_b=0.9, bb_bandwidth=0.2, bb_state="upper_half",
    )


def bearish(close=100.0):
    return IndicatorSnapshot(
        close=close, sma_fast=close * 0.95, sma_slow=close,
        sma_trend="bearish", sma_cross="death", sma_gap_pct=-5.0,
        macd=-close * 0.01, macd_signal=-close * 0.005, macd_hist=-close * 0.005,
        macd_state="bearish", macd_cross="bearish",
        bb_upper=close * 1.1, bb_mid=close, bb_lower=close * 0.9,
        bb_percent_b=0.1, bb_bandwidth=0.2, bb_state="lower_half",
    )


# --- shape and bounds -------------------------------------------------
v = build_feature_vector(bullish())
check("vector length matches FEATURE_NAMES", len(v), len(FEATURE_NAMES))
check("all components are floats", all(isinstance(x, float) for x in v), True)
check("all components finite", all(math.isfinite(x) for x in v), True)
check("all components within [-1, 1]", all(-1.0 <= x <= 1.0 for x in v), True)
check("vector is JSON-serialisable", __import__("json").loads(
    __import__("json").dumps(v)) == v, True)

# --- an empty snapshot is the neutral vector, not a crash -------------
empty = build_feature_vector(IndicatorSnapshot(close=50.0))
check("no indicators -> all-neutral vector", set(empty), {0.0})
check("neutral vector still full length", len(empty), len(FEATURE_NAMES))

# --- direction: opposite setups must be dissimilar --------------------
vb, vs = build_feature_vector(bullish()), build_feature_vector(bearish())
check("bullish vs bearish cosine is negative", cosine_similarity(vb, vs) < -0.5, True)
check("identical setups cosine ~1.0", cosine_similarity(vb, vb), 1.0)

# --- price-scale invariance: same shape, different share price --------
cheap = build_feature_vector(bullish(close=5.0))
rich = build_feature_vector(bullish(close=500.0))
check("same setup at $5 and $500 is near-identical",
      cosine_similarity(cheap, rich) > 0.999, True)

# --- missing walls must not shift the indicator components ------------
no_walls = build_feature_vector(bullish())
with_walls = build_feature_vector(bullish(), OptionWalls(expiry="x"))
check("absent walls == empty walls", no_walls, with_walls)

# --- wall geometry is signed the way price sits relative to it --------
w = OptionWalls(expiry="x", spot=100.0, call_wall=110.0, put_wall=90.0,
                call_wall_distance_pct=10.0, put_wall_distance_pct=-10.0,
                put_call_oi_ratio=1.0, put_call_volume_ratio=1.0)
d = describe_vector(build_feature_vector(bullish(), w))
check("call wall above spot is positive", d["call_wall_distance"] > 0, True)
check("put wall below spot is negative", d["put_wall_distance"] < 0, True)
check("parity P/C ratio is neutral", d["put_call_oi_ratio"], 0.0)

# Multiplicative ratios: 2.0 and 0.5 must be equal and opposite skews.
hi = describe_vector(build_feature_vector(
    bullish(), OptionWalls(expiry="x", put_call_oi_ratio=2.0)))["put_call_oi_ratio"]
lo = describe_vector(build_feature_vector(
    bullish(), OptionWalls(expiry="x", put_call_oi_ratio=0.5)))["put_call_oi_ratio"]
check("P/C 2.0 and 0.5 are symmetric", hi, -lo)
check("put-heavy ratio is positive", hi > 0, True)

# --- saturation: an extreme value must not swamp the vector -----------
extreme = IndicatorSnapshot(close=100.0, sma_gap_pct=100000.0, sma_trend="bullish")
ev = describe_vector(build_feature_vector(extreme))
check("extreme gap saturates, not explodes", ev["sma_gap"] <= 1.0, True)
check("extreme gap still bullish-signed", ev["sma_gap"] > 0.9, True)

# --- NaN must never reach the database --------------------------------
nan_snap = IndicatorSnapshot(close=100.0, sma_gap_pct=float("nan"),
                             bb_percent_b=float("nan"), bb_bandwidth=float("inf"))
nv = build_feature_vector(nan_snap)
check("NaN/inf inputs produce finite output", all(math.isfinite(x) for x in nv), True)

# --- dict input (rebuilding from stored JSON) -------------------------
check("dict input matches dataclass input",
      build_feature_vector(bullish().to_dict()), build_feature_vector(bullish()))

# --- ordering is frozen ------------------------------------------------
check("feature names unique", len(set(FEATURE_NAMES)), len(FEATURE_NAMES))
check("describe_vector round-trips", list(describe_vector(v).values()), v)

report("similarity",
       summary=f"similarity: all checks passed ({len(FEATURE_NAMES)} features)")
