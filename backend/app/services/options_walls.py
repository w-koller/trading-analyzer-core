"""Call/put wall calculation from the options chain.

A "wall" is the strike carrying the most positioning — the level where
dealer hedging tends to pin or repel price. Calls and puts are aggregated
separately; distance from spot to each wall is one of the deterministic
inputs to the AI prompt (CLAUDE.md rule #1).

Walls are ranked on **open interest + traded volume**, not open interest
alone. The two measure different things and the spec asks for both:
open interest is the standing book, but the exchanges only publish it
once a day, so intraday it is up to a session stale; volume is same-day
flow, which is what actually moves a level. Both are counts of contracts,
so they share a unit and can be summed directly — `volume_weight` tunes
the balance if one turns out to dominate in practice.

Getting this data from Moomoo takes two calls, which is not obvious:
`get_option_chain()` returns contract *metadata* only (code, strike, type)
with no open interest at all. Open interest lives in
`get_market_snapshot()` on the individual option codes, as
`option_open_interest`, alongside `volume` and the greeks. So
chain -> codes -> snapshot -> aggregate.

Careful with field names on the snapshot row: `volume` is traded contracts,
while `ask_vol`/`bid_vol` are quote sizes and `option_net_open_interest`
is frequently "N/A". Verified against a live PLTR chain.

The maths lives in `compute_walls()`, a pure function over rows, so it is
testable without spending the account's option-data quota.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# get_market_snapshot accepts at most 400 codes per call.
SNAPSHOT_BATCH = 200


@dataclass
class OptionWalls:
    """Aggregated open-interest + volume structure for one expiry."""

    expiry: str
    spot: float | None = None

    call_wall: float | None = None
    call_wall_oi: int = 0
    call_wall_volume: int = 0
    call_wall_score: int = 0

    put_wall: float | None = None
    put_wall_oi: int = 0
    put_wall_volume: int = 0
    put_wall_score: int = 0

    total_call_oi: int = 0
    total_put_oi: int = 0
    total_call_volume: int = 0
    total_put_volume: int = 0

    put_call_oi_ratio: float | None = None
    put_call_volume_ratio: float | None = None

    call_wall_distance_pct: float | None = None
    put_wall_distance_pct: float | None = None

    strikes_considered: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_walls(self) -> bool:
        return self.call_wall is not None or self.put_wall is not None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["has_walls"] = self.has_walls
        return d


def compute_walls(
    snapshot_rows: Iterable[dict[str, Any]],
    expiry: str,
    spot: float | None = None,
    volume_weight: float = 1.0,
) -> OptionWalls:
    """Aggregate option snapshot rows into call/put walls.

    Pure and deterministic. `snapshot_rows` are option rows from
    get_market_snapshot(), each carrying option_type, option_strike_price,
    option_open_interest and volume.

    The wall is the strike with the highest `oi + volume_weight * volume`.
    Ties resolve to the lower strike so repeated scans of unchanged data
    don't flap between two equally-ranked levels.
    """
    walls = OptionWalls(expiry=expiry, spot=spot)

    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}
    call_vol: dict[float, int] = {}
    put_vol: dict[float, int] = {}
    skipped = 0

    for row in snapshot_rows:
        strike = _num(row.get("option_strike_price"))
        oi = _int(row.get("option_open_interest"))
        vol = _int(row.get("volume"))
        opt_type = str(row.get("option_type") or "").upper()

        if strike is None or opt_type not in ("CALL", "PUT"):
            skipped += 1
            continue
        if opt_type == "CALL":
            call_oi[strike] = call_oi.get(strike, 0) + oi
            call_vol[strike] = call_vol.get(strike, 0) + vol
        else:
            put_oi[strike] = put_oi.get(strike, 0) + oi
            put_vol[strike] = put_vol.get(strike, 0) + vol

    walls.strikes_considered = len(set(call_oi) | set(put_oi))
    if skipped:
        walls.warnings.append(f"{skipped} rows skipped (missing strike/type)")

    walls.total_call_oi = sum(call_oi.values())
    walls.total_put_oi = sum(put_oi.values())
    walls.total_call_volume = sum(call_vol.values())
    walls.total_put_volume = sum(put_vol.values())

    def score(oi_map: dict[float, int], vol_map: dict[float, int], strike: float) -> int:
        return int(oi_map.get(strike, 0) + volume_weight * vol_map.get(strike, 0))

    # A strike with no positioning at all is not a wall — max() over a chain
    # of zeroes would otherwise return an arbitrary strike that reads as a
    # real level. Requiring score > 0 rather than OI > 0 means a freshly
    # listed expiry that has traded today but not yet settled any OI still
    # produces a wall.
    call_scores = {k: score(call_oi, call_vol, k) for k in set(call_oi) | set(call_vol)}
    put_scores = {k: score(put_oi, put_vol, k) for k in set(put_oi) | set(put_vol)}

    if call_scores and max(call_scores.values()) > 0:
        walls.call_wall = max(call_scores, key=lambda k: (call_scores[k], -k))
        walls.call_wall_oi = call_oi.get(walls.call_wall, 0)
        walls.call_wall_volume = call_vol.get(walls.call_wall, 0)
        walls.call_wall_score = call_scores[walls.call_wall]
    else:
        walls.warnings.append("no call open interest or volume for this expiry")

    if put_scores and max(put_scores.values()) > 0:
        walls.put_wall = max(put_scores, key=lambda k: (put_scores[k], -k))
        walls.put_wall_oi = put_oi.get(walls.put_wall, 0)
        walls.put_wall_volume = put_vol.get(walls.put_wall, 0)
        walls.put_wall_score = put_scores[walls.put_wall]
    else:
        walls.warnings.append("no put open interest or volume for this expiry")

    # P/C ratio stays defined on open interest (the conventional reading);
    # the volume ratio is reported alongside it rather than replacing it.
    if walls.total_call_oi > 0:
        walls.put_call_oi_ratio = walls.total_put_oi / walls.total_call_oi
    if walls.total_call_volume > 0:
        walls.put_call_volume_ratio = walls.total_put_volume / walls.total_call_volume

    if spot:
        if walls.call_wall is not None:
            walls.call_wall_distance_pct = (walls.call_wall - spot) / spot * 100
        if walls.put_wall is not None:
            walls.put_wall_distance_pct = (walls.put_wall - spot) / spot * 100

    return walls


def fetch_walls(
    gateway,
    code: str,
    expiry: str | None = None,
    spot: float | None = None,
    max_contracts: int = SNAPSHOT_BATCH,
    volume_weight: float = 1.0,
) -> OptionWalls:
    """Fetch the chain for one expiry and reduce it to walls.

    `expiry` defaults to the nearest listed expiry. Only the first
    `max_contracts` contracts are priced, to bound the snapshot call — for
    a normal single-expiry chain that is the whole chain.
    """
    if expiry is None:
        expirations = gateway.get_option_expirations(code)
        if not expirations:
            return OptionWalls(expiry="", spot=spot,
                               warnings=[f"no option expirations listed for {code}"])
        # OpenD still lists recently-expired dates, and they sort first — so
        # taking expirations[0] picks a chain that already settled, whose OI
        # and volume describe nothing that can still happen. Take the nearest
        # expiry that has not passed.
        today = date.today().isoformat()
        future = [e for e in sorted(expirations) if e[:10] >= today]
        if not future:
            return OptionWalls(
                expiry="", spot=spot,
                warnings=[f"all {len(expirations)} listed expiries for {code} "
                          f"have passed (latest {max(expirations)})"],
            )
        expiry = future[0]

    chain = gateway.get_option_chain(code, expiry)
    codes = [row["code"] for row in chain if row.get("code")]
    if not codes:
        return OptionWalls(expiry=expiry, spot=spot,
                           warnings=[f"empty option chain for {code} {expiry}"])

    rows: list[dict[str, Any]] = []
    truncated = len(codes) > max_contracts
    for batch in _batched(codes[:max_contracts], SNAPSHOT_BATCH):
        rows.extend(gateway.get_snapshot(batch))

    walls = compute_walls(rows, expiry=expiry, spot=spot, volume_weight=volume_weight)
    if truncated:
        walls.warnings.append(
            f"chain truncated to {max_contracts} of {len(codes)} contracts"
        )
    return walls


def _batched(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # reject NaN


def _int(value: Any) -> int:
    """Coerce a loosely-typed SDK field to int; 'N/A' and NaN become 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    return 0 if f != f else int(f)
