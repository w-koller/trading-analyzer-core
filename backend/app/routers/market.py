"""Chart data: OHLCV bars plus every indicator as a full series.

The numbers here come from the same `indicators` functions the scan cycle
uses, so a line plotted on the chart is the same value the thesis reasoned
from — rule #1 holds, nothing is recomputed in the browser.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.services import market_data
from app.services.moomoo_gateway import GatewayError, get_gateway
from app.utils import market_hours

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/{code}/klines")
async def klines(code: str, days: int = market_data.KLINE_LOOKBACK_DAYS):
    """Daily bars with SMA/MACD/Bollinger overlays for one ticker.

    `days` counts calendar days, so the default is wider than it looks — a
    200-period SMA needs ~290 calendar days of history before it produces a
    single point.

    Daily only, deliberately: the indicator periods are daily-shaped, and a
    200-period SMA over 5-minute bars describes about three days rather than
    a trend. Offering a `ktype` here would let the UI plot something that
    looks like the scanner's numbers but means something else entirely.
    """
    try:
        payload = await run_in_threadpool(
            market_data.get_klines_with_overlays, get_gateway(), code, days
        )
    except market_data.NotEntitledError as exc:
        # 200, not an error status. The account genuinely cannot see this
        # market and never will without a data subscription, so there is
        # nothing here to retry and nothing broken to report. Returning 502
        # made a known product limitation read as an outage — the page showed
        # "502 Bad Gateway on /market/AU.CSL/klines" for a holding the user
        # can see in their own account. Same available/reason shape as
        # /positions, and the same instinct as movers' `skipped_markets`.
        return {
            "code": code,
            "market": exc.market,
            "available": False,
            "reason": str(exc),
            "gateway_detail": exc.detail,
            "is_delayed_data": market_hours.is_delayed_data(exc.market),
            "data_as_of": None,
            "last_bar_time": None,
            "bar_age_days": None,
            "bars_stale": False,
            "bars": [],
            # The full empty skeleton, not `{}`. One response shape means the
            # chart component reads `overlays.macd.hist` without every access
            # becoming optional — and an optional chain that quietly yields
            # undefined is how a rendering bug hides.
            "overlays": {
                "sma_fast": [],
                "sma_slow": [],
                "sma_cross_events": [],
                "bollinger": {"upper": [], "mid": [], "lower": []},
                "macd": {"macd": [], "signal": [], "hist": [], "cross_events": []},
            },
            "min_rows_available": 0,
            "warnings": [],
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GatewayError as exc:
        # Still a 502: OpenD is down, timed out, or dropped the context, and
        # a retry is the right response. Keeping this distinct from the case
        # above is the entire point of the change.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return payload
