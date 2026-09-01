"""Thin wrapper around moomoo-api / OpenD.

Everything that touches live market data goes through here. Two things
this module is responsible for beyond plain data access:

1. **Never hang the scan loop.** The moomoo SDK's calls are synchronous
   with no timeout parameter, and they *do* block indefinitely in practice
   (observed against this LXC's OpenD when the market is closed). The 60s
   scanner cannot afford an unbounded call, so every request runs in a
   worker thread with a hard timeout. That machinery — the bounded lock, the
   one-worker pool and its rebuild-on-timeout — lives in `sdk_gateway`, which
   the trade gateway sits on too; read its docstring before changing anything
   about how a call is made here.

2. **Surface a dead session loudly.** Per CLAUDE.md, OpenD's session can
   expire independently of the backend. A failed call marks the connection
   suspect and forces a reconnect on next use; `health()` reports the state
   for the dashboard banner rather than silently returning empty data.

This module is quote-side only. It does not and must not place, modify, or
cancel orders — the project is advisory-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import moomoo as ft
import pandas as pd

from app.services.sdk_gateway import SdkContextGateway, records

logger = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    """Any failure talking to OpenD."""


class GatewayTimeout(GatewayError):
    """A call exceeded its timeout — OpenD is wedged or unreachable."""


@dataclass
class GatewayStatus:
    """Connection health, shaped for the dashboard banner."""

    connected: bool
    trd_logined: bool = False
    qot_logined: bool = False
    program_status: str = ""
    market_states: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def healthy(self) -> bool:
        return self.connected and self.qot_logined

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "healthy": self.healthy,
            "trd_logined": self.trd_logined,
            "qot_logined": self.qot_logined,
            "program_status": self.program_status,
            "market_states": self.market_states,
            "error": self.error,
            "checked_at": self.checked_at.isoformat(),
        }


class MoomooGateway(SdkContextGateway):
    """Single reusable OpenQuoteContext with timeouts and reconnect.

    The lock, the worker pool, the timeout handling and the counters are all
    inherited from `SdkContextGateway`. What is here is what is quote-specific:
    the context type, the exceptions callers catch, and the data methods.
    """

    _error_cls = GatewayError
    _timeout_cls = GatewayTimeout
    _thread_name_prefix = "moomoo"
    _call_label = "OpenD call"
    _context_label = "quote context"

    # --- connection lifecycle -------------------------------------------

    def _open_context(self) -> ft.OpenQuoteContext:
        return ft.OpenQuoteContext(host=self.host, port=self.port)

    # --- call plumbing ---------------------------------------------------

    def _call(self, name: str, fn: Callable[..., Any], *args,
              timeout: float | None = None, with_page_key: bool = False, **kwargs):
        """Run one SDK call, taking an already-bound function.

        Quote-side callers resolve the context before calling (see
        `_quote_call` and `get_history_kline`), so the function handed over is
        already bound and the closure below is trivial. The trade gateway
        resolves inside the lock instead; `SdkContextGateway._invoke` supports
        both, and its docstring is where the timeout, lock and pool-rebuild
        behaviour is documented.

        Args:
            name: SDK method name, used in logs and error messages.
            fn:   the bound SDK callable to run in the worker thread.
            timeout: overrides the instance default for this call.
            with_page_key: return `(data, page_req_key)` instead of just data.
                Only `request_history_kline` paginates, and ignoring its key
                silently returns the OLDEST page — see `get_history_kline`.

        Returns:
            The unwrapped `data`, or `(data, page_req_key)` when asked.

        Raises:
            GatewayTimeout / GatewayError, per `_invoke`.
        """
        data, page_key = self._invoke(
            name, lambda: fn, *args, timeout=timeout, **kwargs
        )
        return (data, page_key) if with_page_key else data

    def _quote_call(self, name: str, method: str, *args, timeout: float | None = None, **kwargs):
        ctx = self._context()
        return self._call(name, getattr(ctx, method), *args, timeout=timeout, **kwargs)

    # --- health ----------------------------------------------------------

    def health(self) -> GatewayStatus:
        """Check the live session. Never raises — returns the failure."""
        try:
            state = self._quote_call("get_global_state", "get_global_state", timeout=10.0)
        except GatewayError as exc:
            return GatewayStatus(connected=False, error=str(exc))

        markets = {
            k.removeprefix("market_"): v
            for k, v in state.items()
            if k.startswith("market_") and isinstance(v, str)
        }
        return GatewayStatus(
            connected=True,
            trd_logined=bool(state.get("trd_logined")),
            qot_logined=bool(state.get("qot_logined")),
            program_status=str(state.get("program_status_type", "")),
            market_states=markets,
        )

    # --- watchlist (rule #4: system groups are read-only) -----------------

    def list_groups(self) -> list[dict[str, Any]]:
        """All Moomoo watchlist groups, system + custom."""
        df = self._quote_call(
            "get_user_security_group",
            "get_user_security_group",
            group_type=ft.UserSecurityGroupType.ALL,
        )
        return [
            {
                "group_name": row["group_name"],
                "group_type": row["group_type"],
                "is_system": str(row["group_type"]).upper() == "SYSTEM",
            }
            for row in records(df)
        ]

    def list_group_members(self, group_name: str) -> list[dict[str, Any]]:
        """Tickers in one watchlist group."""
        df = self._quote_call("get_user_security", "get_user_security", group_name)
        return [
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "lot_size": row.get("lot_size"),
                "stock_type": row.get("stock_type"),
                "listing_date": row.get("listing_date"),
                "delisting": row.get("delisting"),
            }
            for row in records(df)
            if row.get("code")
        ]

    # --- quotes ----------------------------------------------------------

    def get_snapshot(self, codes: list[str]) -> list[dict[str, Any]]:
        """Point-in-time snapshot for up to 400 codes.

        Slow relative to other calls (it round-trips to Moomoo's servers),
        so it gets a longer timeout than the default.

        **PLATE codes are valid here** (measured 2026-08-30): a plate such
        as "US.LIST2015" returns a full row carrying `turnover` (sector
        dollar volume), `volume`, and `plate_valid` / `plate_raise_count` /
        `plate_fall_count` / `plate_equal_count` — i.e. sector breadth,
        which klines do not carry. All 262 US plates fit in one call.
        """
        if not codes:
            return []
        df = self._quote_call(
            "get_market_snapshot", "get_market_snapshot", codes, timeout=max(self.timeout, 45.0)
        )
        return records(df)

    def get_history_kline(
        self,
        code: str,
        start: str | None = None,
        end: str | None = None,
        ktype: str = ft.KLType.K_DAY,
        max_count: int = 250,
        max_pages: int = 20,
    ) -> pd.DataFrame:
        """Historical OHLCV bars — the input to every deterministic indicator.

        **PLATE codes are valid here too** (measured 2026-08-30), which is
        what makes sector rotation computable over rolling windows on day
        one rather than after months of accumulation. A plate bar carries
        close/volume/turnover/change_rate normally; `pe_ratio` and
        `turnover_rate` come back 0.0 and mean nothing for a plate.

        **This call is paginated and the pagination is not optional.**
        request_history_kline returns at most `max_count` bars per page and
        hands back a `page_req_key` for the next one. Ignoring that key does
        not raise and does not warn — it silently returns the OLDEST page and
        drops everything after it. A 400-day request came back as 250 bars
        ending five weeks early, which fed stale prices straight into the
        indicators and produced a thesis with stop/target ~35% off the live
        price. Every page is fetched and concatenated here.

        NOTE: history K-line requests draw on a Moomoo per-account quota, and
        pagination multiplies the cost. Callers should cache rather than
        re-request per scan cycle.
        """
        frames: list[pd.DataFrame] = []
        page_key = None
        for page in range(max_pages):
            data, page_key = self._call(
                "request_history_kline",
                getattr(self._context(), "request_history_kline"),
                code,
                start=start,
                end=end,
                ktype=ktype,
                autype=ft.AuType.QFQ,
                max_count=max_count,
                page_req_key=page_key,
                timeout=max(self.timeout, 45.0),
                with_page_key=True,
            )
            if isinstance(data, pd.DataFrame) and not data.empty:
                frames.append(data)
            if not page_key:
                break
        else:
            logger.warning(
                "%s: stopped paginating klines at %d pages; history may be "
                "truncated", code, max_pages,
            )

        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        # Pages can overlap at the seam on some builds; de-duplicate and
        # re-sort so indicators always see a clean chronological series.
        if "time_key" in out.columns:
            out = out.drop_duplicates(subset="time_key").sort_values("time_key")
        return out.reset_index(drop=True)

    # --- options ---------------------------------------------------------

    def get_option_expirations(self, code: str) -> list[str]:
        df = self._quote_call("get_option_expiration_date", "get_option_expiration_date", code)
        return [row["strike_time"] for row in records(df) if row.get("strike_time")]

    # --- earnings, research and news search ------------------------------
    #
    # Still quote-side and still read-only. These are the "what is coming up
    # and what is being said" half of the same market-data session; nothing
    # here can place, modify or cancel anything (decisions #21 applies to the
    # trade context, and this module has never had one).

    EARNINGS_MAX_WINDOW_DAYS = 7
    EARNINGS_MARKETS = ("US", "HK")

    def get_earnings_calendar(
        self, market: str, begin_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """WHOLE-MARKET earnings rows for one market over a <=7-day window.

        Two traps, both confirmed against the live server:

        1. `begin_date`/`end_date` MUST be keywords. The SDK signature is
           (market, sort_type, begin_date, end_date, filter_list), so a
           second positional argument silently lands in `sort_type` and the
           dates are never applied.
        2. **The SDK docstring claims AU is supported. It is not.** The live
           server answers "Invalid market type, supported:
           HK/US/CNSH/CNSZ/SG/JP". Trust the probe, not the docstring —
           `EARNINGS_MARKETS` is what this account can actually ask for.

        The server also rejects a window wider than 7 days — verified, it
        answers "Date range must not exceed 7 days" — so a fortnight is two
        calls. Callers split it; this does not, so the limit stays visible
        rather than being papered over here.

        `security` comes back in Moomoo's own "US.PDD" form, i.e. the same
        shape as `watchlist_cache.code`. Confirmed live, because the sibling
        `get_search_news` documents `related_securities` the other way round
        ("LITE.US") and guessing wrong yields zero matches and an empty page
        rather than an error.
        """
        df = self._quote_call(
            "get_earnings_calendar",
            "get_earnings_calendar",
            market,
            begin_date=begin_date,
            end_date=end_date,
            timeout=max(self.timeout, 45.0),
        )
        return records(df)

    def search_news(
        self, keyword: str, max_count: int = 10, sub_type: Any = None
    ) -> list[dict[str, Any]]:
        """Moomoo's own news search — this project's substitute for a web search.

        Callers must handle BOTH empty shapes. Probed live, a nonsense
        keyword returned an empty list cleanly — but the SDK also carries an
        `else: return RET_ERROR, "empty data"` branch, and `_call` raises on
        any non-RET_OK, so a quiet ticker can surface either as `[]` or as a
        GatewayError depending on which path the server takes. Treat both as
        "no results" rather than letting one of them take down whatever was
        being built around it.

        `publish_time` comes back as "8/24" — no year, no timezone, and
        ambiguous between M/D and D/M. Never parse it, never sort on it, and
        never let it reach `news_articles.published_at`, whose NOT NULL and
        exact-shape invariant (decisions #43) exists to stop precisely this.
        """
        df = self._quote_call(
            "get_search_news",
            "get_search_news",
            keyword,
            max_count,
            sub_type if sub_type is not None else ft.NewsSubType.ALL,
            timeout=max(self.timeout, 30.0),
        )
        return records(df)

    def get_analyst_consensus(self, code: str) -> dict[str, Any]:
        """Analyst target and rating summary for one code.

        Returns a bare DICT, not a DataFrame — `records()` would flatten it
        to [] and the caller would read "no coverage" for a name with 32
        analysts on it.
        """
        data = self._quote_call(
            "get_research_analyst_consensus", "get_research_analyst_consensus", code
        )
        return dict(data) if isinstance(data, dict) else {}

    # NOT wrapped, deliberately: get_economic_calendar returns a 4-tuple
    # (ret, data, next_page, has_more), and `_call` reads result[0..2] — so it
    # would silently drop `has_more` and return only the first page. That is
    # the same shape as the kline-pagination bug that fed a five-week-old
    # close into every indicator. If it is ever needed, `_call` has to learn
    # about it first.

    def get_option_chain(self, code: str, expiry: str) -> list[dict[str, Any]]:
        """Option chain for one expiry — raw input for call/put wall calc."""
        df = self._quote_call(
            "get_option_chain",
            "get_option_chain",
            code,
            start=expiry,
            end=expiry,
            timeout=max(self.timeout, 45.0),
        )
        return records(df)


    # --- sectors (plates) and capital flow --------------------------------
    #
    # Still quote-side and still read-only. Every limit and failure mode
    # recorded below was measured against the live account on 2026-08-30,
    # not read from a docstring — see the decisions log.

    #: `get_owner_plate` is rate limited to 10 calls per 30 seconds. The
    #: server says so itself ("Maximum 10 times per 30 seconds") and reports
    #: it as an ordinary error string, exactly like `get_user_security`.
    OWNER_PLATE_MAX_CALLS = 10
    #: `get_capital_flow` is rate limited to 30 calls per 30 seconds, also
    #: stated by the server and also as a plain error string. A naive loop
    #: over 39 codes returned 30 rows and 9 silent failures.
    CAPITAL_FLOW_MAX_CALLS = 30
    #: `request_history_kline` is rate limited to 60 calls per 30 seconds.
    #: Measured 2026-08-30 the hard way: an unpaced pass over 262 plates
    #: succeeded 60 times and failed 202, each failure a `ret != RET_OK` that
    #: drops the OpenD context — so the gateway reconnected on every call for
    #: two minutes. This is NOT the per-account daily kline quota CLAUDE.md
    #: warns about; it is a burst limit, and it is the one a bulk caller hits.
    #: The scanner never sees it (48 tickers spread over 20-60 minutes behind
    #: `market_data`'s cache); anything fetching bars in a BURST must pace.
    HISTORY_KLINE_MAX_CALLS = 60
    RATE_WINDOW_SECONDS = 30.0

    def get_plate_list(self, market: str, plate_class: Any) -> list[dict[str, Any]]:
        """Sub-plates of one market: [code, plate_name, plate_id].

        `plate_class` is a `ft.Plate` member. Measured 2026-08-30 for US:
        INDUSTRY 145 plates, CONCEPT 117. `Plate.ALL` also returns REGION
        and OTHER, and OTHER is where Moomoo files its own broker product
        lists ('FUTU-CA 美股定投') next to real themes — which is why
        `sector_universe` builds its universe from INDUSTRY and CONCEPT
        only and treats membership in that enumeration as definitional.
        """
        df = self._quote_call("get_plate_list", "get_plate_list", market, plate_class)
        return records(df)

    def get_plate_stock(self, plate_code: str) -> list[dict[str, Any]]:
        """Constituents of one plate, e.g. "US.LIST2015" -> 72 rows.

        Carries no prices — [code, lot_size, stock_name, stock_owner,
        stock_child_type, stock_type, list_time, ...]. `stock_type` is the
        same vocabulary as `list_group_members` ('STOCK' / 'ETF'), which is
        what lets a caller keep ETFs out of `get_owner_plate` batches.
        """
        df = self._quote_call("get_plate_stock", "get_plate_stock", plate_code)
        return records(df)

    def get_owner_plate(self, codes: list[str]) -> list[dict[str, Any]]:
        """Which plates each code belongs to: [code, name, plate_code,
        plate_name, plate_type].

        **This call fails the WHOLE batch if any code is an ETF**, with
        "Get Stock's Sector interface does not support ETFs type." — the
        same shape as `get_market_snapshot`'s unentitled-market trap, and it
        fails on the batch's CONTENT, not its size: measured 2026-08-30, the
        first 35 enabled watchlist tickers succeeded and 40 failed, because
        US.SMH sits at index ~35. Chunking alone therefore does NOT fix it;
        it only limits the blast radius. Filter ETFs out by
        `watchlist_cache.security_type` first — `list_group_members` already
        reports `stock_type` for exactly this.

        Also rate limited to 10 calls / 30s (`OWNER_PLATE_MAX_CALLS`), so a
        per-code fallback must be paced or it returns partial data with no
        exception raised.

        `plate_type` is one of INDUSTRY / CONCEPT / OTHER. Do not filter on
        it — see `get_plate_list`.
        """
        if not codes:
            return []
        df = self._quote_call(
            "get_owner_plate", "get_owner_plate", list(codes), timeout=max(self.timeout, 30.0)
        )
        return records(df)

    def get_capital_flow(
        self,
        code: str,
        period_type: Any = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Net capital flow for ONE stock or ETF, split by order size.

        Columns: last_valid_time, in_flow, super_in_flow, big_in_flow,
        mid_in_flow, sml_in_flow, main_in_flow, capital_flow_item_time.

        Verified arithmetic against the live server (US.NVDA, 2026-08-30):
            in_flow   == super + big + mid + sml
            main_in_flow == super + big
        so `main_in_flow` is net BLOCK-SIZED ORDER FLOW. It is not reported
        block trades and it is not 13F — describe it as what it is.

        **Plate codes are rejected**: "Only stocks, warrants, and funds are
        supported; other security types are not supported." Sector-level
        signed flow therefore has to come from sector ETFs (funds ARE
        supported), which is what `sector_etfs` is for.

        `PeriodType.INTRADAY` returns one-minute rows and leaves
        `main_in_flow` as the literal string 'N/A' — the institutional split
        is the only reason to want this call, so DAY is the default here
        even though the SDK defaults to INTRADAY. Non-INTRADAY windows are
        capped at 365 days by the SDK.

        Rate limited to 30 calls / 30s (`CAPITAL_FLOW_MAX_CALLS`).
        """
        if period_type is None:
            period_type = ft.PeriodType.DAY
        df = self._quote_call(
            "get_capital_flow",
            "get_capital_flow",
            code,
            period_type=period_type,
            start=start,
            end=end,
            timeout=max(self.timeout, 30.0),
        )
        return records(df)


_gateway: MoomooGateway | None = None


def get_gateway() -> MoomooGateway:
    """Process-wide gateway singleton (one OpenD context per backend)."""
    global _gateway
    if _gateway is None:
        _gateway = MoomooGateway()
    return _gateway
