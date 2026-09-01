"""Shared-secret gate for the LAN-exposed API.

The backend binds 0.0.0.0 so the dashboard can be browsed from another
machine (decisions #19). CORS restricts *browsers* to known origins; it does
nothing about curl, a script, or anything else on the network. Without this,
`GET /positions` handed real holdings and P&L to anything on the LAN, and
`POST /outcomes` let anyone write P&L straight into the RAG corpus that
future advice is built from.

**What this is honestly worth.** The token is delivered to the browser as
part of the frontend bundle, so anyone who can load the dashboard can read
it. It stops casual access, scanners, and other devices on the network; it is
not authentication and it would not stop someone who has already loaded the
page. Treat it as a lock on a door, not a vault. The genuine fix for a
hostile network is a reverse proxy with real auth, or not exposing the port.

Deliberately unauthenticated: `/livez` and `/readyz`. The systemd watchdog
probes them as root with no config of its own, and an auth failure there
would read as a wedged backend and trigger restarts. They expose no market or
account data.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

_warned_disabled = False


def auth_enabled() -> bool:
    return bool(settings.api_token)


def require_api_key(x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)) -> None:
    """FastAPI dependency. No token configured means the gate is open.

    Unset is treated as "off" rather than "deny everything" on purpose: this
    runs on a homelab box where locking the owner out of their own dashboard
    over a missing .env line is a worse failure than staying as open as it
    was yesterday. It says so loudly in the log instead.
    """
    global _warned_disabled
    if not auth_enabled():
        if not _warned_disabled:
            logger.warning(
                "API_TOKEN is not set — every endpoint is reachable "
                "unauthenticated by anything on the LAN, including /positions"
            )
            _warned_disabled = True
        return

    # Constant-time: a plain == leaks the token's prefix through timing.
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {API_KEY_HEADER} header.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )


# --------------------------------------------------------------------------
# Session-or-header gate
#
# `require_api_key` above is retained unchanged for anything that still calls
# it directly, but every router now depends on `require_auth`, which accepts
# either credential:
#
#   * a valid session cookie — how the browser authenticates, via the
#     same-origin /api proxy on the Next.js server;
#   * a valid X-API-Key — how non-browser callers on localhost authenticate
#     (scripts/benchmark_models.py, curl, anything ad hoc).
#
# **The empty-token rule is inverted here, and that is the point.**
# `require_api_key` treats an unset API_TOKEN as "gate disabled", because
# locking the owner out of a LAN-only dashboard over a missing .env line was
# the worse failure. That trade-off does not survive the move to a public
# address: the dashboard now authenticates with a cookie and cannot be bricked
# by an absent token, so "open to everyone" has stopped being the safe default
# and has become simply an open door. An unset token here means the *header*
# path is unavailable — never that the request is waved through.
# --------------------------------------------------------------------------

def _header_ok(x_api_key: str | None) -> bool:
    """True only if a token is configured AND the header matches it."""
    if not settings.api_token or x_api_key is None:
        return False
    return hmac.compare_digest(x_api_key, settings.api_token)


def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> None:
    # Imported here rather than at module scope: auth_service imports app.db,
    # and app.db must not be pulled in as a side effect of importing this
    # module during the tests, which set db.DB_PATH before touching anything.
    from app.services import auth_service

    if auth_service.session_is_valid(request.cookies.get(auth_service.COOKIE_NAME)):
        return
    if _header_ok(x_api_key):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign in, or present a valid X-API-Key.",
    )
