"""A concurrency cap on the one GPU everything shares.

There is one Ollama box and one GPU behind it. Nothing enforced that before
this: the scanner was the only caller and `_scan_lock` serialised it by
accident of being OpenD's mutex. Chat changes that — a browser tab can start
a generation at any moment, and three tabs asking at once means three queued
generations, every answer slower than if they had waited, and a real chance
of the Ollama LXC running out of memory on a 27B model.

Deliberately NOT `_scan_lock`. That lock is held for over an hour by a
pre-market scan (decisions #20), and a chat that waits an hour is not a chat.
The chat path also makes zero OpenD calls, so it has no claim on OpenD's
single-context mutex on its own merits — the two locks guard different
scarce things, and conflating them would make the wrong one the bottleneck.

CAPACITY is 2, which is "one background generation plus one human". The
earnings-outlook job takes at most one slot, so an interactive question can
always get the other. That is the sizing rationale; it is not a round number.

The scanner is deliberately left outside this semaphore. It is already
serialised by `_scan_lock`, and making an hour-long scan wait on a slot that
a chat might hold would be a new way to stall the thing the whole product is
built around.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

CAPACITY = 2

# Acquire timeouts, named rather than passed as bare floats at call sites so
# the asymmetry between them is legible: a person is waiting on one of these
# and a cron job is waiting on the other.
INTERACTIVE_TIMEOUT = 5.0     # chat: fail fast and say the model is busy
BACKGROUND_TIMEOUT = 900.0    # batch jobs: wait, they have nowhere to be

_slots = threading.BoundedSemaphore(CAPACITY)
_lock = threading.Lock()
_held: dict[int, tuple[str, float]] = {}   # token -> (label, acquired_at)
_next_token = 0


def acquire(label: str, timeout: float = INTERACTIVE_TIMEOUT) -> int | None:
    """Take a slot. Returns a token to release with, or None on timeout.

    The token exists so `stats()` can name what is holding the GPU. A stuck
    slot with no label is a mystery; one that says "chat US.PLTR, 400s" is a
    diagnosis.
    """
    global _next_token
    if not _slots.acquire(timeout=timeout):
        logger.warning("llm_slots: %s found all %d slots busy after %.0fs (%s)",
                       label, CAPACITY, timeout, _describe())
        return None
    with _lock:
        _next_token += 1
        token = _next_token
        _held[token] = (label, time.monotonic())
    return token


def release(token: int | None) -> None:
    """Idempotent for None, so a caller can release unconditionally."""
    if token is None:
        return
    with _lock:
        if _held.pop(token, None) is None:
            logger.warning("llm_slots: double release of token %s", token)
            return
    _slots.release()


def _describe() -> str:
    with _lock:
        now = time.monotonic()
        return ", ".join(f"{lbl} {now - at:.0f}s" for lbl, at in _held.values()) or "idle"


def stats() -> dict:
    """Reported on /health, so a leaked slot is visible rather than mysterious."""
    with _lock:
        now = time.monotonic()
        ages = [now - at for _, at in _held.values()]
        return {
            "active": len(_held),
            "capacity": CAPACITY,
            "oldest_held_seconds": round(max(ages), 1) if ages else None,
            "holders": [lbl for lbl, _ in _held.values()],
        }
