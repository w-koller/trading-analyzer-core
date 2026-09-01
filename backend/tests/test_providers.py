"""The two deployment seams: MarketDataProvider and AuthProvider.

These protocols exist so the Informed Trader cloud deployment can supply a
different data source and a multi-user auth backend without forking core/.
The whole point is that conformance is STRUCTURAL — `MoomooGateway` and
`auth_service` were not modified to satisfy them — so what is worth asserting
is exactly that: that today's implementations already fit, that the protocols
still reject something that does not, and that the assumptions the protocols
rest on are true of the installed SDK rather than merely believed.

Run:  .venv/bin/python -m tests.test_providers
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, settings                                # noqa: E402
from app.services import auth_service                                    # noqa: E402
from app.services.auth_provider import (                                 # noqa: E402
    OWNER, AuthProvider, LocalAuthProvider, get_auth_provider,
)
from app.services.market_data_provider import (                          # noqa: E402
    MarketDataProvider, OptionsProvider, ProviderStatus,
    get_market_data_provider,
)
from app.services.moomoo_gateway import GatewayStatus, MoomooGateway     # noqa: E402

from tests.harness import check, check_eq, report                        # noqa: E402

# --- the existing implementations must already fit -------------------------
# Constructing a gateway opens no OpenD context; that happens lazily on the
# first call, so this is safe with OpenD down.
gw = MoomooGateway()

check("MoomooGateway satisfies MarketDataProvider", isinstance(gw, MarketDataProvider),
      "structural — the gateway was not modified to achieve this")
check("MoomooGateway satisfies OptionsProvider", isinstance(gw, OptionsProvider),
      "self-hosted has option open interest, so it does walls")
check("GatewayStatus satisfies ProviderStatus",
      isinstance(GatewayStatus(connected=True), ProviderStatus))
check("LocalAuthProvider satisfies AuthProvider", isinstance(LocalAuthProvider(), AuthProvider))

# --- and the protocols must still reject a non-conformer -------------------
# A runtime_checkable protocol that accepts everything documents nothing.


class OnlyHealth:
    def health(self): ...


class MarketOnly:
    """Bars and quotes but no options — the shape a cloud provider has."""

    def health(self): ...
    def get_snapshot(self, codes): ...
    def get_history_kline(self, code, start=None, end=None, ktype="K_DAY",
                          max_count=250, max_pages=20): ...


check("a bare object does not satisfy MarketDataProvider",
      not isinstance(object(), MarketDataProvider))
check("a partial implementation does not satisfy it either",
      not isinstance(OnlyHealth(), MarketDataProvider),
      "health() alone is not a market data provider")
check("a non-conformer does not satisfy AuthProvider",
      not isinstance(object(), AuthProvider))

# --- the options split is the load-bearing part of the design --------------
check("a market-only provider DOES satisfy MarketDataProvider",
      isinstance(MarketOnly(), MarketDataProvider))
check("...and does NOT satisfy OptionsProvider",
      not isinstance(MarketOnly(), OptionsProvider),
      "cloud v1 has no OI source; this must be knowable statically, "
      "not by raising at call time")

# --- the assumption the ktype annotation rests on --------------------------
# The protocol types ktype as `str` so that the moomoo SDK never has to be
# imported by a cloud deployment. That is only correct if the SDK's enum
# member IS a plain string. Verify it against the installed SDK rather than
# trusting the documentation — this project has been wrong that way before.
import moomoo as ft                                                      # noqa: E402

check_eq("moomoo.KLType.K_DAY is the plain string 'K_DAY'", ft.KLType.K_DAY, "K_DAY")
check("...so it is a str, not an enum object", isinstance(ft.KLType.K_DAY, str),
      f"type={type(ft.KLType.K_DAY).__name__}")

# --- deployment mode -------------------------------------------------------
check_eq("deployment_mode defaults to self_hosted", settings.deployment_mode, "self_hosted")

def _bad_mode_rejected() -> bool:
    try:
        Settings(deployment_mode="nonsense")
        return False
    except Exception:
        return True


check("an unrecognised deployment_mode is rejected outright", _bad_mode_rejected(),
      "a typo in DEPLOYMENT_MODE must stop the process, not silently pick a branch")

check("cloud-only settings exist and default empty",
      settings.cloud_database_url == "" and settings.twelve_data_api_key == "",
      "declared in core/ so there is one settings object, unread under self_hosted")
check_eq("the Twelve Data daily credit ceiling is stated", settings.twelve_data_daily_credits, 800)

# --- factories resolve to today's behaviour --------------------------------
check_eq("market factory returns the Moomoo gateway under self_hosted",
         type(get_market_data_provider()).__name__, "MoomooGateway")
check_eq("auth factory returns LocalAuthProvider under self_hosted",
         type(get_auth_provider()).__name__, "LocalAuthProvider")
check("the market factory returns the SINGLETON, not a new context each call",
      get_market_data_provider() is get_market_data_provider(),
      "a second OpenQuoteContext would be a leaked connection")

# --- LocalAuthProvider adds no policy of its own ---------------------------
lp = LocalAuthProvider()
check_eq("COOKIE_NAME is auth_service's, not a second copy",
         lp.COOKIE_NAME, auth_service.COOKIE_NAME)
check_eq("credentials_configured delegates",
         lp.credentials_configured(), auth_service.credentials_configured())

# authenticate returns an identity or None — never a bare bool, because the
# cloud provider must be able to say WHICH user authenticated.
result = lp.authenticate(None, "definitely-not-the-password", "000000")
check("a wrong password yields None, not False", result is None,
      f"got {result!r}")
check("the return type is an identity, not a bool",
      not isinstance(result, bool), "None or a user id, so callers cannot "
      "treat it as a truthy flag and lose the user")
check_eq("the local identity is a stable, named single user", OWNER, "owner")

check("user_for_session is present, which is what makes request scoping possible",
      hasattr(lp, "user_for_session") and lp.user_for_session(None) is None,
      "session_is_valid alone cannot answer 'whose session is this'")

report("providers")
