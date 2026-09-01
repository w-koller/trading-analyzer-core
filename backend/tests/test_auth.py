"""Checks for the shared-secret gate and the endpoints deliberately left open.

Run from backend/:  .venv/bin/python -m tests.test_auth

Two things must both hold, and they pull in opposite directions:

  * account and market data must not be readable by anything on the LAN;
  * /livez and /readyz must stay open, because the systemd watchdog probes
    them with no credentials and would read a 401 as a wedged backend and
    start restarting a perfectly healthy service.

Uses FastAPI's TestClient, so no server and no OpenD.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="auth-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from fastapi.testclient import TestClient                          # noqa: E402

from app import auth                                              # noqa: E402
from app.config import settings                                   # noqa: E402

from tests.harness import check, report  # noqa: E402


TOKEN = "test-token-abc123"
settings.api_token = TOKEN
auth._warned_disabled = False

from app.main import app                                          # noqa: E402
from app.routers import health as health_router                   # noqa: E402

# Stub the dependency probes. This is about who may call an endpoint, not
# about what it returns — and reaching the real gateway would block on the
# OpenD lock whenever a scan happens to be running, turning an auth test into
# a flaky several-minute one.
health_router._opend_status = lambda: {"connected": True, "qot_logined": True}
health_router._trade_status = lambda: {"connected": True}
health_router._ollama_status = lambda: {"reachable": True, "configured_model_present": True}

client = TestClient(app, raise_server_exceptions=False)

# --- open by design ----------------------------------------------------
# These carry no market or account data and the watchdog depends on them.
for path in ("/livez", "/readyz", "/health"):
    r = client.get(path)
    check(f"{path} stays reachable without a token", r.status_code != 401,
          f"HTTP {r.status_code}")

check("/livez needs no IO and answers 200", client.get("/livez").status_code == 200)

# --- guarded ------------------------------------------------------------
# /positions is the one that matters most: real holdings, sizes and P&L.
GUARDED = ["/positions", "/watchlist", "/setups", "/scan/status",
           "/watchlist/movers", "/outcomes"]
for path in GUARDED:
    r = client.get(path)
    check(f"{path} is refused without a token", r.status_code == 401,
          f"HTTP {r.status_code}")

r = client.get("/positions", headers={"X-API-Key": "wrong-token"})
check("a wrong token is refused", r.status_code == 401, f"HTTP {r.status_code}")

# Proven on a DB-only endpoint: /positions would reach the real Moomoo trade
# context and block behind whatever scan happens to be running.
r = client.get("/setups", headers={"X-API-Key": TOKEN})
check("the right token is accepted", r.status_code == 200, f"HTTP {r.status_code}")

# Writes are the ones that corrupt future advice, not just leak it.
for method, path, body in (
    ("post", "/scan/schedule/pause", None),
    ("post", "/outcomes", {"setup_id": 1}),
    ("post", "/watchlist/sync", None),
    ("patch", "/watchlist/US.TEST/enabled", {"enabled": False}),
):
    r = getattr(client, method)(path, json=body)
    check(f"{method.upper()} {path} is refused without a token",
          r.status_code == 401, f"HTTP {r.status_code}")

# --- an unset token now closes the header path, it does not open the gate --
#
# This INVERTS the old rule, deliberately. `require_api_key` still treats an
# empty API_TOKEN as "gate disabled", and its reasoning was sound while the
# box was LAN-only: bricking the dashboard over a missing .env line was the
# worse failure. That reasoning does not survive the move to a public address.
# The dashboard now signs in with a cookie, so an absent token cannot lock the
# owner out of anything — which leaves "waved through" as the only thing the
# old behaviour still bought, and that is now simply an open door.
settings.api_token = ""
auth._warned_disabled = False
check("an unset token does NOT open the gate",
      client.get("/watchlist").status_code == 401,
      f"HTTP {client.get('/watchlist').status_code}")
check("an unset token means a token cannot authenticate either",
      client.get("/watchlist", headers={"X-API-Key": ""}).status_code == 401)
check("auth_enabled() still reflects the setting", auth.auth_enabled() is False)

settings.api_token = TOKEN
check("auth_enabled() flips back on when configured", auth.auth_enabled() is True)


# --- sessions ------------------------------------------------------------
import pyotp                                                      # noqa: E402
from app.services import auth_service                             # noqa: E402

settings.auth_password_hash = auth_service.hash_password("a-long-test-password")
settings.auth_totp_secret = pyotp.random_base32()


def totp_now():
    return pyotp.TOTP(settings.auth_totp_secret).now()


# https:// matters — the session cookie is Secure, so a conformant client
# will not send it back over plain http. Using http here would test a
# weaker cookie than the one that actually ships.
fresh = TestClient(app, raise_server_exceptions=False,
                   base_url="https://testserver")

r = fresh.post("/auth/login", json={"password": "wrong", "totp": totp_now()})
check("login rejects a wrong password", r.status_code == 401, f"HTTP {r.status_code}")

r = fresh.post("/auth/login", json={"password": "a-long-test-password", "totp": "000000"})
check("login rejects a wrong TOTP code", r.status_code == 401, f"HTTP {r.status_code}")

check("both failures give the identical message, revealing nothing",
      fresh.post("/auth/login", json={"password": "x", "totp": totp_now()}).json()
      == fresh.post("/auth/login", json={"password": "a-long-test-password",
                                         "totp": "000000"}).json())

# Those four failures put this client at the lockout threshold; clear it so the
# success path is testable.
auth_service.note_success("testclient")

r = fresh.post("/auth/login",
               json={"password": "a-long-test-password", "totp": totp_now()})
check("login accepts the right pair", r.status_code == 200, f"HTTP {r.status_code}")
check("login sets a session cookie",
      auth_service.COOKIE_NAME in r.cookies, str(dict(r.cookies)))

# The cookie now stands in for the token. Note no X-API-Key is sent.
settings.api_token = ""
r = fresh.get("/setups")
check("a session cookie authenticates with no token configured at all",
      r.status_code == 200, f"HTTP {r.status_code}")
settings.api_token = TOKEN

r = fresh.get("/auth/me")
check("/auth/me reports the session", r.json().get("authenticated") is True)
check("/auth/me is always 200, so 'signed out' never reads as 'unreachable'",
      r.status_code == 200)

fresh.post("/auth/logout")
check("logout invalidates the session",
      fresh.get("/setups").status_code == 401)
check("/auth/me reports signed out after logout",
      fresh.get("/auth/me").json().get("authenticated") is False)

# --- lockout -------------------------------------------------------------
locked = TestClient(app, raise_server_exceptions=False,
                    base_url="https://testserver")
auth_service.note_success("testclient")
codes = [locked.post("/auth/login", json={"password": "nope", "totp": "000000"}).status_code
         for _ in range(settings.auth_max_login_failures)]
check("failed attempts are refused with 401 up to the limit",
      all(c == 401 for c in codes), str(codes))
r = locked.post("/auth/login",
                json={"password": "a-long-test-password", "totp": totp_now()})
check("past the limit even the RIGHT credentials are refused",
      r.status_code == 429, f"HTTP {r.status_code}")
auth_service.note_success("testclient")

# --- login must fail closed when unconfigured ----------------------------
settings.auth_password_hash = ""
r = TestClient(app, raise_server_exceptions=False).post(
    "/auth/login", json={"password": "anything", "totp": "000000"})
check("unconfigured login refuses rather than admitting everyone",
      r.status_code == 503, f"HTTP {r.status_code}")

settings.api_token = ""

# --- the KDF must be DETERMINISTIC on this machine ------------------------
#
# This guards a real, measured failure. A corrupted KDF shared object on this
# box made argon2 return a wrong digest for identical input 11.6% of the time
# — i.e. roughly one login in ten rejecting a correct password, at random,
# with nothing in any log. (Root cause was single-bit flips in files on disk,
# not RAM and not argon2; see services/auth_service.py.)
#
# So this is not a tautology test. If anyone swaps the KDF back to argon2, or
# to any other native implementation that misbehaves here, this fails.
_probe = auth_service.hash_password("determinism-probe")
settings.auth_password_hash = _probe
_wrong = sum(1 for _ in range(120)
             if not auth_service.verify_password("determinism-probe"))
check("the password KDF is deterministic over 120 verifications",
      _wrong == 0, f"{_wrong} spurious mismatches")
check("hashing the same password twice yields different stored values (salted)",
      auth_service.hash_password("x") != auth_service.hash_password("x"))
check("the stored format is self-describing, so cost can be raised later",
      _probe.startswith("scrypt$") and _probe.count("$") == 5, _probe[:24])
settings.auth_password_hash = ""

report("auth")
