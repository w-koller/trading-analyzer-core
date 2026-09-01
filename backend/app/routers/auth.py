"""Login, logout, and "am I signed in".

Mounted WITHOUT the global auth dependency — a login endpoint that requires
being logged in is not useful. Everything here is therefore reachable by
anyone who can reach the app, which is why the lockout in `auth_service` is
not optional decoration.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)
    # Six digits, but not validated as such here: a length check would make a
    # malformed code a 422 and a wrong code a 401, which tells an attacker
    # which of the two they got wrong. Everything wrong is one 401.
    totp: str = ""


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=auth_service.COOKIE_NAME,
        value=token,
        max_age=int(settings.auth_session_days * 86400),
        httponly=True,                      # JS cannot read it, so XSS cannot steal it
        secure=settings.auth_cookie_secure,  # HTTPS only
        # Lax, not Strict: tapping a push notification is a cross-site
        # navigation, and under Strict the cookie would not be sent, so the
        # user would land on the login page every time. Lax still blocks the
        # cross-site POSTs that matter.
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(payload: LoginRequest, request: Request,
                response: Response) -> dict[str, Any]:
    """One generic failure for every cause. Never reveal which factor was wrong."""
    ip, distinct = auth_service.resolve_client_ip(request)
    if not distinct:
        # See resolve_client_ip: this means X-Forwarded-For did not survive the
        # proxy chain, so per-IP buckets would silently merge into one. Fall
        # back to a single global bucket rather than pretending to rate-limit.
        logger.warning(
            "login: client IP resolved to %r — X-Forwarded-For is not reaching "
            "the backend, so rate limiting is GLOBAL, not per-IP. Check the "
            "NPM -> Next -> uvicorn header chain.", ip or "(none)")
        ip = "__global__"

    if not auth_service.credentials_configured():
        # Fails closed, unlike the API_TOKEN rule. An unconfigured login
        # endpoint on a public address must not admit anyone.
        logger.error("login attempted but AUTH_PASSWORD_HASH / AUTH_TOTP_SECRET "
                     "are not set — run scripts/set_password.py")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Login is not configured on this server.")

    if await run_in_threadpool(auth_service.lockout_remaining, ip):
        logger.warning("login: %s is locked out", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.")

    # scrypt is deliberately slow, so it does not belong on the event loop.
    ok = await run_in_threadpool(
        auth_service.authenticate, payload.password, payload.totp)

    if not ok:
        await run_in_threadpool(auth_service.note_failure, ip)
        logger.warning("login: failed attempt from %s", ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid credentials.")

    await run_in_threadpool(auth_service.note_success, ip)
    token, expires = await run_in_threadpool(
        auth_service.start_session, request.headers.get("user-agent"), ip)
    _set_cookie(response, token)
    logger.info("login: new session from %s, expires %s", ip, expires)
    return {"ok": True, "expires_at": expires}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(auth_service.COOKIE_NAME)
    ended = await run_in_threadpool(auth_service.end_session, token)
    response.delete_cookie(auth_service.COOKIE_NAME, path="/")
    return {"ok": True, "ended": ended}


@router.post("/logout-all")
async def logout_all(response: Response) -> dict[str, Any]:
    """Kill every session everywhere — the "I lost my phone" button.

    This is why sessions are rows in SQLite rather than a signed stateless
    cookie: a signed cookie cannot be revoked before it expires.
    """
    n = await run_in_threadpool(auth_service.end_all_sessions)
    response.delete_cookie(auth_service.COOKIE_NAME, path="/")
    logger.info("logout-all: ended %d session(s)", n)
    return {"ok": True, "ended": n}


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """Cheap "is this cookie still good", for the frontend's redirect logic.

    Always 200 — the frontend needs to distinguish "signed out" from
    "backend unreachable", and a 401 here would be indistinguishable from the
    transport failure it is trying to rule out. Same reasoning that keeps
    /health always-200.
    """
    token = request.cookies.get(auth_service.COOKIE_NAME)
    valid = await run_in_threadpool(auth_service.session_is_valid, token)
    return {"authenticated": valid,
            "login_configured": auth_service.credentials_configured()}
