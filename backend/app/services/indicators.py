"""Deterministic technical indicators over OHLCV bars.

CLAUDE.md rule #1: every number here is computed in Python. The LLM is only
ever shown these outputs — it never calculates one.

Implemented in plain pandas rather than pandas-ta: pandas-ta is not
installable on Python 3.11 (PyPI carries only 0.4.67b0/0.4.71b0, both
requiring >=3.12, and 0.3.14b0 has been pulled from the index), while
Python 3.11 is a hard constraint from the Debian 12 base image. SMA/MACD/
Bollinger are a few lines of pandas each, so the dependency buys little.

Conventions chosen to match what charting platforms (TradingView, Moomoo)
display, so a number here matches what the user sees on their chart:
  - MACD uses EMAs with adjust=False.
  - Bollinger uses population standard deviation (ddof=0), not pandas'
    default sample std (ddof=1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


class InsufficientData(ValueError):
    """Not enough bars to compute the requested indicator."""


@dataclass
class IndicatorSnapshot:
    """Latest-bar state of every indicator, for the prompt and feature vector."""

    close: float
    sma_fast: float | None = None
    sma_slow: float | None = None
    sma_trend: str = "unknown"          # bullish | bearish | unknown
    sma_cross: str = "none"             # golden | death | none
    sma_gap_pct: float | None = None    # (fast - slow) / slow * 100

    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    macd_state: str = "unknown"         # bullish | bearish | unknown
    macd_cross: str = "none"            # bullish | bearish | none

    bb_upper: float | None = None
    bb_mid: float | None = None
    bb_lower: float | None = None
    bb_percent_b: float | None = None   # 0 = lower band, 1 = upper band
    bb_bandwidth: float | None = None   # (upper-lower)/mid, volatility proxy
    bb_state: str = "unknown"           # above | upper_half | lower_half | below

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _validate(df: pd.DataFrame, min_rows: int) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {sorted(missing)}")
    if len(df) < min_rows:
        raise InsufficientData(f"need >= {min_rows} bars, got {len(df)}")
    return df


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (adjust=False, the charting convention)."""
    return series.ewm(span=period, adjust=False).mean()


def sma_cross(
    df: pd.DataFrame, fast: int = 50, slow: int = 200
) -> pd.DataFrame:
    """Fast/slow SMA plus the crossover event on each bar.

    `cross` is 'golden' on the bar where fast crosses up through slow,
    'death' on the bar where it crosses down, else 'none'.
    """
    _validate(df, slow)
    out = pd.DataFrame(index=df.index)
    out["sma_fast"] = sma(df["close"], fast)
    out["sma_slow"] = sma(df["close"], slow)

    # A cross is a sign flip of (fast - slow). Using the sign rather than a
    # single boolean avoids having to decide which side exact equality falls
    # on — with a plain `>` or `>=`, equality silently swallows a cross in
    # one of the two directions. A flat/equal run instead carries the last
    # non-zero sign forward, so it neither emits a cross nor resets state.
    diff = out["sma_fast"] - out["sma_slow"]
    sign = diff.gt(0).astype(float) - diff.lt(0).astype(float)   # +1 / -1 / 0
    sign = sign.where(diff.notna())          # keep warm-up bars undefined
    sign = sign.replace(0.0, pd.NA).ffill()  # equality inherits prior state
    prev = sign.shift(1)

    out["cross"] = "none"
    out.loc[(sign == 1) & (prev == -1), "cross"] = "golden"
    out.loc[(sign == -1) & (prev == 1), "cross"] = "death"
    return out


def macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line, histogram, and signal-line crossovers."""
    _validate(df, slow)
    out = pd.DataFrame(index=df.index)
    out["macd"] = ema(df["close"], fast) - ema(df["close"], slow)
    out["macd_signal"] = ema(out["macd"], signal)
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    above = out["macd"] > out["macd_signal"]
    prev = above.shift(1)
    out["cross"] = "none"
    out.loc[above & (prev == False), "cross"] = "bullish"   # noqa: E712
    out.loc[(~above) & (prev == True), "cross"] = "bearish"  # noqa: E712
    return out


def bollinger(
    df: pd.DataFrame, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """Bollinger bands, %B and bandwidth.

    Uses population std (ddof=0) to match charting platforms.
    """
    _validate(df, period)
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["bb_mid"] = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + num_std * std
    out["bb_lower"] = out["bb_mid"] - num_std * std

    width = out["bb_upper"] - out["bb_lower"]
    # %B is undefined on a zero-width band (a perfectly flat series).
    out["bb_percent_b"] = ((close - out["bb_lower"]) / width).where(width != 0)
    out["bb_bandwidth"] = (width / out["bb_mid"]).where(out["bb_mid"] != 0)
    return out


def compute(
    df: pd.DataFrame,
    sma_fast: int = 50,
    sma_slow: int = 200,
    bb_period: int = 20,
) -> IndicatorSnapshot:
    """Compute every indicator and return the latest-bar snapshot.

    Degrades rather than raising when history is short: an indicator that
    needs more bars than are available is left as None and the reason is
    recorded in `warnings`, so a young listing still produces a usable (if
    partial) setup instead of failing the whole scan.
    """
    if df is None or df.empty:
        raise InsufficientData("no bars provided")
    _validate(df, 1)

    df = df.sort_values("time_key") if "time_key" in df.columns else df
    snap = IndicatorSnapshot(close=float(df["close"].iloc[-1]))

    if len(df) >= sma_slow:
        s = sma_cross(df, sma_fast, sma_slow).iloc[-1]
        snap.sma_fast = _f(s["sma_fast"])
        snap.sma_slow = _f(s["sma_slow"])
        snap.sma_cross = str(s["cross"])
        if snap.sma_fast is not None and snap.sma_slow:
            snap.sma_trend = "bullish" if snap.sma_fast > snap.sma_slow else "bearish"
            snap.sma_gap_pct = (snap.sma_fast - snap.sma_slow) / snap.sma_slow * 100
    else:
        snap.warnings.append(f"sma_cross needs {sma_slow} bars, got {len(df)}")

    if len(df) >= 26:
        m = macd(df).iloc[-1]
        snap.macd = _f(m["macd"])
        snap.macd_signal = _f(m["macd_signal"])
        snap.macd_hist = _f(m["macd_hist"])
        snap.macd_cross = str(m["cross"])
        if snap.macd_hist is not None:
            snap.macd_state = "bullish" if snap.macd_hist > 0 else "bearish"
    else:
        snap.warnings.append(f"macd needs 26 bars, got {len(df)}")

    if len(df) >= bb_period:
        b = bollinger(df, bb_period).iloc[-1]
        snap.bb_upper = _f(b["bb_upper"])
        snap.bb_mid = _f(b["bb_mid"])
        snap.bb_lower = _f(b["bb_lower"])
        snap.bb_percent_b = _f(b["bb_percent_b"])
        snap.bb_bandwidth = _f(b["bb_bandwidth"])
        if snap.bb_percent_b is not None:
            pb = snap.bb_percent_b
            snap.bb_state = (
                "above" if pb > 1 else
                "below" if pb < 0 else
                "upper_half" if pb >= 0.5 else "lower_half"
            )
    else:
        snap.warnings.append(f"bollinger needs {bb_period} bars, got {len(df)}")

    return snap


def _f(value: Any) -> float | None:
    """Cast to float, mapping NaN/None to None (JSON-safe, prompt-safe)."""
    if value is None or pd.isna(value):
        return None
    return float(value)
