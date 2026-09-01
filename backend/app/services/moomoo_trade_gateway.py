"""Read-only Moomoo trade context — positions and past fills, nothing else.

**This module opens a trade context but is read-only by construction.** It
exposes three data methods — `health()`, `list_positions()` and
`list_deals()` — and every one of them only reads. There is no `place_order`,
`modify_order`, `cancel_order`, or `unlock_trade` method here, and none may
ever be added: CLAUDE.md's first rule is that this project never places
orders, and the trade context is the one component that could. Note there is
deliberately no trading-password setting in `config.py` either — without an
unlock the SDK cannot submit an order even if code tried to.

Why a trade context exists at all, when `moomoo_gateway.py` says the project
is quote-side only: two questions that neither quote data nor anything stored
locally can answer. "What do I hold right now" (`list_positions`, for the
holdings badge), and "how did my past trades actually turn out"
(`list_deals`, which feeds rule #5's outcome sync — the RAG corpus is only
as good as the outcomes in it, and before this there was exactly one).

Everything here was verified against the live account rather than assumed;
the surprising parts are documented at each call site below.

The timeout, lock and pool machinery is shared with the quote gateway and
lives in `sdk_gateway` — read its docstring before changing how a call is
made here. Sharing it is deliberate: the failure this module must survive (a
wedged trade context showing an empty portfolio instead of an error) is the
same failure the quote side must survive, and two copies of that logic is two
places for it to rot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import moomoo as ft

from app.config import settings
from app.services.sdk_gateway import DEFAULT_TIMEOUT, SdkContextGateway, records

logger = logging.getLogger(__name__)

POSITIONS_CACHE_TTL = 45.0  # positions barely move for a manual-execution tool
# Moomoo rejects a wider window outright; longer ranges are split.
MAX_DEAL_WINDOW_DAYS = 360


class TradeGatewayError(RuntimeError):
    """Any failure talking to OpenD's trade context."""


class TradeGatewayTimeout(TradeGatewayError):
    """A call exceeded its timeout — OpenD is wedged or unreachable."""


@dataclass
class TradeGatewayStatus:
    """Trade-session health, shaped for the dashboard banner."""

    connected: bool
    acc_id: int | None = None
    trd_env: str = ""
    error: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "acc_id": self.acc_id,
            "trd_env": self.trd_env,
            "error": self.error,
            "checked_at": self.checked_at.isoformat(),
        }


class MoomooTradeGateway(SdkContextGateway):
    """Single reusable OpenSecTradeContext, timeout-guarded. Queries only.

    The lock, the worker pool, the timeout handling and the counters are all
    inherited from `SdkContextGateway`. What is here is trade-specific: the
    context type, the exceptions callers catch, account resolution, and the
    three read methods.

    **No order-side method exists on this class and none may be added.** The
    base class cannot introduce one either — it opens no context of its own
    and knows nothing about orders. See CLAUDE.md decisions #21.
    """

    _error_cls = TradeGatewayError
    _timeout_cls = TradeGatewayTimeout
    _thread_name_prefix = "moomoo-trd"
    _call_label = "Trade call"
    _context_label = "trade context"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(host=host, port=port, timeout=timeout)
        # Trade-specific state. `_acc_id` is seeded from config so a pinned
        # account skips resolution entirely; `_cache` backs list_positions.
        self._acc_id: int | None = settings.trd_acc_id
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    # --- connection lifecycle -------------------------------------------

    def _open_context(self) -> ft.OpenSecTradeContext:
        return ft.OpenSecTradeContext(
            filter_trdmarket=settings.trd_market,
            host=self.host,
            port=self.port,
            security_firm=settings.trd_security_firm,
        )

    # --- call plumbing ---------------------------------------------------

    def _call(self, name: str, method: str, *args,
              timeout: float | None = None, **kwargs):
        """Run one SDK call, resolving the method off the live context.

        The method is named rather than passed as a callable so the attribute
        lookup — and therefore the connection, if there is not one yet —
        happens INSIDE the lock, where a failure to connect is caught and
        wrapped as a TradeGatewayError like any other fault. The quote gateway
        binds beforehand instead; `SdkContextGateway._invoke` supports both,
        and its docstring is where the timeout, lock and pool-rebuild
        behaviour is documented.

        There is no paginated call on the trade side, so the page key
        `_invoke` returns is always None and is discarded here.

        Args:
            name:   SDK method name, used in logs and error messages.
            method: the attribute to look up on the trade context. Same string
                    as `name` at every call site today; kept separate because
                    the two are conceptually different (what to report vs what
                    to invoke).
            timeout: overrides the instance default for this call.

        Returns:
            The unwrapped `data` from the SDK's (ret, data) pair.

        Raises:
            TradeGatewayTimeout / TradeGatewayError, per `_invoke`.
        """
        data, _ = self._invoke(
            name, lambda: getattr(self._context(), method), *args, timeout=timeout, **kwargs
        )
        return data

    # --- account resolution ---------------------------------------------

    def _resolve_acc_id(self) -> int:
        """The REAL account's id, resolved once and remembered.

        Resolved explicitly rather than relying on `acc_id=0` (which picks
        whatever sits at index 0) because the account list contains a
        SIMULATE account alongside the real one, and silently querying the
        simulated account would report holdings the user does not have.
        """
        if self._acc_id is not None:
            return self._acc_id

        data = self._call("get_acc_list", "get_acc_list")
        rows = records(data)
        real = [r for r in rows if str(r.get("trd_env", "")).upper() == settings.trd_env.upper()]
        if not real:
            raise TradeGatewayError(
                f"no {settings.trd_env} account found for security_firm="
                f"{settings.trd_security_firm} (a wrong firm returns only the "
                f"SIMULATE account without erroring)"
            )
        self._acc_id = int(real[0]["acc_id"])
        logger.info("Resolved %s trade account %s", settings.trd_env, self._acc_id)
        return self._acc_id

    # --- health ----------------------------------------------------------

    def health(self) -> TradeGatewayStatus:
        """Check the trade session. Never raises — returns the failure."""
        try:
            acc_id = self._resolve_acc_id()
        except TradeGatewayError as exc:
            return TradeGatewayStatus(connected=False, error=str(exc))
        return TradeGatewayStatus(connected=True, acc_id=acc_id, trd_env=settings.trd_env)

    # --- positions (the only data this module reads) ----------------------

    def list_positions(self, market: str | None = None, use_cache: bool = True) -> list[dict[str, Any]]:
        """Current holdings, normalised for the dashboard.

        Filtering is done with `position_market`, NOT with the context's
        `filter_trdmarket`: the latter selects which accounts are listed and
        does not filter positions at all (a context built with
        filter_trdmarket='US' still returns AU holdings), and it rejects 'AU'
        outright with a misleading "the type of environment param is wrong".
        """
        now = time.monotonic()
        if use_cache and self._cache is not None:
            cached_at, rows = self._cache
            if now - cached_at < POSITIONS_CACHE_TTL:
                return _filter_market(rows, market)

        acc_id = self._resolve_acc_id()
        data = self._call(
            "position_list_query",
            "position_list_query",
            trd_env=settings.trd_env,
            acc_id=acc_id,
            timeout=max(self.timeout, 30.0),
        )
        rows = [_normalise_position(r) for r in records(data)]
        self._cache = (now, rows)
        return _filter_market(rows, market)


    # --- historical fills (read-only, like everything here) --------------

    def list_deals(self, start: date, end: date) -> list[dict[str, Any]]:
        """Executed fills between two dates. Reads history; changes nothing.

        Moomoo rejects any window wider than 360 days ("The interval between
        start and end must not exceed 360 days"), so longer ranges are split
        and concatenated here rather than surfacing that as a caller problem.

        These are **fills, not round trips**: one order can appear as several
        rows sharing an `order_id`, and there is no P&L column. Turning them
        into trades is `outcome_sync`'s job.
        """
        if start > end:
            raise ValueError("start must not be after end")

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=MAX_DEAL_WINDOW_DAYS - 1), end)
            data = self._call(
                "history_deal_list_query",
                "history_deal_list_query",
                start=window_start.isoformat(),
                end=window_end.isoformat(),
                trd_env=settings.trd_env,
                acc_id=self._resolve_acc_id(),
                timeout=max(self.timeout, 45.0),
            )
            for row in records(data):
                # Windows are half-open by date, but a fill on a boundary day
                # can appear in both; deal_id is the identity.
                deal_id = str(row.get("deal_id") or "")
                if deal_id and deal_id in seen:
                    continue
                if deal_id:
                    seen.add(deal_id)
                rows.append(_normalise_deal(row))
            window_start = window_end + timedelta(days=1)

        rows.sort(key=lambda r: r["time"] or "")
        logger.info("fetched %d historical fills between %s and %s",
                    len(rows), start, end)
        return rows


def _normalise_deal(row: dict[str, Any]) -> dict[str, Any]:
    """One SDK fill row -> the shape outcome_sync consumes."""
    return {
        "deal_id": str(row.get("deal_id") or ""),
        "order_id": str(row.get("order_id") or ""),
        "code": str(row.get("code") or ""),
        "name": row.get("stock_name") or "",
        "market": str(row.get("deal_market") or "").upper(),
        "side": str(row.get("trd_side") or "").upper(),   # BUY | SELL
        "qty": _f(row.get("qty")),
        "price": _f(row.get("price")),
        "time": str(row.get("create_time") or ""),
        "status": str(row.get("status") or ""),
    }


def _filter_market(rows: list[dict[str, Any]], market: str | None) -> list[dict[str, Any]]:
    if not market:
        return rows
    want = market.strip().upper()
    return [r for r in rows if r["market"] == want]


def _normalise_position(row: dict[str, Any]) -> dict[str, Any]:
    """One SDK position row -> the shape the frontend consumes.

    `pl_ratio_avg_cost` is the P/L percentage used, not `pl_ratio`. They
    differ once a position has realised profit: `pl_ratio` is computed off
    `diluted_cost`, which can go negative in that case (a real holding here
    shows diluted_cost -5711.58 against an average_cost of 42.77, and reports
    pl_ratio 0.00 while the position is genuinely up 9.27%). The avg-cost
    basis is the one that matches what the user sees in the Moomoo app.
    """
    code = str(row.get("code") or "")
    return {
        "code": code,
        "name": row.get("stock_name") or "",
        "market": str(row.get("position_market") or code.split(".", 1)[0]).upper(),
        "qty": _f(row.get("qty")),
        "avg_cost": _f(row.get("average_cost")),
        "market_value": _f(row.get("market_val")),
        "last_price": _f(row.get("nominal_price")),
        "unrealized_pnl_pct": _f(row.get("pl_ratio_avg_cost")),
        "unrealized_pnl": _f(row.get("unrealized_pl")),
        "currency": row.get("currency") or "",
        "position_side": row.get("position_side") or "",
    }


def _f(value: Any) -> float | None:
    """Cast to float, mapping NaN/None to None (JSON-safe)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


_trade_gateway: MoomooTradeGateway | None = None


def get_trade_gateway() -> MoomooTradeGateway:
    """Process-wide trade gateway singleton (one trade context per backend)."""
    global _trade_gateway
    if _trade_gateway is None:
        _trade_gateway = MoomooTradeGateway()
    return _trade_gateway
