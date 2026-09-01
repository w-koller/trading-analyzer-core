"""Session authentication for a dashboard that is now on the public internet.

`app/auth.py` guards the API with a shared secret and its own docstring is
blunt about the limit: the token was delivered to the browser, so anyone who
could load the page could read it. That was a defensible lock on a LAN-only
box. It is not defensible on an address the whole internet can reach, in front
of real holdings and real P&L.

So this module adds the thing that was missing — an actual login — and
`app/auth.py` keeps its header check for non-browser callers on localhost.
Neither replaces the other.

## What is stored where, and why

The password hash and the TOTP secret live in `backend/.env`, not in
`trading.db`. The database is snapshotted hourly by the backup timer and kept
for fourteen days; credentials do not belong in a rolling backup set. Sessions
*do* live in the database, because they need server-side revocation — a signed
stateless cookie cannot be cancelled, and "log out every device" has to mean
something after a phone is lost.

## Two deliberate inversions of existing rules

**Empty config fails CLOSED here.** `auth.py` treats an empty `API_TOKEN` as
"gate disabled", reasoning that locking the owner out of a homelab dashboard
over a missing .env line is the worse failure. That reasoning does not survive
contact with the public internet: an unconfigured login endpoint that admits
everyone is not a degraded state, it is an open door. Empty credentials mean
nobody can log in.

**A loopback client IP is treated as a misconfiguration, not a client.** See
`resolve_client_ip`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pyotp

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Password hashing: stdlib scrypt.
#
# argon2id was implemented first and swapped out — but NOT because argon2 is
# bad, and the real reason is worth recording because it nearly produced a
# wrong conclusion in this file.
#
# Symptom: argon2 returned a WRONG digest for identical input several percent
# of the time, non-deterministically (11.6% over one 200s run). That looked
# like an argon2-cffi bug. It was not. The box had bit-flipped shared objects
# on disk — the same fault CLAUDE.md records for numpy's libgfortran in
# August. `pandas/_libs/lib...so` was found with 15 single-bit flips, every
# one xor 0x08 or xor 0x10, inside a 22KB span; argon2's own library had been
# uninstalled before the integrity sweep ran, so it was never checked, and a
# clean reinstall measured 0/1827 wrong. RAM was ruled out separately: 611 GiB
# of write-and-verify produced zero corrupted bytes.
#
# So the choice here is not "argon2 is broken". It is that on a machine which
# demonstrably corrupts files written to disk, a password KDF with no
# third-party native dependency is one less thing that can silently start
# returning wrong answers — and a wrong answer here is a login that rejects a
# correct password with nothing in any log. scrypt is stdlib, RFC 7914,
# memory-hard, single-threaded, needs no compiler and no sdist build.
#
# If argon2id is ever wanted back, it is a fine choice; verify the installed
# files against their RECORD hashes first (see CLAUDE.md), and keep the
# determinism check in tests/test_auth.py either way.
#
# Parameters follow the usual interactive-login guidance: N=2^15, r=8, p=1
# (~32 MiB, ~0.07s here). The stored format is self-describing —
# "scrypt$N$r$p$salt$hash", all base64url — so these can be raised later
# without stranding an existing password.
# --------------------------------------------------------------------------

_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# scrypt needs roughly 128 * N * r bytes; CPython refuses above maxmem.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2

COOKIE_NAME = settings.auth_cookie_name


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                          dklen=_SCRYPT_DKLEN, maxmem=128 * n * r * 2)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Used only by scripts/set_password.py, never on the request path."""
    salt = secrets.token_bytes(16)
    dk = _derive(plain, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def credentials_configured() -> bool:
    return bool(settings.auth_password_hash and settings.auth_totp_secret)


def verify_password(plain: str) -> bool:
    """Compared with hmac.compare_digest, so a wrong password cannot be
    narrowed down a byte at a time from response timing.

    Parameters are read from the stored string rather than from the constants
    above, so raising the cost later does not invalidate an existing password.

    Returns False rather than raising on a malformed stored hash: a typo in
    .env should read as "wrong password" at the door and be diagnosed from the
    log, rather than a 500 that tells an anonymous caller the server is
    misconfigured.
    """
    stored = settings.auth_password_hash
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            raise ValueError(f"unsupported scheme {scheme!r}")
        candidate = _derive(plain, _b64d(salt), int(n), int(r), int(p))
    except Exception:                                            # noqa: BLE001
        logger.error("AUTH_PASSWORD_HASH is not a valid scrypt hash — "
                     "regenerate it with scripts/set_password.py")
        return False
    return hmac.compare_digest(candidate, _b64d(digest))


def verify_totp(code: str) -> bool:
    """One step of drift either side, which is ~30s of clock skew.

    `valid_window=1` is the standard allowance for a phone whose clock has
    drifted. Widening it further trades real security for convenience the user
    does not need, since the phone syncs its clock over the network anyway.
    """
    if not settings.auth_totp_secret:
        return False
    try:
        return pyotp.TOTP(settings.auth_totp_secret).verify(
            (code or "").strip().replace(" ", ""), valid_window=1)
    except Exception:                                          # noqa: BLE001
        # A malformed base32 secret raises rather than returning False.
        logger.error("AUTH_TOTP_SECRET is not valid base32 — "
                     "regenerate it with scripts/set_password.py")
        return False


def totp_provisioning_uri(secret: str, account: str = "trading-analyzer") -> str:
    """The otpauth:// URI an authenticator app scans. Used by the setup script."""
    return pyotp.TOTP(secret).provisioning_uri(name=account,
                                               issuer_name="Trading Analyzer")


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    """SHA-256, not a password KDF. This is a 256-bit random value, not a
    password: there is no dictionary to attack and no need to be slow, and
    being slow here would tax every authenticated request rather than just
    login."""
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_agent: str | None, ip: str | None) -> tuple[str, str]:
    """Mint a session. Returns (raw_token, expires_at); only the hash is stored."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc)
               + timedelta(days=settings.auth_session_days)).isoformat(timespec="seconds")
    db.create_session(_hash_token(token), expires, user_agent, ip)
    return token, expires


def session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    return db.get_session(_hash_token(token)) is not None


def end_session(token: str | None) -> bool:
    if not token:
        return False
    return db.delete_session(_hash_token(token))


def end_all_sessions() -> int:
    return db.delete_all_sessions()


# --------------------------------------------------------------------------
# Client IP and lockout
# --------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def resolve_client_ip(request) -> tuple[str, bool]:
    """(ip, is_distinct_client).

    uvicorn's ProxyHeadersMiddleware is on by default and rewrites
    `request.client.host` from X-Forwarded-For when the socket peer is in
    `forwarded_allow_ips` (default: 127.0.0.1). With NPM -> Next -> FastAPI all
    on this box, that is exactly the arrangement, so a correct chain yields the
    real external address here.

    **A loopback result therefore means the chain is broken, not that the
    client is local.** Every request arrives from 127.0.0.1 at the socket level
    now that the backend binds loopback, so if XFF did not survive the hops we
    would silently bucket the entire internet together — and a single attacker
    would lock the owner out. That is worse than no per-IP limiting at all, so
    the caller is told the value is not a distinct client and falls back to one
    global bucket, loudly.
    """
    host = (request.client.host if request.client else "") or ""
    return host, bool(host) and host not in _LOOPBACK


def lockout_remaining(ip: str) -> bool:
    """True if this IP has spent its attempts inside the window."""
    n = db.count_login_failures(ip, settings.auth_login_window_minutes)
    return n >= settings.auth_max_login_failures


def note_failure(ip: str) -> None:
    db.record_login_failure(ip)


def note_success(ip: str) -> None:
    db.clear_login_failures(ip)


# --------------------------------------------------------------------------
# The one place a login is decided
# --------------------------------------------------------------------------

def authenticate(password: str, totp_code: str) -> bool:
    """Both factors, evaluated without short-circuiting.

    `and` would return as soon as the password failed, so the response time
    would reveal which factor was wrong — the timing equivalent of the error
    message this deliberately does not vary. Both are always computed.
    """
    ok_password = verify_password(password)
    ok_totp = verify_totp(totp_code)
    return hmac.compare_digest(
        bytes([ok_password and ok_totp]), bytes([True]))
