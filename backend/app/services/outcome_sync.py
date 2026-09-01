"""Turn Moomoo fills into realised trade outcomes.

Moomoo's history is a list of **fills**, not trades: one order can arrive as
several rows sharing an `order_id`, and there is no P&L column anywhere. To
answer "how did that setup actually turn out" the fills have to be walked
chronologically per ticker and matched into round trips.

**Why weighted-average cost rather than FIFO.** Both are defensible, and they
disagree whenever a position was built in tranches. Average cost is chosen
because it is what the Moomoo app shows (`average_cost` on a position, which
`/positions` already surfaces), so a realised P&L computed here matches the
number the user can see next to the holding. A FIFO figure would be correct
by a different convention and look like a bug. This is a bookkeeping choice,
not a tax calculation — nothing here should be used for tax reporting.

What this deliberately does not do:
  * short positions — a SELL with no position on record is skipped and
    counted, not guessed at. Inferring an open short from a bare sell would
    invent a cost basis.
  * fees, commissions, FX — the fills carry none, so P&L is gross. Said
    plainly here rather than quietly implied to be net.
  * partial-close attribution across setups — a sell closes against the
    running average, and the whole round trip is attributed to one setup.

Everything in this module is pure given a list of fills, so the pairing can
be tested against hand-worked examples without touching OpenD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Fills whose qty or price cannot be read are unusable, not zero.
_UNUSABLE = object()


@dataclass
class _Lot:
    """A running position in one ticker."""

    qty: float = 0.0
    cost_total: float = 0.0          # qty * avg cost
    opened_at: str | None = None     # when the CURRENT holding period began

    @property
    def avg_cost(self) -> float:
        return self.cost_total / self.qty if self.qty else 0.0


@dataclass
class PairingResult:
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    skipped_sells_without_position: int = 0
    unusable_fills: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcomes": len(self.outcomes),
            "skipped_sells_without_position": self.skipped_sells_without_position,
            "unusable_fills": self.unusable_fills,
        }


def _parse_time(raw: str) -> datetime | None:
    """Moomoo returns local exchange time as 'YYYY-MM-DD HH:MM:SS.sss'."""
    if not raw:
        return None
    text = raw.strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def pair_fills(fills: Iterable[dict[str, Any]]) -> PairingResult:
    """Walk fills chronologically per ticker and emit one outcome per close.

    A SELL that reduces a position realises P&L against the running average
    cost. A SELL larger than the position closes what exists and the excess
    is skipped rather than treated as a short.
    """
    result = PairingResult()
    by_code: dict[str, _Lot] = {}

    ordered = sorted(
        (f for f in fills if str(f.get("status", "")).upper() in ("", "OK", "FILLED")),
        key=lambda f: f.get("time") or "",
    )

    for fill in ordered:
        code = fill.get("code") or ""
        qty = fill.get("qty")
        price = fill.get("price")
        side = str(fill.get("side") or "").upper()
        when = fill.get("time") or ""

        if not code or not side or qty is None or price is None or qty <= 0:
            result.unusable_fills += 1
            continue

        lot = by_code.setdefault(code, _Lot())

        if side == "BUY":
            # Adding to a flat position starts a new holding period.
            if lot.qty <= 0:
                lot.opened_at = when
                lot.qty = 0.0
                lot.cost_total = 0.0
            lot.qty += qty
            lot.cost_total += qty * price
            continue

        if side != "SELL":
            result.unusable_fills += 1
            continue

        if lot.qty <= 0:
            # No cost basis on record — most often a position opened before
            # the queried window. Guessing one would fabricate a P&L.
            result.skipped_sells_without_position += 1
            continue

        closed_qty = min(qty, lot.qty)
        avg_cost = lot.avg_cost
        pnl_abs = (price - avg_cost) * closed_qty
        pnl_pct = ((price - avg_cost) / avg_cost * 100.0) if avg_cost else None

        opened = _parse_time(lot.opened_at or "")
        closed = _parse_time(when)
        hold_hours = (
            (closed - opened).total_seconds() / 3600.0
            if opened and closed and closed >= opened
            else None
        )

        result.outcomes.append({
            "code": code,
            # The closing fill's id is the natural identity: it is unique per
            # close and lets a re-sync upsert rather than duplicate.
            "moomoo_deal_id": fill.get("deal_id"),
            "entry_price": round(avg_cost, 6),
            "exit_price": price,
            "pnl_abs": round(pnl_abs, 6),
            "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
            "hold_time_hours": round(hold_hours, 3) if hold_hours is not None else None,
            "exit_reason": "closed on Moomoo" if closed_qty >= lot.qty else "partial close",
            "opened_at": opened.isoformat() if opened else None,
            "closed_at": closed.isoformat() if closed else None,
            "qty": closed_qty,
        })

        lot.cost_total -= avg_cost * closed_qty
        lot.qty -= closed_qty
        if qty > closed_qty:
            # The remainder would be a short; not modelled.
            result.skipped_sells_without_position += 1
        if lot.qty <= 1e-9:
            lot.qty = 0.0
            lot.cost_total = 0.0
            lot.opened_at = None

    return result


def sync_outcomes(
    trade_gateway,
    db_module,
    days: int = 360,
    end: date | None = None,
) -> dict[str, Any]:
    """Fetch fills, pair them, and hand the round trips to the database.

    `db_module` is injected so the pairing can be exercised against a
    throwaway database without patching imports.
    """
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    fills = trade_gateway.list_deals(start, end)
    paired = pair_fills(fills)

    summary = db_module.sync_moomoo_outcomes(paired.outcomes)
    out = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "fills_fetched": len(fills),
        **paired.to_dict(),
        **summary,
    }
    logger.info("outcome sync: %s", out)
    return out
