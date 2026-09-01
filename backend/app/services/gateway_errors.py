"""Market-data gateway exceptions, with no SDK dependency.

These are plain RuntimeError subclasses. They lived in `moomoo_gateway` until
2026-09-01, which meant that `market_data.py` — the shared kline cache, which
is otherwise entirely deployment-agnostic — could not be imported at all
without the moomoo SDK installed, and neither could everything downstream of
it: `scanner`, `signals` and `thesis_scorecard`.

That transitively blocked the Informed Trader cloud deployment from reusing the
scan pipeline, which is the whole premise of keeping one codebase. One import
line was the entire coupling.

`moomoo_gateway` re-exports both names, so every existing call site and test is
unchanged.
"""

from __future__ import annotations


class GatewayError(RuntimeError):
    """Any failure talking to a market-data gateway."""


class GatewayTimeout(GatewayError):
    """A call exceeded its timeout — the gateway is wedged or unreachable."""
