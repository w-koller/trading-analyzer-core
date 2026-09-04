"""The deterministic rotation score: which sectors money is moving into.

WHAT THIS MEASURES, AND WHAT IT DOES NOT

Every number here is computed in Python from stored plate bars (rule #1).
The model is not asked to score anything and does not phrase the output —
`alerts.py` and `signals.py` both settled that argument already, and the
reason applies with more force here: a rotation score that came back
different on two runs over identical data would be worse than no score.

What we can observe per sector, per session, is `close`, `volume`,
`turnover` and (from the snapshot) advancing/declining counts. Turnover is
UNSIGNED — it counts both sides of every trade — so none of this is net
flow, and nothing here may be described as such. What it supports is a
dispersion measure: how unusual is this sector's move, against the spread of
every other sector's move on the same day, and was it carried on unusual
volume for *this* sector.

Signed institutional-vs-retail flow exists in exactly one place, sector
ETFs, because `get_capital_flow` refuses plate codes. That lives in
`sector_etfs` and is reported ALONGSIDE the score, never folded into it —
it covers ~24 of 262 plates, and a component present for 9% of the universe
would make scores incomparable across the rest while laundering "we have no
ETF for this sector" into "this sector had no institutional flow" (the
decisions #52 failure).

FOUR WINDOWS, READING DIFFERENT EVIDENCE

Not one score with four sets of weights. A day reads thrust and breadth —
what just happened. A quarter reads persistence — whether it stuck. That is
`signals.py`'s two-horizon argument, and it is why the 1-session window has
no `persistence` key at all rather than a degenerate one, and why the longer
windows have no `breadth` (a snapshot has no history to look back through).

The weights are PRIORS. Nothing is fitted to realised outcomes, so every row
ships the components it was summed from — a ranking whose ranking cannot be
inspected is a black box (decisions #66).

THE GUARDS

`thesis_scorecard` (#67) had to learn that a corpus will flatter itself
given any chance, and every guard below is a version of something that
already went wrong once:

  (c) is the important one. On a day the whole market rises, an ABSOLUTE
      measure reports inflow into all 145 industries at once — one market
      observation wearing 145 hats, which is exactly the failure #67(c)
      caught in the scorecard. Every return here is therefore relative to
      the cross-sectional median of the same window on the same day, and
      scaled by the cross-sectional dispersion. The baseline is the MEDIAN
      SECTOR, equal-weighted, not a cap-weighted market — the right zero for
      a dispersion measure, and it must be labelled as that and nothing else.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any

from app import db
# From its real home, not through `sdk_gateway`'s back-compat re-export.
# That module imports `moomoo` at module scope, so the old path made the whole
# of this file — including `build_scores`, which is pure arithmetic over its
# inputs — unimportable on a box without the SDK. Lifting the limiter out was
# done for exactly this reason (see app/utils/rate_limiter.py). Same object
# either way; `sector_universe` and `sector_etfs` are corrected alongside it.
from app.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

#: Sessions, not calendar days: daily / weekly / monthly / quarterly.
WINDOWS: tuple[int, ...] = (1, 5, 21, 63)

#: A window emits NOTHING below this, rather than a low-confidence row.
#: "Not yet knowable" and "knowable and neutral" must never share a
#: representation (decisions #69d).
MIN_SESSIONS: dict[int, int] = {1: 2, 5: 6, 21: 22, 63: 64}

#: A 3-member plate is one stock wearing a sector's name. The row still
#: renders — a visibly-building corpus is useful (#69b) — but marked
#: insufficient. A count of 0 means UNKNOWN (the member refresh is a rotating
#: slice and has not reached this plate yet), not "no constituents".
MIN_CONSTITUENTS = 5

#: Below this share of a window's weight actually populated, no score is
#: emitted at all.
MIN_COVERAGE = 0.5

#: Sessions of history used as the turnover baseline.
TRAILING_SESSIONS = 63

#: A session whose cross-sectional median turnover falls below this fraction
#: of the trailing median is a half-day or a holiday. Detected FROM THE DATA,
#: because this project does not hardcode exchange calendars (decisions #9).
THIN_SESSION_RATIO = 0.4

#: A single-session relative move beyond this is treated as an index rebase
#: rather than a price move, and excluded from persistence. Measured over 145
#: Semiconductor sessions the largest real move was 8.2%, so this is well
#: clear of ordinary volatility.
SUSPECT_BAR_PCT = 25.0

#: Floor on cross-sectional dispersion, so a day when every sector moved
#: identically cannot divide by ~0 and turn rounding noise into a signal.
MIN_DISPERSION_PCT = 0.25

#: log(2): a doubling of a sector's own normal turnover maps to tanh(1).
TURNOVER_SCALE = math.log(2.0)

#: Calendar days of plate bars to keep on hand. 180 covers the 63-session
#: window plus its 63-session trailing baseline with room for holidays.
BACKFILL_DAYS = 180

#: `request_history_kline` is 60 calls / 30s — measured, and stated by the
#: server as an ordinary error string. Paced below it here rather than inside
#: the gateway on purpose: a shared limiter would make an interactive chart
#: request queue behind this job's 262-plate pass, and the scanner has never
#: needed one (its 48 tickers are spread over 20-60 minutes behind
#: `market_data`'s cache). Same division of labour as `get_user_security`,
#: whose limit is documented on the gateway and paced by `watchlist_service`.
_KLINE_CALLS = 48
_KLINE_WINDOW = 30.0

WEIGHTS: dict[int, dict[str, float]] = {
    1: {"rel_return": 0.40, "turnover_thrust": 0.35, "breadth": 0.25},
    5: {"rel_return": 0.35, "turnover_thrust": 0.25, "persistence": 0.25, "acceleration": 0.15},
    21: {"rel_return": 0.35, "turnover_thrust": 0.20, "persistence": 0.30, "acceleration": 0.15},
    63: {"rel_return": 0.40, "turnover_thrust": 0.15, "persistence": 0.35, "acceleration": 0.10},
}


@dataclass
class SectorScore:
    plate_code: str
    as_of_date: str
    window_days: int
    plate_name: str = ""
    plate_class: str = ""
    sector_group: str = ""
    score: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    components_missing: list[str] = field(default_factory=list)
    rel_return_pct: float | None = None
    turnover_thrust: float | None = None
    breadth: float | None = None
    persistence: float | None = None
    news_thrust: float | None = None
    sessions_used: int = 0
    constituents: int | None = None
    coverage: float = 0.0
    thin_session: bool = False
    sufficient: bool = False
    available: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plate_code": self.plate_code,
            "as_of_date": self.as_of_date,
            "window_days": self.window_days,
            "plate_name": self.plate_name,
            "plate_class": self.plate_class,
            "sector_group": self.sector_group,
            "score": self.score,
            "components": self.components,
            "components_missing": self.components_missing,
            "rel_return_pct": self.rel_return_pct,
            "turnover_thrust": self.turnover_thrust,
            "breadth": self.breadth,
            "persistence": self.persistence,
            "news_thrust": self.news_thrust,
            "sessions_used": self.sessions_used,
            "constituents": self.constituents,
            "coverage": self.coverage,
            "thin_session": self.thin_session,
            "sufficient": self.sufficient,
            "available": self.available,
            "reason": self.reason,
        }


# --- small numeric helpers -------------------------------------------------


def _f(value: Any) -> float | None:
    """None for anything that is not a usable finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _dispersion(values: list[float]) -> float:
    """Normal-consistent MAD, floored.

    Deliberately not the standard deviation: one plate rebasing its index
    would inflate an SD enough to flatten every real signal that day, and
    the MAD simply ignores it.
    """
    if not values:
        return MIN_DISPERSION_PCT
    med = median(values)
    mad = median([abs(v - med) for v in values])
    return max(1.4826 * mad, MIN_DISPERSION_PCT)


def _pct_return(bars: list[dict[str, Any]], window: int, offset: int = 0) -> float | None:
    """Percent close-to-close return over `window` sessions ending `offset`
    sessions before the newest bar."""
    end = len(bars) - 1 - offset
    start = end - window
    if start < 0 or end < 0:
        return None
    a, b = _f(bars[start].get("close")), _f(bars[end].get("close"))
    if a is None or b is None or a == 0:
        return None
    return (b / a - 1.0) * 100.0


def _window_turnover(bars: list[dict[str, Any]], window: int, offset: int = 0) -> float | None:
    end = len(bars) - offset
    start = end - window
    if start < 0:
        return None
    vals = [_f(b.get("turnover")) for b in bars[start:end]]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _sign(value: float | None) -> float:
    if value is None or value == 0:
        return 0.0
    return 1.0 if value > 0 else -1.0


# --- the score -------------------------------------------------------------


def build_scores(
    bars_by_plate: dict[str, list[dict[str, Any]]],
    breadth_by_plate: dict[str, dict[str, Any]],
    meta: dict[str, dict[str, Any]],
    as_of_date: str,
    news_by_plate: dict[str, float] | None = None,
) -> list[SectorScore]:
    """Score every plate over every window. Pure over its inputs; never raises.

    `bars_by_plate` values are oldest-first and must END on `as_of_date`'s
    bar. A plate whose newest bar is older than that is scored on what it
    has and reports fewer `sessions_used` — it is not silently aligned to
    someone else's date.
    """
    news_by_plate = news_by_plate or {}
    out: list[SectorScore] = []

    # Thin-session detection, once for the whole universe: compare the newest
    # session's cross-sectional median turnover against the median of the
    # trailing sessions' medians. A half-day shows up as a fraction of normal
    # volume across every sector at once, which is what makes it detectable
    # without a calendar.
    thin_session = _detect_thin_session(bars_by_plate)

    for window in WINDOWS:
        weights = WEIGHTS[window]
        total_w = sum(weights.values())

        # --- cross-sectional baselines for THIS window -------------------
        cur_returns: dict[str, float] = {}
        prev_returns: dict[str, float] = {}
        for code, bars in bars_by_plate.items():
            r = _pct_return(bars, window)
            if r is not None:
                cur_returns[code] = r
            p = _pct_return(bars, window, offset=window)
            if p is not None:
                prev_returns[code] = p

        if not cur_returns:
            continue
        cur_median = median(cur_returns.values())
        cur_disp = _dispersion([v - cur_median for v in cur_returns.values()])
        prev_median = median(prev_returns.values()) if prev_returns else None

        # Daily relative returns, needed by persistence. Computed once per
        # window pass rather than per plate.
        daily_rel = _daily_relative_returns(bars_by_plate, window)

        for code, bars in bars_by_plate.items():
            info = meta.get(code, {})
            sc = SectorScore(
                plate_code=code,
                as_of_date=as_of_date,
                window_days=window,
                plate_name=info.get("plate_name", ""),
                plate_class=info.get("plate_class", ""),
                sector_group=info.get("sector_group") or "",
                constituents=info.get("constituent_count"),
                thin_session=thin_session,
                news_thrust=news_by_plate.get(code),
            )
            sc.sessions_used = max(0, len(bars) - 1)

            # (a) Below the floor a window emits NOTHING scoreable.
            if sc.sessions_used < MIN_SESSIONS[window]:
                sc.available = False
                sc.reason = f"needs {MIN_SESSIONS[window]} sessions, has {sc.sessions_used}"
                out.append(sc)
                continue

            components: dict[str, float] = {}
            missing: list[str] = []

            # --- rel_return ---------------------------------------------
            raw = cur_returns.get(code)
            if raw is None:
                missing.append("rel_return")
            else:
                sc.rel_return_pct = round(raw - cur_median, 4)
                components["rel_return"] = math.tanh(sc.rel_return_pct / cur_disp)

            # --- turnover_thrust ----------------------------------------
            thrust = _turnover_thrust(bars, window)
            if thrust is None:
                missing.append("turnover_thrust")
            else:
                # Signed by the direction of the relative move: heavy volume
                # is evidence of conviction in whichever direction the sector
                # actually went, and unsigned volume says nothing about
                # rotation at all.
                sc.turnover_thrust = round(thrust, 4)
                components["turnover_thrust"] = thrust * _sign(sc.rel_return_pct)

            # --- breadth (1-session only) --------------------------------
            if "breadth" in weights:
                b = _breadth(breadth_by_plate.get(code))
                if b is None:
                    missing.append("breadth")
                else:
                    sc.breadth = round(b, 4)
                    components["breadth"] = b

            # --- persistence (multi-session only) ------------------------
            if "persistence" in weights:
                p = _persistence(code, bars, daily_rel, window, sc.rel_return_pct)
                if p is None:
                    missing.append("persistence")
                else:
                    sc.persistence = round(p, 4)
                    components["persistence"] = p

            # --- acceleration --------------------------------------------
            if "acceleration" in weights:
                a = _acceleration(
                    code, cur_returns, prev_returns, cur_median, prev_median, cur_disp
                )
                if a is None:
                    missing.append("acceleration")
                else:
                    components["acceleration"] = a

            # (g) Coverage. A missing component contributes 0.0, which shrinks
            # the magnitude toward neutral — the conservative direction. The
            # surviving weights are deliberately NOT renormalised: doing that
            # would silently amplify whatever happened to be present.
            have_w = sum(w for k, w in weights.items() if k in components)
            sc.coverage = round(have_w / total_w, 4) if total_w else 0.0
            sc.components = {k: round(v, 4) for k, v in components.items()}
            sc.components_missing = missing

            if sc.coverage < MIN_COVERAGE:
                sc.available = False
                sc.reason = f"coverage {sc.coverage:.0%} below {MIN_COVERAGE:.0%}"
                out.append(sc)
                continue

            sc.score = round(sum(components[k] * weights[k] for k in components), 4)

            # (b)/(e) Sufficiency is separate from availability: the row is
            # rendered either way, but an insufficient one loses all emphasis.
            enough_members = (sc.constituents or 0) >= MIN_CONSTITUENTS
            sc.sufficient = bool(
                enough_members
                and sc.coverage >= MIN_COVERAGE
                and not (window == 1 and thin_session)
            )
            if not sc.sufficient:
                if not enough_members:
                    sc.reason = (
                        "constituent count unknown"
                        if not sc.constituents
                        else f"only {sc.constituents} constituents"
                    )
                elif window == 1 and thin_session:
                    sc.reason = "thin session (likely a half day)"
            out.append(sc)

    return out


def _detect_thin_session(bars_by_plate: dict[str, list[dict[str, Any]]]) -> bool:
    """Was the newest session unusually light across the WHOLE universe?"""
    newest: list[float] = []
    trailing: list[list[float]] = []
    for bars in bars_by_plate.values():
        if len(bars) < 2:
            continue
        t = _f(bars[-1].get("turnover"))
        if t is None or t <= 0:
            continue
        newest.append(t)
        prior = [_f(b.get("turnover")) for b in bars[-TRAILING_SESSIONS - 1 : -1]]
        prior = [p for p in prior if p is not None and p > 0]
        if prior:
            trailing.append(prior)
    if not newest or not trailing:
        return False
    latest_med = median(newest)
    baseline = median([median(p) for p in trailing])
    if baseline <= 0:
        return False
    return latest_med < THIN_SESSION_RATIO * baseline


def _turnover_thrust(bars: list[dict[str, Any]], window: int) -> float | None:
    """This window's turnover against THIS PLATE's own trailing normal.

    (d) Compared only to itself, never cross-sectionally: Semiconductors
    turns over $117B a day and a niche plate $200M, so a cross-sectional
    volume comparison would rank sectors by size forever.
    """
    cur = _window_turnover(bars, window)
    if cur is None or cur <= 0:
        return None
    prior = [_f(b.get("turnover")) for b in bars[: len(bars) - window]]
    prior = [p for p in prior if p is not None and p > 0]
    if len(prior) < window:
        return None
    prior = prior[-TRAILING_SESSIONS:]
    baseline = median(prior) * window
    if baseline <= 0:
        return None
    return math.tanh(math.log(cur / baseline) / TURNOVER_SCALE)


def _breadth(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    up, down = _f(row.get("raise_count")), _f(row.get("fall_count"))
    flat = _f(row.get("equal_count")) or 0.0
    if up is None or down is None:
        return None
    total = up + down + flat
    if total <= 0:
        return None
    return (up - down) / total


def _daily_relative_returns(
    bars_by_plate: dict[str, list[dict[str, Any]]], window: int
) -> dict[str, list[float | None]]:
    """Per-session returns minus that session's cross-sectional median.

    Built for the whole universe at once so each session's median is taken
    across every sector, which is what makes the result *relative*.
    """
    per_plate: dict[str, list[float | None]] = {}
    for code, bars in bars_by_plate.items():
        series: list[float | None] = []
        for i in range(max(1, len(bars) - window), len(bars)):
            a, b = _f(bars[i - 1].get("close")), _f(bars[i].get("close"))
            series.append(None if (a is None or b is None or a == 0) else (b / a - 1.0) * 100.0)
        per_plate[code] = series
    length = max((len(v) for v in per_plate.values()), default=0)
    medians: list[float] = []
    for i in range(length):
        vals = [v[i] for v in per_plate.values() if len(v) > i and v[i] is not None]
        medians.append(median(vals) if vals else 0.0)
    return {
        code: [None if v is None else v - medians[i] for i, v in enumerate(series)]
        for code, series in per_plate.items()
    }


def _persistence(
    code: str,
    bars: list[dict[str, Any]],
    daily_rel: dict[str, list[float | None]],
    window: int,
    window_rel: float | None,
) -> float | None:
    """Did the window's move happen across its sessions, or on one of them?

    +1 means every session agreed with the window's direction; 0 means half
    did; -1 means the sessions contradicted it and one outsized day carried
    the whole move. That last case is the one worth separating from a real
    reallocation, which is the whole point of the component.

    (f) Suspect bars are excluded from BOTH the numerator and the
    denominator, so an index rebase neither counts as agreement nor dilutes
    the sessions that are real.
    """
    series = daily_rel.get(code) or []
    if not series or window_rel is None:
        return None
    if window_rel == 0:
        # Measurable, and the answer is "no direction to persist in". That is
        # NOT the same as a missing component: None here would cost coverage
        # and imply the data was unavailable, when in fact this sector simply
        # tracked the median exactly. Missing means "could not measure";
        # 0.0 means "measured, and it is neutral".
        return 0.0
    suspect = [bool(b.get("suspect_bar")) for b in bars[-len(series) :]]
    usable = [
        v
        for i, v in enumerate(series)
        if v is not None
        and not (i < len(suspect) and suspect[i])
        and abs(v) <= SUSPECT_BAR_PCT
    ]
    if not usable:
        return None
    want = _sign(window_rel)
    agree = sum(1 for v in usable if _sign(v) == want)
    frac = agree / len(usable)
    return (2.0 * frac - 1.0) * want


def _acceleration(
    code: str,
    cur_returns: dict[str, float],
    prev_returns: dict[str, float],
    cur_median: float,
    prev_median: float | None,
    dispersion: float,
) -> float | None:
    """Is the relative move speeding up against the window just before it?

    Compared against the SAME plate's immediately preceding window of equal
    length, so it is well defined for the longest window too — there is no
    "next window up" to lean on at 63 sessions.
    """
    cur = cur_returns.get(code)
    prev = prev_returns.get(code)
    if cur is None or prev is None or prev_median is None:
        return None
    delta = (cur - cur_median) - (prev - prev_median)
    return math.tanh(delta / dispersion)


# --- persistence to disk and ingest ----------------------------------------


def persist(scores: list[SectorScore]) -> int:
    """Write scoreable rows.

    Rows marked unavailable are deliberately NOT stored: "this window is not
    yet knowable" is the ABSENCE of a row, not a row saying zero. A reader
    that finds nothing for the 63-session window on a young corpus is being
    told the truth (decisions #69d).
    """
    rows = [s.to_dict() for s in scores if s.available and s.score is not None]
    return db.upsert_rotation_scores(rows)


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            return frame.to_dict("records")
        except Exception:
            return []
    return list(frame) if isinstance(frame, list) else []


def _news_thrust(plate_codes: list[str]) -> dict[str, float]:
    """Recent article volume per plate against its own 30-day normal.

    REPORTED, never scored. News coverage is driven by a price move at least
    as much as it drives one, so folding it into the score would make the
    score partly a measure of itself. It is also structurally incomplete:
    `news_article_tickers` links articles only to WATCHLIST tickers
    (decisions #42), so a sector holding none of the user's names correctly
    reports nothing rather than zero.
    """
    if not plate_codes:
        return {}
    try:
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT m.plate_code AS plate_code,
                       SUM(CASE WHEN a.published_at >= datetime('now','-5 days')
                                THEN 1 ELSE 0 END) AS recent,
                       COUNT(*) AS total
                FROM sector_plate_members m
                JOIN news_article_tickers t ON t.code = m.code
                JOIN news_articles a ON a.id = t.article_id
                WHERE a.published_at >= datetime('now','-30 days')
                GROUP BY m.plate_code
                """
            ).fetchall()
    except Exception as exc:  # reporting only — never fatal to a scoring run
        logger.debug("news thrust unavailable: %s", exc)
        return {}
    out: dict[str, float] = {}
    for r in rows:
        total = r["total"] or 0
        if total <= 0:
            continue
        expected = total * (5.0 / 30.0)
        out[r["plate_code"]] = round(
            math.tanh(math.log(((r["recent"] or 0) + 1) / (expected + 1))), 4
        )
    return out


def score_stored(market: str = "US", as_of_date: str | None = None) -> int:
    """Score whatever is already in `sector_bars`.

    Separated from `ingest` so a scoring change can be re-run over history
    without spending the gateway budget again — and so the offline tests can
    exercise the whole path with no gateway at all.
    """
    plates = db.get_sector_universe(market=market)
    if not plates:
        return 0
    codes = [p["plate_code"] for p in plates]
    bars = {c: v for c, v in db.get_sector_bars(codes, limit_per_plate=TRAILING_SESSIONS * 3).items() if v}
    if not bars:
        return 0
    if as_of_date is None:
        as_of_date = max(v[-1]["bar_date"] for v in bars.values())
    meta = {p["plate_code"]: p for p in plates}
    return persist(
        build_scores(
            bars,
            db.get_latest_breadth(market=market),
            meta,
            as_of_date,
            news_by_plate=_news_thrust(codes),
        )
    )


def ingest(gateway, market: str = "US", backfill_days: int = BACKFILL_DAYS) -> dict[str, Any]:
    """Fetch plate bars and breadth, score them, and persist.

    Klines are fetched holding NO database connection and everything is
    written in one transaction at the end — `get_connection()` sets
    `PRAGMA synchronous = FULL` on the premise that write volume is trivial,
    and ~1,600 rows in a burst breaks that premise unless batched
    (decisions #39).

    Never raises on a partial failure. A board built from 250 of 262 plates
    is worth far more than none, and the cross-sectional baseline is a median
    — it does not care that a few plates are missing.
    """
    from app.services import sector_etfs, sector_universe

    result: dict[str, Any] = {
        "market": market,
        "started_at": db.now_iso(),
        "plates": 0,
        "bars_written": 0,
        "breadth_written": 0,
        "scores_written": 0,
        "as_of_date": None,
        "kline_failures": 0,
        "failures": [],
    }

    # The plate LIST is cheap (2 calls) and refreshed on staleness; the member
    # lists are a rotating slice every run regardless, because get_plate_stock
    # is 10/30s and a full pass would hold the gateway for 13 minutes.
    result["universe"] = sector_universe.refresh_universe(gateway, market=market)

    plates = sector_universe.plate_universe(market)
    result["plates"] = len(plates)
    if not plates:
        result["failures"].append("universe is empty")
        return result

    codes = [p["plate_code"] for p in plates]
    end = date.today()
    start = end - timedelta(days=backfill_days)

    bar_rows: list[dict[str, Any]] = []
    kline_limiter = RateLimiter(_KLINE_CALLS, _KLINE_WINDOW, "sector klines")
    for code in codes:
        try:
            kline_limiter.acquire()
            frame = gateway.get_history_kline(code, start=start.isoformat(), end=end.isoformat())
        except Exception as exc:
            result["kline_failures"] += 1
            if len(result["failures"]) < 10:
                result["failures"].append(f"kline:{code}: {exc}")
            continue
        for rec in _frame_records(frame):
            bar_date = str(rec.get("time_key") or "")[:10]
            if not bar_date:
                continue
            change = _f(rec.get("change_rate"))
            bar_rows.append(
                {
                    "plate_code": code,
                    "bar_date": bar_date,
                    "close": _f(rec.get("close")),
                    "change_rate": change,
                    "volume": _f(rec.get("volume")),
                    "turnover": _f(rec.get("turnover")),
                    "suspect_bar": bool(change is not None and abs(change) > SUSPECT_BAR_PCT),
                }
            )

    # `as_of_date` comes from the newest BAR, never the clock, so a run at an
    # unexpected hour labels its rows with what they actually are
    # (decisions #32). It is also what makes a re-run idempotent.
    as_of = max((b["bar_date"] for b in bar_rows), default=None)

    breadth_rows: list[dict[str, Any]] = []
    try:
        for chunk_start in range(0, len(codes), 400):
            for row in gateway.get_snapshot(codes[chunk_start : chunk_start + 400]):
                if not row.get("plate_valid"):
                    continue
                breadth_rows.append(
                    {
                        "plate_code": row.get("code"),
                        "as_of_date": as_of or str(row.get("update_time") or "")[:10],
                        "raise_count": _f(row.get("plate_raise_count")),
                        "fall_count": _f(row.get("plate_fall_count")),
                        "equal_count": _f(row.get("plate_equal_count")),
                        "last_price": _f(row.get("last_price")),
                        "turnover": _f(row.get("turnover")),
                        "partial_session": True,
                    }
                )
    except Exception as exc:
        result["failures"].append(f"snapshot: {exc}")

    result["bars_written"] = db.upsert_sector_bars(bar_rows)
    result["breadth_written"] = db.upsert_sector_breadth(breadth_rows)
    result["as_of_date"] = as_of

    # The ETF leg is reported beside the score and is never an input to it,
    # so its failure must not stop the board being built.
    try:
        result["etf_flows"] = sector_etfs.ingest_flows(gateway)
    except Exception as exc:
        result["failures"].append(f"etf_flows: {exc}")

    if not as_of:
        result["failures"].append("no bars, nothing to score")
        return result

    result["scores_written"] = score_stored(market=market, as_of_date=as_of)
    result["finished_at"] = db.now_iso()
    logger.info(
        "sector ingest %s: %d plates, %d bars, %d breadth, %d scores, as_of=%s, %d kline failures",
        market, result["plates"], result["bars_written"], result["breadth_written"],
        result["scores_written"], as_of, result["kline_failures"],
    )
    return result


# --- read side -------------------------------------------------------------


def rotation_board(
    market: str = "US",
    window_days: int = 5,
    top_n: int = 10,
    plate_class: str | None = None,
) -> dict[str, Any]:
    """Sectors money moved into and out of, best and worst first.

    Degrades honestly: an empty corpus answers `available: false` with a
    reason rather than an empty list that reads as "nothing is rotating"
    (the decisions #47 shape).
    """
    if window_days not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    rows = db.get_rotation_board(market=market, window_days=window_days, plate_class=plate_class)
    base = {
        "market": market,
        "window_days": window_days,
        "windows": list(WINDOWS),
        "plate_class": plate_class,
        "min_constituents": MIN_CONSTITUENTS,
        "min_sessions": MIN_SESSIONS[window_days],
        # Stated in the payload rather than assumed by the UI: this is a
        # DISPERSION measure, and calling its zero "the market" would be a
        # different and wrong claim.
        "baseline": "the median sector, equal-weighted — not a cap-weighted market",
    }
    if not rows:
        return {
            **base,
            "available": False,
            "reason": "no scores yet — the sector refresh has not run for this window",
            "inflow": [],
            "outflow": [],
        }
    scored = [r for r in rows if r.get("score") is not None]
    return {
        **base,
        "available": True,
        "reason": None,
        "as_of_date": rows[0]["as_of_date"],
        "scored": len(scored),
        "sufficient": sum(1 for r in scored if r.get("sufficient")),
        "thin_session": bool(rows[0].get("thin_session")),
        "inflow": scored[:top_n],
        "outflow": list(reversed(scored[-top_n:])) if len(scored) > top_n else [],
    }


def rotation_pairs(
    market: str = "US", window_days: int = 5, top_n: int = 5
) -> dict[str, Any]:
    """Opposite-moving sectors that are genuinely related to each other.

    **These are NOT traced flows and must never be rendered as any.** Nothing
    available — not Moomoo, and not 13F either — links a dollar leaving one
    sector to a dollar arriving in another. What a pair says is that two
    RELATED sectors moved in opposite directions over the same window. That
    is a correlation worth a look, and it is not a conservation law, which is
    exactly why this returns a ranked list rather than a Sankey.

    Relatedness comes from shared constituents, so "Semiconductors down,
    Software-Infrastructure up" surfaces only if the two genuinely overlap —
    not merely because they sat at opposite ends of the ranking.
    """
    from app.services import sector_universe

    board = rotation_board(market=market, window_days=window_days, top_n=50)
    base: dict[str, Any] = {
        "market": market,
        "window_days": window_days,
        "pairs": [],
        "note": (
            "Two related sectors moved in opposite directions over the same "
            "window. That is a correlation between them, not a dollar traced "
            "from one to the other."
        ),
    }
    if not board["available"]:
        return {**base, "available": False, "reason": board["reason"]}
    scored = list(board["inflow"]) + list(board["outflow"])

    # A pair needs constituent data on BOTH sides, and member lists arrive as
    # a rotating slice — so on a young universe the honest answer is "not yet
    # enough overlap data", NOT an empty list that reads as "nothing is
    # rotating". Reported as coverage so the UI can say which it is.
    with_members = sum(1 for r in scored if (r.get("constituent_count") or 0) > 0)
    base["coverage"] = {
        "rows": len(scored),
        "with_members": with_members,
        "share": round(with_members / len(scored), 3) if scored else 0.0,
    }
    if with_members < 2:
        return {
            **base,
            "available": False,
            "reason": (
                f"only {with_members} of {len(scored)} ranked sectors have "
                "constituent data yet — pairing needs both sides, and member "
                "lists are still being fetched"
            ),
        }
    gainers = sorted((r for r in scored if (r.get("score") or 0) > 0),
                     key=lambda r: r["score"], reverse=True)[:12]
    losers = sorted((r for r in scored if (r.get("score") or 0) < 0),
                    key=lambda r: r["score"])[:12]

    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for loser in losers:
        related = {
            r["plate_code"]: r
            for r in sector_universe.related_plates(loser["plate_code"], limit=25)
        }
        for winner in gainers:
            link = related.get(winner["plate_code"])
            if not link:
                continue
            key = (loser["plate_code"], winner["plate_code"])
            if key in seen:
                continue
            seen.add(key)
            both = bool(loser.get("sufficient") and winner.get("sufficient"))
            pairs.append(
                {
                    "from": {
                        "plate_code": loser["plate_code"],
                        "plate_name": loser["plate_name"],
                        "score": loser["score"],
                        "sufficient": bool(loser.get("sufficient")),
                    },
                    "to": {
                        "plate_code": winner["plate_code"],
                        "plate_name": winner["plate_name"],
                        "score": winner["score"],
                        "sufficient": bool(winner.get("sufficient")),
                    },
                    "link_basis": "shared_members",
                    "shared_members": link["shared_members"],
                    "jaccard": link["jaccard"],
                    "spread": round(winner["score"] - loser["score"], 4),
                    "both_sufficient": both,
                }
            )
    pairs.sort(key=lambda p: (p["both_sufficient"], p["spread"] * p["jaccard"]), reverse=True)
    return {
        **base,
        "available": True,
        "reason": (
            None
            if pairs
            else "no related sectors moved in opposite directions this window"
        ),
        "pairs": pairs[:top_n],
    }


def sector_detail(plate_code: str, window_days: int = 5) -> dict[str, Any]:
    """One sector: its score, its history, its constituents, its neighbours."""
    from app.services import sector_etfs, sector_universe

    if window_days not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    plate = {p["plate_code"]: p for p in db.get_sector_universe()}.get(plate_code)
    if not plate:
        return {"available": False, "reason": "unknown plate", "plate_code": plate_code}
    board = db.get_rotation_board(market=plate["market"], window_days=window_days)
    members = db.get_plate_members(plate_code)
    return {
        "available": True,
        "reason": None,
        "plate_code": plate_code,
        "plate_name": plate["plate_name"],
        "plate_class": plate["plate_class"],
        "sector_group": plate.get("sector_group"),
        "market": plate["market"],
        "window_days": window_days,
        "windows": list(WINDOWS),
        # 0 means the rotating member refresh has not reached this plate yet.
        # That is UNKNOWN, not "no constituents", and the UI must say so.
        "constituent_count": plate.get("constituent_count") or 0,
        "score": next((r for r in board if r["plate_code"] == plate_code), None),
        "history": db.get_score_history(plate_code, window_days=window_days),
        "members": members,
        "watchlist_members": [m for m in members if m["on_watchlist"]],
        "related": sector_universe.related_plates(plate_code, limit=8),
        # Signed institutional flow where an ETF proxies for this sector, and
        # None where none does — reported BESIDE the score, never inside it.
        "etf_flow": sector_etfs.flow_for_plate(plate_code),
        "min_constituents": MIN_CONSTITUENTS,
    }
