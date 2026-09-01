"""A sliding-window call limiter, with no SDK dependency of any kind.

Lifted out of `sdk_gateway` on 2026-09-01. It had lived there since decisions
#63 consolidated the copies that `watchlist_service` and the sector modules
each carried, and it was correct there right up until a caller appeared on a
machine that cannot import the moomoo SDK at all.

`sdk_gateway` imports `moomoo` at module scope, so importing RateLimiter from
it required the SDK to be installed — which the Informed Trader cloud box
deliberately does not do. Nothing about counting calls in a time window is
Moomoo-specific, so the limiter moved here and `sdk_gateway` re-exports it.
Every existing call site is unchanged.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window call limiter; sleeps only when the window is full.

    Several OpenD quote calls are rate limited and the SDK reports a refusal
    as an ordinary error STRING rather than raising — so an unpaced loop
    returns partial data with nothing in any log and no exception to catch.
    Measured against the live server on 2026-08-30:

        get_user_security   10 / 30s
        get_owner_plate     10 / 30s
        get_plate_stock     10 / 30s
        get_capital_flow    30 / 30s

    Callers pace BELOW the documented ceiling so the rest of the app can
    still make a call while a batch job is running.

    Lifted here from `watchlist_service`, which had the only copy, when the
    sector work needed a third and fourth: this is the same decision written
    once, not several decisions that resemble each other (decisions #63).
    """

    def __init__(self, calls: int, window: float = 30.0, label: str = "sdk"):
        self.calls = calls
        self.window = window
        self.label = label
        self._times: list[float] = []

    def acquire(self) -> None:
        now = time.monotonic()
        self._times = [t for t in self._times if now - t < self.window]
        if len(self._times) >= self.calls:
            wait = self.window - (now - self._times[0]) + 0.1
            logger.info("%s throttled, sleeping %.1fs", self.label, wait)
            time.sleep(wait)
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < self.window]
        self._times.append(now)

