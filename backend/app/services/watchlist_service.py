"""Watchlist sync: Moomoo account groups -> local SQLite cache.

CLAUDE.md rule #4: the watchlist is sourced from the user's Moomoo account,
all groups, system and custom. System groups are read-only for writes —
`db.add_ticker_to_group` / `remove_ticker_from_group` enforce that and raise
ValueError; nothing here tries to work around it.

Two live constraints shape this module:

- **`get_user_security` is rate limited to 10 calls per 30 seconds.** One
  call per group times 19 groups blows through it instantly, and the SDK
  reports the failure as an ordinary error string rather than a retryable
  exception, so a naive loop silently returns a half-empty watchlist. Group
  member fetches are therefore paced by `_RateLimiter`, and a group that
  still fails is reported rather than quietly skipped.

- **`watchlist_cache.market` CHECKs IN ('US','HK','AU').** A Moomoo account
  can hold SG/JP/SH/SZ tickers, and inserting one raises IntegrityError. The
  sync filters to supported markets and counts the rest, so an unsupported
  ticker appearing in a group cannot take down the whole sync.

Newly discovered tickers default to enabled=1. `enabled` is deliberately
never overwritten on re-sync — it is the user's own scanner on/off switch,
and clobbering it every cycle would silently re-enable things they turned
off.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.utils.rate_limiter import RateLimiter
from app.utils.market_hours import market_of

logger = logging.getLogger(__name__)

SUPPORTED_MARKETS = ("US", "HK", "AU")

# get_user_security: 10 calls / 30s. Leave headroom for the rest of the app.
_RATE_LIMIT_CALLS = 8
_RATE_LIMIT_WINDOW = 30.0

# The limiter itself moved to `app.utils.rate_limiter` when the sector work needed a
# third and fourth copy of it (decisions #63). The alias keeps this module's
# own call sites and its tests reading the same as before.
_RateLimiter = RateLimiter


@dataclass
class SyncResult:
    groups_synced: int = 0
    tickers_synced: int = 0
    groups_failed: list[str] = field(default_factory=list)
    unsupported_markets: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups_synced": self.groups_synced,
            "tickers_synced": self.tickers_synced,
            "groups_failed": self.groups_failed,
            "unsupported_markets": self.unsupported_markets,
        }


def sync_watchlist(gateway, limiter: RateLimiter | None = None) -> SyncResult:
    """Pull every Moomoo group and its members into the local cache.

    Returns a SyncResult rather than raising on partial failure: a scan cycle
    with 18 of 19 groups is far more useful than no scan at all, and the
    failures are surfaced for the dashboard banner.
    """
    limiter = limiter or RateLimiter(_RATE_LIMIT_CALLS, _RATE_LIMIT_WINDOW, "watchlist sync")
    result = SyncResult()

    groups = gateway.list_groups()
    logger.info("watchlist sync: %d groups from Moomoo", len(groups))

    for group in groups:
        name = group.get("group_name") or ""
        group_id = str(group.get("group_id") or name)
        is_system = bool(group.get("is_system"))
        db.upsert_group(group_id=group_id, group_name=name, is_system=is_system)

        limiter.acquire()
        try:
            members = gateway.list_group_members(name)
        except Exception as exc:
            logger.warning("group %r member fetch failed: %s", name, exc)
            result.groups_failed.append(name)
            continue

        codes: list[str] = []
        for member in members:
            code = member.get("code")
            if not code:
                continue
            market = market_of(code)
            if market not in SUPPORTED_MARKETS:
                result.unsupported_markets[market or "unknown"] = (
                    result.unsupported_markets.get(market or "unknown", 0) + 1
                )
                continue
            db.upsert_watchlist_ticker(
                code=code,
                name=member.get("name") or code,
                market=market,
                # Moomoo's own 'STOCK' / 'ETF' label. It was already on this
                # dict and was being dropped; `get_owner_plate` fails its
                # whole batch on one ETF, so this is what makes a sector
                # lookup over the watchlist possible at all.
                security_type=member.get("stock_type"),
            )
            codes.append(code)

        db.set_group_members(group_id, codes)
        result.groups_synced += 1
        result.tickers_synced += len(codes)

    if result.unsupported_markets:
        logger.info(
            "watchlist sync skipped unsupported markets: %s",
            result.unsupported_markets,
        )
    logger.info(
        "watchlist sync: %d groups, %d ticker memberships, %d groups failed",
        result.groups_synced, result.tickers_synced, len(result.groups_failed),
    )
    return result
