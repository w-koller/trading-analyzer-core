"""The market-data contract the deterministic pipeline actually depends on.

Why this exists
---------------
`indicators.compute()`, `similarity.build_feature_vector()`,
`options_walls.fetch_walls()` and `scanner.scan_ticker()` all take a `gateway`
argument and call methods on it. That has always been duck typing: nothing
declared what a gateway had to provide, so the contract lived scattered across
call sites and could only be discovered by reading them all.

The Informed Trader cloud deployment supplies a different data source (Twelve
Data, no broker), so the contract has to be written down. This module is that
contract and nothing more — it is deliberately NOT a base class, and
`MoomooGateway` does not inherit from anything here. Conformance is structural,
so today's gateway satisfies these protocols without a single byte of its
runtime behaviour changing. That is what makes "the offline suites report
identical check counts" a meaningful statement about this change.

What the surface was derived from, rather than guessed
------------------------------------------------------
Only the three methods the pipeline genuinely consumes are required:

  health()             -> a status object the health routers render
  get_snapshot()       -> scanner.py's `_spot_price` reads `last_price`;
                          market_data.py's movers additionally read
                          `prev_close_price` and the extended-hours fields
  get_history_kline()  -> must carry indicators.REQUIRED_COLUMNS
                          ({open, high, low, close, volume}) plus `time_key`,
                          which market_data._time_column looks for and
                          thesis_scorecard parses forward bars from

`MoomooGateway` exposes a great deal more — plates, capital flow, earnings,
analyst consensus, news search. None of that is required here, because none of
it is required to produce a thesis, and demanding it would make every future
provider implement Moomoo's whole API to supply a price.

`ktype` is typed `str`, not the SDK's enum
------------------------------------------
`moomoo.KLType.K_DAY` **is** the plain string `'K_DAY'` — verified against the
installed SDK, not assumed from its documentation. Typing the parameter as
`str` therefore leaves `MoomooGateway.get_history_kline` structurally
conformant exactly as written, while keeping the moomoo SDK out of this module
entirely. That matters: the cloud deployment does not install moomoo-api at
all, so anything it imports must not reach for it.

Options are a SEPARATE protocol, on purpose
-------------------------------------------
Cloud v1 has no confirmed source of option open interest, so it computes no
walls. Folding the two option methods into `MarketDataProvider` would force a
cloud provider to implement methods it cannot honour and raise from them at
call time — a runtime failure standing in for a fact that is knowable
statically. Keeping `OptionsProvider` separate means a provider that cannot do
options simply does not satisfy it, and `scanner.scan_ticker(with_walls=...)`
already has the switch to skip them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd


@runtime_checkable
class ProviderStatus(Protocol):
    """What a health surface needs from a provider's status object.

    `moomoo_gateway.GatewayStatus` already satisfies this. A cloud provider
    returns its own status type carrying its own fields (credit budget spent,
    for instance) rather than pretending to have an OpenD quote session.
    """

    @property
    def healthy(self) -> bool: ...

    def to_dict(self) -> dict[str, Any]: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """Bars and quotes. The minimum needed to produce a thesis."""

    def health(self) -> ProviderStatus: ...

    def get_snapshot(self, codes: list[str]) -> list[dict[str, Any]]:
        """Point-in-time rows keyed by the same `MARKET.SYMBOL` codes used
        everywhere else. An empty `codes` list must return `[]` without a
        round trip."""
        ...

    def get_history_kline(
        self,
        code: str,
        start: str | None = None,
        end: str | None = None,
        ktype: str = "K_DAY",
        max_count: int = 250,
        max_pages: int = 20,
    ) -> "pd.DataFrame":
        """Chronologically ordered OHLCV bars.

        Implementations MUST return every bar in the requested window, not the
        first page of one. Ignoring pagination is the specific failure this
        project has already been bitten by: it returns well-formed data that is
        simply old, which no schema check catches.
        """
        ...


@runtime_checkable
class OptionsProvider(Protocol):
    """Option chains, for wall computation. Optional — see the module docstring."""

    def get_option_expirations(self, code: str) -> list[str]: ...

    def get_option_chain(self, code: str, expiry: str) -> list[dict[str, Any]]: ...


def get_market_data_provider() -> MarketDataProvider:
    """The provider for the active deployment mode.

    The moomoo import is deliberately INSIDE the function. The cloud
    deployment does not install moomoo-api, so importing it at module scope
    would make this module unimportable there — and this module is exactly what
    the cloud provider needs in order to declare what it implements.
    """
    from app.config import settings

    if settings.deployment_mode == "cloud":
        raise NotImplementedError(
            "No cloud market-data provider is registered in core/. The cloud "
            "deployment supplies its own implementation of MarketDataProvider "
            "and passes it to the pipeline directly."
        )

    from app.services.moomoo_gateway import get_gateway

    return get_gateway()
