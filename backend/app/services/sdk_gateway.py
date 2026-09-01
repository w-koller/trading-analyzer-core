"""The call plumbing both Moomoo gateways sit on.

There are two gateways — `moomoo_gateway` for quotes and
`moomoo_trade_gateway` for read-only holdings and fills — and they had grown
byte-identical copies of the machinery that makes an SDK call survivable. This
module owns that machinery once. What stays in each gateway is what genuinely
differs: which context it opens, which exceptions it raises, and which data
methods it exposes.

**The reason any of this exists** is that the moomoo SDK's calls are
synchronous, take no timeout parameter, and do block indefinitely in practice
— `get_market_snapshot` has been observed hanging past 180 seconds against
this LXC's OpenD. A scan cycle cannot afford an unbounded call, so every
request runs in a worker thread behind a hard timeout.

Three details below are load-bearing and each cost a real debugging session.
They are commented where they happen, but in summary:

  1. The pool is REBUILT on timeout. The abandoned worker is still blocked
     inside the SDK call, and the pool has exactly one worker, so reusing it
     queues every later call behind the orphan — one transient hang bricks all
     access until the process restarts.
  2. Lock acquisition is BOUNDED. An unbounded `with self._lock` makes the
     second caller wait on the first with no ceiling at all.
  3. `stats()` never acquires the lock and never does IO, because a stats call
     that blocks on the lock cannot diagnose a stuck lock — which is the one
     thing it is for.

This module opens no context of its own and knows nothing about orders. The
advisory-only rule lives with the subclasses; nothing here can weaken it.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

import moomoo as ft
import pandas as pd

from app.config import settings
# Re-exported: RateLimiter moved to app.utils.rate_limiter so that callers on
# a box without the moomoo SDK can still use it. Existing importers of
# `sdk_gateway.RateLimiter` keep working unchanged.
from app.utils.rate_limiter import RateLimiter  # noqa: F401

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0

# How much longer than the call itself a caller will wait for the lock. The
# lock protects a single-threaded SDK context, so the realistic worst case is
# "one call ahead of me, running to its own timeout" plus a little slack.
LOCK_TIMEOUT_MARGIN = 5.0


def records(df: Any) -> list[dict[str, Any]]:
    """Normalise an SDK result to plain dicts.

    Args:
        df: whatever the SDK returned — usually a pandas DataFrame, sometimes
            already a list, occasionally something else entirely.

    Returns:
        A list of row dicts; an empty list for anything unrecognised.

    Note the "anything unrecognised" case is not always harmless: a call that
    returns a bare DICT (get_research_analyst_consensus does) would be
    flattened to [] here, so a name with 32 analysts covering it would read as
    no coverage. Such calls must not go through this function.
    """
    if isinstance(df, pd.DataFrame):
        return df.to_dict("records")
    if isinstance(df, list):
        return df
    return []



class SdkContextGateway:
    """A single reusable SDK context, serialised and timeout-guarded.

    Subclasses supply five class attributes and one method:

        _error_cls / _timeout_cls   the exception pair to raise, so a caller can
                                  keep catching the gateway-specific type it
                                  always caught
        _thread_name_prefix        names the worker thread, so a stack dump says
                                  which gateway is stuck
        _call_label / _context_label  wording for log lines and error messages
        _open_context()           builds the actual SDK context

    Everything else — the lock, the pool, the counters, the timeout handling
    and the reconnect-on-failure policy — is inherited and must not be
    reimplemented.
    """

    _error_cls: type[Exception] = RuntimeError
    _timeout_cls: type[Exception] = TimeoutError
    _thread_name_prefix: str = "moomoo"
    _call_label: str = "OpenD call"
    _context_label: str = "context"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        Args:
            host / port: where OpenD is listening. Default to the configured
                addresses, which are loopback — OpenD is never LAN-exposed.
            timeout: per-call ceiling in seconds. Individual calls may raise
                it (a snapshot round-trips to Moomoo's servers); none may
                remove it.
        """
        self.host = host or settings.opend_host
        self.port = port or settings.opend_port
        self.timeout = timeout
        self._ctx: Any = None
        # The SDK context is not safe for concurrent calls; serialise them.
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=self._thread_name_prefix
        )
        # Observability for the watchdog. Plain attributes, read without the
        # lock on purpose — see the module docstring, point 3.
        self._lock_held_since: float | None = None
        self._waiters: int = 0
        self._orphaned_threads: int = 0

    # --- connection lifecycle -------------------------------------------

    def _open_context(self) -> Any:
        """Build the SDK context. Implemented by each gateway."""
        raise NotImplementedError

    def _context(self) -> Any:
        """The live context, connecting on first use."""
        if self._ctx is None:
            logger.info("Connecting %s to OpenD at %s:%s",
                        self._context_label, self.host, self.port)
            self._ctx = self._open_context()
        return self._ctx

    def _drop_context(self) -> None:
        """Discard a context we no longer trust so the next call reconnects."""
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                logger.debug("Error closing stale %s", self._context_label,
                             exc_info=True)

    def close(self) -> None:
        """Drop the context and stop the worker. Safe to call more than once."""
        with self._lock:
            self._drop_context()
        self._pool.shutdown(wait=False)

    def _reset_pool(self) -> None:
        """Replace the executor whose only worker is stuck on a dead call."""
        old, self._pool = self._pool, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=self._thread_name_prefix
        )
        # wait=False: the point is that the worker is NOT coming back.
        old.shutdown(wait=False)

    # --- observability ----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Lock/pool counters. Never acquires the lock; never does IO.

        Returns:
            lock_held_seconds  how long the current holder has had it, 0.0 if free
            waiters            callers currently blocked trying to acquire it
            orphaned_threads   workers abandoned mid-call over this process's life
            connected          whether a context object currently exists
        """
        held_since = self._lock_held_since
        return {
            "lock_held_seconds": round(time.monotonic() - held_since, 1) if held_since else 0.0,
            "waiters": self._waiters,
            "orphaned_threads": self._orphaned_threads,
            "connected": self._ctx is not None,
        }

    # --- call plumbing ---------------------------------------------------

    def _invoke(
        self,
        name: str,
        resolve: Callable[[], Callable[..., Any]],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> tuple[Any, Any]:
        """Run one SDK call with a hard timeout, unwrapping (ret, data).

        Args:
            name:    the SDK method name, for logs and error messages.
            resolve: a zero-argument callable returning the function to submit.
                     A callable rather than the function itself because the
                     trade gateway resolves its method off the live context and
                     wants that resolution to happen INSIDE the lock, where a
                     connection failure is caught and wrapped like any other.
                     The quote gateway binds its function beforehand and passes
                     a trivial closure.
            timeout: overrides the instance default for this one call.
            *args / **kwargs: forwarded to the SDK call untouched.

        Returns:
            `(data, page_req_key)`. Most SDK calls return `(ret, data)` and the
            page key is None; `request_history_kline` alone returns a third
            element, and dropping it silently returns the OLDEST page and
            discards the rest — which once fed a five-week-old close into every
            indicator. Callers that do not paginate ignore the second element.

        Raises:
            timeout_cls: the call, or the wait for the lock, exceeded its bound.
            error_cls:   the call raised, or the SDK reported a non-RET_OK.
        """
        timeout = timeout or self.timeout
        page_key = None

        # Bounded, not `with self._lock` — see the module docstring, point 2.
        lock_timeout = timeout + LOCK_TIMEOUT_MARGIN
        self._waiters += 1
        try:
            acquired = self._lock.acquire(timeout=lock_timeout)
        finally:
            self._waiters -= 1
        if not acquired:
            raise self._timeout_cls(
                f"{name} waited {lock_timeout:.0f}s for the {self._context_label} lock "
                f"(held {self.stats()['lock_held_seconds']:.0f}s)"
            )

        self._lock_held_since = time.monotonic()
        try:
            fn = resolve()
            future = self._pool.submit(fn, *args, **kwargs)
            try:
                result = future.result(timeout=timeout)
                ret, data = result[0], result[1]
                if len(result) > 2:
                    page_key = result[2]
            except FutureTimeout:
                # The worker is abandoned — the SDK gives us no way to cancel
                # it — so the context is dropped AND the pool rebuilt. See the
                # module docstring, point 1: skipping the rebuild is what turns
                # one transient hang into a permanently wedged gateway.
                self._orphaned_threads += 1
                logger.error(
                    "%s %s timed out after %.1fs; abandoning worker and "
                    "rebuilding pool (%d orphaned so far)",
                    self._call_label, name, timeout, self._orphaned_threads,
                )
                self._drop_context()
                self._reset_pool()
                raise self._timeout_cls(
                    f"{name} timed out after {timeout:.0f}s") from None
            except Exception as exc:
                logger.error("%s %s raised: %s", self._call_label, name, exc)
                self._drop_context()
                raise self._error_cls(f"{name} failed: {exc}") from exc
        finally:
            self._lock_held_since = None
            self._lock.release()

        if ret != ft.RET_OK:
            # A protocol-level error usually means the session died.
            self._drop_context()
            raise self._error_cls(f"{name} returned error: {data}")
        return data, page_key
