"""Feature-vector construction for RAG retrieval (CLAUDE.md rule #3).

`db.get_similar_setups()` ranks historical setups by cosine similarity over
`trade_setups.feature_vector`. This module builds that vector.

Design constraints that fall out of using cosine similarity:

- **Every component is squashed into roughly [-1, 1].** Raw indicator values
  live on wildly different scales (a MACD histogram of 0.4 next to an OI
  ratio of 1200), and cosine over unscaled features is dominated entirely by
  whichever component happens to have the largest units. Percentages are
  divided by a characteristic scale and passed through tanh, which is smooth,
  monotonic, and saturates gracefully instead of clipping a 40% move and a
  400% move to the same number.

- **Signed, centred features.** A feature that is 0 when neutral, positive
  when bullish and negative when bearish means "opposite setups" score as
  dissimilar rather than merely far apart. This is why %B is remapped from
  [0, 1] to [-1, 1] rather than used raw.

- **Missing data is 0.0, not omitted.** The vector is fixed-length and
  fixed-order — a young listing with no 200-day SMA still produces a
  comparable vector, with the unknown components sitting at neutral.

- **Order is frozen.** `FEATURE_NAMES` is the schema. Appending a feature
  changes the vector length; `db.cosine_similarity` returns 0.0 for
  length-mismatched pairs, so old rows degrade to "not similar" instead of
  silently comparing the wrong axes. Bump `FEATURE_VERSION` when the list
  changes so the mix is auditable after the fact.

Data freshness (`is_delayed_data`) is deliberately *not* a feature: it
describes the quote pipeline, not the shape of the setup, and two identical
chart setups shouldn't be considered dissimilar because one is US and one
is ASX. It is stored on the setup row in its own column instead.
"""

from __future__ import annotations

import math
from typing import Any

FEATURE_VERSION = 1

FEATURE_NAMES: tuple[str, ...] = (
    "sma_gap",            # fast vs slow SMA separation
    "sma_trend",          # +1 bullish / -1 bearish
    "sma_cross",          # +1 golden / -1 death (event on this bar)
    "macd_hist",          # histogram, scaled by price
    "macd_state",         # +1 above signal / -1 below
    "macd_cross",         # +1 bullish / -1 bearish (event on this bar)
    "bb_percent_b",       # position within the bands, centred
    "bb_bandwidth",       # volatility regime
    "price_vs_bb_mid",    # extension from the 20-period mean
    "call_wall_distance",  # room to the call wall above
    "put_wall_distance",   # room to the put wall below
    "put_call_oi_ratio",   # standing positioning skew
    "put_call_vol_ratio",  # same-day flow skew
)

# Characteristic scale for each percentage feature, in percent. A move of
# this size maps to tanh(1) ~= 0.76, so typical values use the responsive
# part of the curve and outliers saturate instead of dominating.
_SCALE_SMA_GAP = 10.0
_SCALE_MACD = 2.0
_SCALE_BANDWIDTH = 20.0
_SCALE_BB_EXTENSION = 10.0
_SCALE_WALL_DISTANCE = 10.0


def _squash(value: float | None, scale: float) -> float:
    """tanh-scale a percentage into [-1, 1]; None is neutral."""
    if value is None or not math.isfinite(value):
        return 0.0
    return math.tanh(value / scale)


def _sign(state: str | None, positive: str, negative: str) -> float:
    if state == positive:
        return 1.0
    if state == negative:
        return -1.0
    return 0.0


def _ratio_feature(ratio: float | None) -> float:
    """Map a put/call ratio to [-1, 1], centred on parity.

    Ratios are multiplicative — 2.0 and 0.5 are equal and opposite skews —
    so the log is taken before squashing. Positive means put-heavy.
    """
    if ratio is None or not math.isfinite(ratio) or ratio <= 0:
        return 0.0
    return math.tanh(math.log(ratio))


def build_feature_vector(
    indicators: Any,
    walls: Any = None,
) -> list[float]:
    """Build the fixed-order feature vector for one setup.

    `indicators` is an `IndicatorSnapshot`; `walls` an `OptionWalls` or None
    (not every ticker has a listed options chain). Accepts dataclasses or
    plain dicts, so a vector can be rebuilt from a stored
    `indicator_snapshot` JSON blob without reconstructing the objects.
    """
    ind = _as_dict(indicators)
    wal = _as_dict(walls) if walls is not None else {}

    close = ind.get("close")
    bb_mid = ind.get("bb_mid")
    macd_hist = ind.get("macd_hist")
    percent_b = ind.get("bb_percent_b")
    bandwidth = ind.get("bb_bandwidth")

    # MACD is in price units; divide by price so a $5 stock and a $500 stock
    # with the same relative momentum land in the same place.
    macd_scaled = None
    if macd_hist is not None and close:
        macd_scaled = macd_hist / close * 100

    # Extension from the Bollinger midline, as a percentage of the midline.
    bb_extension = None
    if close is not None and bb_mid:
        bb_extension = (close - bb_mid) / bb_mid * 100

    # %B is 0 at the lower band and 1 at the upper; recentre so mid-band is
    # 0 and clamp beyond the bands rather than letting a blow-off move
    # dominate the vector.
    percent_b_centred = 0.0
    if percent_b is not None and math.isfinite(percent_b):
        percent_b_centred = max(-1.5, min(1.5, (percent_b - 0.5) * 2)) / 1.5

    # Bandwidth is a magnitude, never negative — keep it unsigned so it
    # measures "how volatile", not a direction.
    bandwidth_pct = bandwidth * 100 if bandwidth is not None else None

    vector = [
        _squash(ind.get("sma_gap_pct"), _SCALE_SMA_GAP),
        _sign(ind.get("sma_trend"), "bullish", "bearish"),
        _sign(ind.get("sma_cross"), "golden", "death"),
        _squash(macd_scaled, _SCALE_MACD),
        _sign(ind.get("macd_state"), "bullish", "bearish"),
        _sign(ind.get("macd_cross"), "bullish", "bearish"),
        percent_b_centred,
        _squash(bandwidth_pct, _SCALE_BANDWIDTH),
        _squash(bb_extension, _SCALE_BB_EXTENSION),
        _squash(wal.get("call_wall_distance_pct"), _SCALE_WALL_DISTANCE),
        _squash(wal.get("put_wall_distance_pct"), _SCALE_WALL_DISTANCE),
        _ratio_feature(wal.get("put_call_oi_ratio")),
        _ratio_feature(wal.get("put_call_volume_ratio")),
    ]

    assert len(vector) == len(FEATURE_NAMES), "feature vector/name list out of step"
    # Guard against a NaN reaching the database: one NaN component poisons
    # every cosine comparison against that row, silently and forever.
    return [float(v) if math.isfinite(v) else 0.0 for v in vector]


def describe_vector(vector: list[float]) -> dict[str, float]:
    """Name the components of a stored vector, for the UI and for debugging."""
    return dict(zip(FEATURE_NAMES, vector))


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(getattr(obj, "__dict__", {}))
