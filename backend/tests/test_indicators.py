"""Checks for indicators.py against hand-computed reference values.

Run from backend/:  .venv/bin/python -m tests.test_indicators

Reference values are worked out by hand in the comments rather than taken
from another library, so this also pins the conventions we chose (EMA
adjust=False, Bollinger ddof=0).
"""

import math

import pandas as pd

from app.services.indicators import (
    InsufficientData,
    bollinger,
    compute,
    ema,
    macd,
    sma,
    sma_cross,
)

# The harness default (rel_tol=abs_tol=1e-6) is exactly the tolerance these
# hand-computed reference values were written against.
from tests.harness import check_close as check, report


def frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "time_key": pd.date_range("2026-01-01", periods=len(closes), freq="D").astype(str),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000] * len(closes),
    })


# --- SMA -------------------------------------------------------------
s = pd.Series([1.0, 2.0, 3.0, 4.0])
check("sma period=2 last", sma(s, 2).iloc[-1], 3.5)
check("sma warms up (NaN before period)", bool(pd.isna(sma(s, 2).iloc[0])), True)

# --- EMA: span=3 -> alpha=0.5, adjust=False ---------------------------
# ema0 = 1; ema1 = 1 + 0.5*(2-1) = 1.5; ema2 = 1.5 + 0.5*(3-1.5) = 2.25
e = ema(pd.Series([1.0, 2.0, 3.0]), 3)
check("ema seeds on first value", e.iloc[0], 1.0)
check("ema step 2", e.iloc[1], 1.5)
check("ema step 3", e.iloc[2], 2.25)

# --- Bollinger: period=3 on [1,2,3] ----------------------------------
# mid = 2; population std = sqrt(((1-2)^2+(2-2)^2+(3-2)^2)/3) = sqrt(2/3)
std_pop = math.sqrt(2 / 3)
b = bollinger(frame([1.0, 2.0, 3.0]), period=3).iloc[-1]
check("bb mid", b["bb_mid"], 2.0)
check("bb upper (ddof=0)", b["bb_upper"], 2 + 2 * std_pop)
check("bb lower (ddof=0)", b["bb_lower"], 2 - 2 * std_pop)
check("bb %B at upper-ish", b["bb_percent_b"], (3 - (2 - 2 * std_pop)) / (4 * std_pop))

# Sample std (ddof=1) would be 1.0 -> upper 4.0. Confirm we are NOT that.
check("bb does not use sample std", math.isclose(b["bb_upper"], 4.0), False)

# --- Flat series: zero-width band must not divide by zero -------------
flat = compute(frame([100.0] * 30), sma_fast=5, sma_slow=10, bb_period=20)
check("flat bb_mid", flat.bb_mid, 100.0)
check("flat %B is None not inf", flat.bb_percent_b, None)
check("flat bandwidth 0", flat.bb_bandwidth, 0.0)
check("flat macd 0", flat.macd, 0.0)
check("flat macd_hist 0", flat.macd_hist, 0.0)

# --- Golden / death cross --------------------------------------------
# closes [10,10,10,10,20,20], fast=2 slow=3
# sma2: -,10,10,10,15,20   sma3: -,-,10,10,13.33,16.67
# fast>slow first becomes True at index 4 -> golden there
# closes [30,28,26,24,40,44], fast=2 slow=3
# sma2: -,29,27,25,32,42     sma3: -,-,28,26,30,36
# fast is below slow at bars 2-3, crosses above at bar 4.
gc = sma_cross(frame([30, 28, 26, 24, 40, 44]), fast=2, slow=3)
check("golden cross at bar 4", gc["cross"].iloc[4], "golden")
check("no cross at bar 3", gc["cross"].iloc[3], "none")
check("no repeat cross at bar 5", gc["cross"].iloc[5], "none")

# closes [10,30,32,34,5,5], fast=2 slow=3
# sma2: -,20,31,33,19.5,5    sma3: -,-,24,32,23.67,14.67
# bar 2 is the first bar where both exist -> must NOT report a cross
# (warm-up artefact); fast falls below slow at bar 4 -> death there.
dc = sma_cross(frame([10, 30, 32, 34, 5, 5]), fast=2, slow=3)
check("no spurious cross on first valid bar", dc["cross"].iloc[2], "none")
check("death cross at bar 4", dc["cross"].iloc[4], "death")

# A flat run where the SMAs are exactly equal must not invent a cross.
# (Degenerate in real float data, but it must not produce a false signal.)
eq = sma_cross(frame([20, 20, 20, 20, 20, 20]), fast=2, slow=3)
check("flat/equal run emits no cross", set(eq["cross"]), {"none"})

# Equality mid-run must not reset state: below -> equal -> below stays "none",
# and the earlier golden is not re-emitted.
mid = sma_cross(frame([30, 28, 26, 24, 40, 44, 44, 44]), fast=2, slow=3)
check("only one golden across a flat tail", list(mid["cross"]).count("golden"), 1)
check("flat tail emits no death", list(mid["cross"]).count("death"), 0)

# --- MACD sign on a rising series ------------------------------------
rising = frame([float(i) for i in range(1, 61)])
m = macd(rising).iloc[-1]
check("macd positive on uptrend", m["macd"] > 0, True)
check("macd hist = macd - signal", m["macd_hist"], m["macd"] - m["macd_signal"])

falling = frame([float(i) for i in range(60, 0, -1)])
check("macd negative on downtrend", macd(falling).iloc[-1]["macd"] < 0, True)

# --- compute() snapshot on a full-history uptrend ---------------------
full = compute(rising[:0].append(rising) if False else frame([float(i) for i in range(1, 261)]))
check("snapshot close is last bar", full.close, 260.0)
check("uptrend sma_trend bullish", full.sma_trend, "bullish")
check("uptrend macd_state bullish", full.macd_state, "bullish")
check("full history has no warnings", full.warnings, [])
check("sma_gap_pct positive in uptrend", full.sma_gap_pct > 0, True)

# --- Degradation on short history (must warn, not raise) --------------
short = compute(frame([float(i) for i in range(1, 31)]))
check("short history still returns close", short.close, 30.0)
check("short history leaves sma None", short.sma_fast, None)
check("short history computes macd", short.macd is not None, True)
check("short history computes bollinger", short.bb_mid is not None, True)
check("short history warns about sma", any("sma" in w for w in short.warnings), True)

# --- Error cases ------------------------------------------------------
try:
    compute(pd.DataFrame())
    check("empty frame raises", False, True)
except InsufficientData:
    check("empty frame raises InsufficientData", True, True)

try:
    compute(pd.DataFrame({"close": [1, 2, 3]}))
    check("missing OHLCV columns raises", False, True)
except ValueError:
    check("missing OHLCV columns raises ValueError", True, True)

# --- Unsorted input is sorted by time_key before computing ------------
f = frame([float(i) for i in range(1, 41)])
shuffled = f.sample(frac=1, random_state=1).reset_index(drop=True)
check("unsorted input gives same close as sorted",
      compute(shuffled, sma_fast=5, sma_slow=10).close,
      compute(f, sma_fast=5, sma_slow=10).close)

report("indicators")
