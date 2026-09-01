"""Ranked opportunities: which watchlist names the stored evidence favours.

This is a RANKING, not a forecast, and the distinction is structural rather
than a disclaimer. Every number here is computed in Python from data already
stored (rule #1) — the model is not asked to score anything, and it does not
phrase the output either. That follows `alerts.py`'s precedent for the same
reason given there: text that has to be identical on every poll to be
trustworthy cannot come from a generative model, and a paraphrase of "below
the 170.00 stop" can come back as 107.00.

THE SCOPE PROBLEM, AND HOW IT IS KEPT HONEST

`alerts.py` scopes itself to held positions on purpose: "a watchlist-wide
engine would fire constantly; this scoping is the discipline that stops it
becoming one." This module IS watchlist-wide, so it has to be structurally
different rather than an exception to that rule:

    **Opportunities are PULL, never PUSH.** A ranked list the user looks at
    when they choose to — capped, inspectable, rendered only on the
    dashboard. It never produces an alert row, never enters push_service,
    never raises a notification, never carries a badge. Alerts remain the
    only thing that demands attention, and they remain held-positions-only.

Two horizons, and they are not the same score with different weights — they
read different evidence. Short-term reads momentum and positioning, which
say something about the next few days. Medium-term reads trend structure and
standing positioning. A pre-market pop is evidence about tomorrow and is not
evidence about next quarter, so it is simply absent from the medium model.

WHY CONVICTION ALONE IS NOT THE RANKING

Measured on the live corpus: 83% of stored theses score 4 or 5, and 15 rows
out of 2,000 are 7+. Sorting by conviction would be arbitrary tie-breaking
among the 5s. Conviction is one input among several, weighted accordingly.

The weights are PRIORS, not fitted values — nothing has been calibrated
against realised outcomes because there are none (n=1, decisions #36).
`thesis_scorecard` exists to make them checkable over time. Until it has
samples, every score ships its own components so a human can see what drove
it rather than trusting the number.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import db
from app.config import settings
from app.services import thesis_scorecard
from app.utils import market_hours

logger = logging.getLogger(__name__)

SHORT, MEDIUM = "short", "medium"
HORIZONS = (SHORT, MEDIUM)

TOP_N = 5                     # what the dashboard renders per horizon
# Theses per ticker used as agreement evidence. Eight is two days now that
# scans fire on session boundaries (four a day) rather than hourly. Under the
# old rotation the same number covered about four HOURS — and since daily
# klines do not change intraday, most of that was the model re-rolling the
# same uncertain call rather than independent evidence. Fewer, more widely
# spaced samples say more about whether a read persists.
HISTORY_DEPTH = 8
MIN_RISK_REWARD = 1.0         # below this the trade loses money if it works
MIN_SCORE = 0.45              # below this it is not worth a slot

# Priors. Named rather than inlined so the scorecard has something to argue
# with later, and so a reader can see the shape of the opinion at a glance.
WEIGHTS: dict[str, dict[str, float]] = {
    SHORT: {
        "conviction": 0.20,
        "persistence": 0.20,
        "conviction_trend": 0.10,
        "momentum": 0.20,
        "room_to_run": 0.15,
        "squeeze": 0.05,
        "extended_move": 0.10,
    },
    MEDIUM: {
        "conviction": 0.20,
        "persistence": 0.25,
        "trend_structure": 0.25,
        "cross_event": 0.10,
        "not_overextended": 0.10,
        "positioning": 0.10,
    },
}


@dataclass
class Opportunity:
    code: str
    name: str
    market: str
    horizon: str
    direction: str
    score: float
    components: dict[str, float]
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    stop_distance_pct: float | None = None
    target_distance_pct: float | None = None
    spot: float | None = None
    setup_id: int | None = None
    conviction: int | None = None
    thesis_created_at: str | None = None
    agreeing: int = 0
    of_last: int = 0
    held: bool = False
    is_delayed_data: bool = False
    data_as_of: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "market": self.market,
            "horizon": self.horizon, "direction": self.direction,
            "score": round(self.score, 4),
            # Shipped with every row on purpose: the weights are priors, and
            # a ranked list whose ranking cannot be inspected is a black box.
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "risk_reward": round(self.risk_reward, 2) if self.risk_reward else None,
            # The ratio alone is misleading and a big one is not a better
            # trade. A 21:1 seen live on US.SNDK came from a stop sitting
            # 1.1% under the entry — a stop that tight is likely to be taken
            # out by noise, so the impressive ratio was really a warning.
            # Reporting both legs makes the number interpretable instead.
            "stop_distance_pct": self.stop_distance_pct,
            "target_distance_pct": self.target_distance_pct,
            "spot": self.spot,
            "setup_id": self.setup_id, "conviction": self.conviction,
            "thesis_created_at": self.thesis_created_at,
            "agreeing": self.agreeing, "of_last": self.of_last,
            "held": self.held,
            "is_delayed_data": self.is_delayed_data,
            "data_as_of": self.data_as_of,
            "notes": self.notes,
            "href": f"/ticker/{self.code}",
        }


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _sign_of(direction: str) -> int:
    return 1 if direction == "Bullish" else -1 if direction == "Bearish" else 0


# --- components ---------------------------------------------------------
# Each returns [0, 1]: "how much does this favour the thesis's direction".

def _persistence(history: list[dict[str, Any]], direction: str) -> tuple[float, int, int]:
    """How consistently the model has said the same thing about this ticker.

    A single thesis is one sample of an uncertain model — measured on the
    corpus, 24% of consecutive theses computed on identical bars at
    essentially identical price came back with a different direction. Eight
    in a row saying Bullish is therefore a materially stronger claim than
    one, and it costs nothing to look because the rows are already stored.

    Spanning two days of session scans rather than four hours of rotation
    also means the agreement survived new closing bars, not just repeated
    reads of one.
    """
    if not history:
        return 0.0, 0, 0
    agreeing = sum(1 for h in history if h["trade_direction"] == direction)
    return agreeing / len(history), agreeing, len(history)


def _conviction_trend(history: list[dict[str, Any]]) -> float:
    """Is the model getting more or less sure? Newest-first input.

    Compares the recent half against the older half rather than fitting a
    line: conviction is a small integer that moves in steps, and a slope
    over 10 points of a 1-10 scale reads noise as trend.
    """
    if len(history) < 4:
        return 0.5                       # no opinion, not a negative one
    half = len(history) // 2
    recent = sum(h["conviction_score"] for h in history[:half]) / half
    older = sum(h["conviction_score"] for h in history[half:]) / (len(history) - half)
    return _clamp01(0.5 + (recent - older) / 4.0)


def _momentum(ind: dict[str, Any], direction: str) -> float:
    """MACD histogram, signed to the thesis direction and price-scaled."""
    hist, close = _f(ind.get("macd_hist")), _f(ind.get("close"))
    if hist is None or not close:
        return 0.5
    scaled = (hist / close * 100) * _sign_of(direction)
    return _clamp01(0.5 + scaled / 2.0)


def _room_to_run(ind: dict[str, Any], walls: dict[str, Any] | None, direction: str) -> float:
    """How much space there is before the next structural obstacle.

    Combines the Bollinger position with the option wall ahead. A setup
    already pinned to the upper rail with a call wall 0.4% away has nowhere
    to go, however good the trend looks.
    """
    pb = _f(ind.get("bb_percent_b"))
    band_room = 0.5 if pb is None else _clamp01(1 - pb if direction == "Bullish" else pb)

    wall_room = 0.5
    if walls and walls.get("has_walls"):
        key = "call_wall_distance_pct" if direction == "Bullish" else "put_wall_distance_pct"
        dist = _f(walls.get(key))
        if dist is not None:
            wall_room = _clamp01(abs(dist) / 8.0)
    return (band_room + wall_room) / 2


def _squeeze(ind: dict[str, Any]) -> float:
    """Low bandwidth scores high: compression precedes expansion.

    Directionless on purpose — a squeeze says a move is more likely, not
    which way. It carries the smallest weight in the model for that reason.
    """
    bw = _f(ind.get("bb_bandwidth"))
    if bw is None:
        return 0.5
    return _clamp01(1 - bw / 0.30)


def _extended_move(mover: dict[str, Any] | None, direction: str) -> float:
    """Pre/after/overnight movement aligned with the direction.

    Short horizon only. The largest absolute move across the three sessions
    is used rather than a sum, because they are measured from different
    bases and adding them would be meaningless.
    """
    if not mover:
        return 0.5
    moves = [
        _f(mover.get(k)) for k in
        ("pre_change_pct", "after_change_pct", "overnight_change_pct")
    ]
    moves = [m for m in moves if m is not None]
    if not moves:
        return 0.5
    top = max(moves, key=abs)
    return _clamp01(0.5 + (top * _sign_of(direction)) / 6.0)


def _trend_structure(ind: dict[str, Any], direction: str) -> float:
    """SMA 50/200 alignment and separation — the medium-term backbone."""
    trend = ind.get("sma_trend")
    gap = _f(ind.get("sma_gap_pct"))
    if trend not in ("bullish", "bearish"):
        return 0.5
    aligned = (trend == "bullish") == (direction == "Bullish")
    if gap is None:
        return 0.65 if aligned else 0.35
    strength = _clamp01(abs(gap) / 12.0)
    return _clamp01(0.5 + strength / 2) if aligned else _clamp01(0.5 - strength / 2)


def _cross_event(ind: dict[str, Any], direction: str) -> float:
    """A golden/death cross on the latest bar, in the thesis's favour."""
    cross = ind.get("sma_cross")
    if cross not in ("golden", "death"):
        return 0.5
    return 1.0 if ((cross == "golden") == (direction == "Bullish")) else 0.0


def _not_overextended(ind: dict[str, Any]) -> float:
    """Distance from the 20-period mean, penalised in BOTH directions.

    Deliberately unsigned. For a multi-week entry, a name that has already
    run 15% past its mean is a worse entry than one sitting near it, whether
    the thesis is Bullish or Bearish — this scores the ENTRY, not the idea.
    """
    close, mid = _f(ind.get("close")), _f(ind.get("bb_mid"))
    if close is None or not mid:
        return 0.5
    return _clamp01(1 - abs((close - mid) / mid * 100) / 12.0)


def _positioning(walls: dict[str, Any] | None, direction: str) -> float:
    """Standing option positioning: put/call by open interest."""
    if not walls or not walls.get("has_walls"):
        return 0.5
    ratio = _f(walls.get("put_call_oi_ratio"))
    if ratio is None or ratio <= 0:
        return 0.5
    # A low P/C is call-heavy (bullish positioning); invert for Bearish.
    skew = _clamp01(1 - ratio / 2.0)
    return skew if direction == "Bullish" else 1 - skew


# --- levels -------------------------------------------------------------

def _levels(
    ind: dict[str, Any], walls: dict[str, Any] | None, setup: dict[str, Any],
    spot: float, direction: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Entry, stop, target and risk/reward — all from ALREADY-STORED numbers.

    Nothing here is invented. Stop is the thesis's own `suggested_stop`,
    which `validate_thesis` has already checked sits the right side of both
    the entry and the target (decisions #14). Target is the thesis's own, or
    the option wall ahead if that is nearer — a wall between here and the
    target is where the move realistically stalls.

    Entry is the thesis's own `suggested_entry` when it named one. Only when
    it did not does this fall back to the nearest real level the price would
    pull back to (fast SMA or the Bollinger mid, both computed
    deterministically in Python). That fallback is a PULLBACK assumption, and
    it is wrong for a breakout thesis — which is exactly why the model is
    asked for the level now and why this branch is the second choice rather
    than the only one. Rows written before `suggested_entry` existed carry
    NULL and land here too.
    """
    stop = _f(setup.get("suggested_stop"))
    target = _f(setup.get("suggested_target"))
    if stop is None or target is None:
        return None, None, None, None

    levels = [v for v in (_f(ind.get("sma_fast")), _f(ind.get("bb_mid"))) if v is not None]
    wall = None
    if walls and walls.get("has_walls"):
        wall = _f(walls.get("call_wall" if direction == "Bullish" else "put_wall"))

    stated = _f(setup.get("suggested_entry"))

    if direction == "Bullish":
        supports = [v for v in levels if v < spot]
        entry = stated if stated is not None else (max(supports) if supports else spot)
        if wall and entry < wall < target:
            target = wall
        if not (stop < entry < target):
            return None, None, None, None
        rr = (target - entry) / (entry - stop)
    elif direction == "Bearish":
        resistances = [v for v in levels if v > spot]
        entry = stated if stated is not None else (min(resistances) if resistances else spot)
        if wall and target < wall < entry:
            target = wall
        if not (target < entry < stop):
            return None, None, None, None
        rr = (entry - target) / (stop - entry)
    else:
        return None, None, None, None

    return round(entry, 4), round(stop, 4), round(target, 4), rr


# --- assembly -----------------------------------------------------------

def _score(horizon: str, ind, walls, setup, history, mover) -> tuple[float, dict[str, float]]:
    direction = setup["trade_direction"]
    persistence, _, _ = _persistence(history, direction)
    if horizon == SHORT:
        parts = {
            "conviction": setup["conviction_score"] / 10.0,
            "persistence": persistence,
            "conviction_trend": _conviction_trend(history),
            "momentum": _momentum(ind, direction),
            "room_to_run": _room_to_run(ind, walls, direction),
            "squeeze": _squeeze(ind),
            "extended_move": _extended_move(mover, direction),
        }
    else:
        parts = {
            "conviction": setup["conviction_score"] / 10.0,
            "persistence": persistence,
            "trend_structure": _trend_structure(ind, direction),
            "cross_event": _cross_event(ind, direction),
            "not_overextended": _not_overextended(ind),
            "positioning": _positioning(walls, direction),
        }
    w = WEIGHTS[horizon]
    return sum(parts[k] * w[k] for k in parts), parts


def _snapshot_of(setup: dict[str, Any]) -> dict[str, Any] | None:
    snap = setup.get("indicator_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except json.JSONDecodeError:
            return None
    return snap if isinstance(snap, dict) else None


def build_opportunities(
    tickers: list[dict[str, Any]],
    movers: dict[str, dict[str, Any]] | None = None,
    held_codes: set[str] | None = None,
    horizon: str = SHORT,
    top_n: int = TOP_N,
) -> list[dict[str, Any]]:
    """Rank `tickers` for one horizon. Pure over its inputs; never raises."""
    movers = movers or {}
    held = held_codes or set()
    codes = [t["code"] for t in tickers]
    history = db.get_setup_history(codes, HISTORY_DEPTH)

    out: list[Opportunity] = []
    for ticker in tickers:
        code = ticker["code"]
        rows = history.get(code) or []
        if not rows:
            continue
        setup = rows[0]
        direction = setup["trade_direction"]
        if direction == "Neutral":
            continue                      # no direction, nothing to act on

        # A Bearish read on something already held is an EXIT signal, and
        # alerts.py's thesis_contradicts_position already reports it. One
        # fact should produce one row, in the surface that demands attention.
        if direction == "Bearish" and code in held:
            continue

        snap = _snapshot_of(setup)
        if not snap:
            continue
        ind = snap.get("indicators") or {}
        walls = snap.get("walls")

        # Never rank on evidence that has gone off. Same staleness budget the
        # alert rules use, plus the bar-level check rule #7 exists for.
        # bar_age_days parses the raw time_key itself; do not pre-parse.
        age_days = market_hours.bar_age_days(snap.get("last_bar_time"))
        if snap.get("bars_stale"):
            continue
        created = db.parse_iso(setup["created_at"])
        if created is not None:
            thesis_age = (
                datetime.now(timezone.utc) - created
            ).total_seconds() / 86400
            if thesis_age > settings.alerts_setup_stale_days:
                continue

        mover = movers.get(code)
        spot = _f((mover or {}).get("last_price")) or _f(snap.get("spot")) \
            or _f(ind.get("close"))
        if not spot:
            continue

        entry, stop, target, rr = _levels(ind, walls, setup, spot, direction)
        stop_pct = target_pct = None
        if entry:
            if stop is not None:
                stop_pct = round(abs(entry - stop) / entry * 100, 2)
            if target is not None:
                target_pct = round(abs(target - entry) / entry * 100, 2)
        # The one filter that can disqualify a candidate from stored data
        # alone: a "top opportunity" whose own stop and target imply a losing
        # expectancy is not an opportunity.
        if rr is None or rr < MIN_RISK_REWARD:
            continue

        score, parts = _score(horizon, ind, walls, setup, rows, mover)
        if score < MIN_SCORE:
            continue

        _, agreeing, of_last = _persistence(rows, direction)
        notes = [f"{agreeing}/{of_last} recent theses agree"]
        # Only mention the wall when it actually lies AHEAD of the entry.
        # Seen live: US.SNDK at 1411 with a call wall at 900, i.e. the price
        # has run clear above the whole strike range. `_levels` already
        # ignores a wall behind the entry, but reporting it in prose implied
        # resistance overhead that is not there.
        if walls and walls.get("has_walls"):
            key = "call_wall" if direction == "Bullish" else "put_wall"
            wall_price = _f(walls.get(key))
            ahead = wall_price is not None and entry is not None and (
                wall_price > entry if direction == "Bullish" else wall_price < entry
            )
            if ahead:
                notes.append(f"{key.replace('_', ' ')} at {wall_price:g}")
        if stop_pct is not None and stop_pct < 2.0:
            # Deterministic, and phrased as the caution it is rather than
            # letting a large risk/reward read as unambiguously good.
            notes.append(f"tight stop ({stop_pct:.1f}% away)")
        if age_days is not None and age_days >= 1:
            notes.append(f"bars {age_days:.1f}d old")

        out.append(Opportunity(
            code=code, name=ticker.get("name") or code,
            market=ticker.get("market") or market_hours.market_of(code),
            horizon=horizon, direction=direction, score=score, components=parts,
            entry=entry, stop=stop, target=target, risk_reward=rr,
            stop_distance_pct=stop_pct, target_distance_pct=target_pct, spot=spot,
            setup_id=setup["id"], conviction=setup["conviction_score"],
            thesis_created_at=setup["created_at"],
            agreeing=agreeing, of_last=of_last,
            held=code in held,
            is_delayed_data=bool(setup["is_delayed_data"]),
            data_as_of=setup["data_as_of"],
            notes=notes,
        ))

    out.sort(key=lambda o: (-o.score, o.code))
    return [o.to_dict() for o in out[:top_n]]


def get_opportunities(
    market: str | None = None,
    held_codes: set[str] | None = None,
    movers: dict[str, dict[str, Any]] | None = None,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    """Both horizons plus the calibration state. The router's entry point."""
    tickers = db.get_enabled_tickers(market)
    horizons = {
        h: build_opportunities(tickers, movers, held_codes, h, top_n)
        for h in HORIZONS
    }
    card = thesis_scorecard.scorecard()
    return {
        "horizons": horizons,
        "counts": {h: len(v) for h, v in horizons.items()},
        # Surfaced so the UI states the calibration honestly instead of
        # implying these rankings have a track record they do not have.
        "calibrated": card["calibrated"],
        "scored_samples": card["total_samples"],
        "min_risk_reward": MIN_RISK_REWARD,
        "min_score": MIN_SCORE,
    }
