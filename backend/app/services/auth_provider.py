"""The authentication contract, and the local implementation of it.

Why this exists
---------------
`services/auth_service.py` is a module of functions, not an object, and it is
single-tenant *in its signatures*: `authenticate(password, totp_code) -> bool`
has no user in it anywhere, because on a self-hosted box there is exactly one
user and naming them would be ceremony. The Informed Trader cloud deployment
has many users, so the contract has to grow an identity.

The load-bearing decision here
------------------------------
The protocol is declared in the PER-USER shape, and `LocalAuthProvider` binds
a synthetic single identity (`OWNER`) to satisfy it.

The alternative — declaring the protocol in `auth_service`'s existing
single-tenant shape and having the cloud provider work around it — was
rejected. It would mean the cloud provider either smuggling a user id through a
side channel or keeping a per-request global, and "which user is this" would
stop being visible in the type. Multi-tenancy is the thing most likely to be
got wrong in a way that leaks one user's data to another, so the user has to be
an explicit argument, and the single-tenant case is the one that adapts.

`authenticate` therefore returns `str | None` — a user id or None — rather than
a bool. For `LocalAuthProvider` a success is always the same `OWNER` string,
which carries exactly as much information as `True` did.

What this does NOT do yet
-------------------------
Nothing in core/ is rewired to go through this. `routers/auth.py` and
`app/auth.py` still call `auth_service` directly, and deliberately so: the live
login path on the self-hosted box should not change its behaviour inside a
commit whose stated purpose is a directory move plus a protocol extraction. The
seam is defined, defaulted and tested here; adopting it in core's routers is
its own change, with its own verification.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.services import auth_service

# The self-hosted deployment has exactly one user. Giving them a name costs
# nothing and lets one protocol serve both deployments.
OWNER = "owner"


@runtime_checkable
class AuthProvider(Protocol):
    """Password/session handling, with the user made explicit."""

    COOKIE_NAME: str

    def credentials_configured(self) -> bool:
        """False means nobody can sign in — never that everybody can.

        Self-hosted treats unset credentials as "login is impossible" rather
        than "login is open"; that inversion is deliberate and any cloud
        implementation must preserve it.
        """
        ...

    def authenticate(
        self, identity: str | None, secret: str, second_factor: str | None = None
    ) -> str | None:
        """Return the authenticated user id, or None. Never a bare bool.

        Implementations must evaluate every factor rather than short-circuiting,
        so response timing does not reveal WHICH factor was wrong.
        """
        ...

    def start_session(
        self, user_id: str, user_agent: str | None, ip: str | None
    ) -> tuple[str, str]:
        """Mint a session; returns (raw_token, expires_at). Store only a hash."""
        ...

    def session_is_valid(self, token: str | None) -> bool: ...

    def user_for_session(self, token: str | None) -> str | None:
        """The user a token belongs to, or None. This is the method that makes
        request scoping possible; `session_is_valid` alone cannot answer it."""
        ...

    def end_session(self, token: str | None) -> bool: ...

    def end_all_sessions(self, user_id: str | None = None) -> int:
        """Revoke sessions. None means every session this provider knows about —
        the 'I lost my phone' button."""
        ...

    def resolve_client_ip(self, request: Any) -> tuple[str, bool]: ...

    def lockout_remaining(self, ip: str) -> bool: ...

    def note_failure(self, ip: str) -> None: ...

    def note_success(self, ip: str) -> None: ...


class LocalAuthProvider:
    """Today's scrypt + TOTP behaviour, unchanged, behind the protocol.

    Every method delegates to `auth_service`. This class adds no policy of its
    own — no new hashing, no new session semantics, no new lockout maths — so
    the 36 checks in `tests/test_auth.py` continue to describe the whole of the
    self-hosted behaviour.
    """

    COOKIE_NAME = auth_service.COOKIE_NAME

    def credentials_configured(self) -> bool:
        return auth_service.credentials_configured()

    def authenticate(
        self, identity: str | None, secret: str, second_factor: str | None = None
    ) -> str | None:
        """`identity` is ignored: this deployment has one user.

        It is accepted rather than omitted so the signature matches the
        protocol, and a caller written against the cloud provider does not
        silently break here.
        """
        if auth_service.authenticate(secret, second_factor or ""):
            return OWNER
        return None

    def start_session(
        self, user_id: str, user_agent: str | None, ip: str | None
    ) -> tuple[str, str]:
        # user_id is not stored: auth_sessions has no user column, because
        # there has only ever been one user to attribute a session to.
        return auth_service.start_session(user_agent, ip)

    def session_is_valid(self, token: str | None) -> bool:
        return auth_service.session_is_valid(token)

    def user_for_session(self, token: str | None) -> str | None:
        return OWNER if auth_service.session_is_valid(token) else None

    def end_session(self, token: str | None) -> bool:
        return auth_service.end_session(token)

    def end_all_sessions(self, user_id: str | None = None) -> int:
        return auth_service.end_all_sessions()

    def resolve_client_ip(self, request: Any) -> tuple[str, bool]:
        return auth_service.resolve_client_ip(request)

    def lockout_remaining(self, ip: str) -> bool:
        return auth_service.lockout_remaining(ip)

    def note_failure(self, ip: str) -> None:
        auth_service.note_failure(ip)

    def note_success(self, ip: str) -> None:
        auth_service.note_success(ip)


_provider: AuthProvider | None = None


def get_auth_provider() -> AuthProvider:
    """The auth provider for the active deployment mode."""
    global _provider
    if _provider is None:
        from app.config import settings

        if settings.deployment_mode == "cloud":
            raise NotImplementedError(
                "No cloud auth provider is registered in core/. The cloud "
                "deployment supplies its own AuthProvider implementation."
            )
        _provider = LocalAuthProvider()
    return _provider
