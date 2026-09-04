"""Sector ETFs: the only place signed institutional-vs-retail flow exists.

WHY THIS MODULE EXISTS AT ALL

`get_capital_flow` is the one Moomoo call that returns SIGNED money flow
split by order size, and it refuses plate codes outright:

    "Only stocks, warrants, and funds are supported; other security types
     are not supported."

Funds ARE supported, so a sector ETF is the only available proxy for
sector-level flow. Verified arithmetic against the live server:

    in_flow      == super + big + mid + sml
    main_in_flow == super + big

so `main_in_flow` is net **block-sized order flow** — a reasonable read on
institutional participation, and NOT reported block trades, NOT 13F, and NOT
fund creations. Describe it as what it is.

WHAT IS AND IS NOT ETF "FUND FLOW"

The request asked for ETF creations and redemptions. Those are a
primary-market quantity and Moomoo publishes no history of them — but it
does publish `trust_outstanding_units` on the ETF snapshot, and the CHANGE
in outstanding units across sessions is exactly what a net creation or
redemption is. So this captures units and AUM on every run at zero extra
call cost, and reports the derived figure only once enough consecutive
sessions exist. Before that it says so rather than showing a zero. That
turns "unavailable forever" into "unavailable for about a month".

WHY THE REGISTRY IS CODE

Same argument as `news_feeds.py`: 20-odd entries each carrying a label, an
asset class and a plate mapping do not fit environment variables, and a
human can read and correct a table in a file. The standard that file sets is
also inherited — **every code below was probed against the live account on
2026-08-30 before it was committed**, all 39 candidates resolving with
`trust_valid` true and AUM and outstanding units populated. Do not add a
guessed ticker.

WHY THE PLATE MAPPING IS SPARSE ON PURPOSE

`plate_codes` says "this ETF is a fair flow proxy for these plates", not
"these are its holdings". A narrow theme ETF maps tightly: SMH really does
track Semiconductors. A broad sector SPDR does not — XLK spans semis,
infrastructure software, application software and consumer electronics at
once, so claiming it proxies any one of them would attach a number to a
sector that is mostly about three others. Those entries carry an EMPTY
mapping and are still ingested, because their flow is useful as market
context on its own. An empty tuple here is a deliberate statement, not an
oversight.

RATE LIMIT

`get_capital_flow` is 30 calls / 30 seconds — measured, and stated by the
server itself as an ordinary error STRING rather than an exception. A naive
loop over 39 codes returned 30 rows and 9 silent failures. Paced below the
ceiling so the rest of the app can still call it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app import db
# From its real home rather than `sdk_gateway`'s back-compat re-export, which
# drags `moomoo` in at module scope (see app/utils/rate_limiter.py). This module
# needs the gateway for its own work, but `sector_flow` imports it lazily from
# `rotation_pairs`, so the old path put the SDK back on a code path that had
# just been freed of it.
from app.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

#: Below the measured 30/30s ceiling, leaving room for anything else that
#: needs the call while a batch job is running.
_FLOW_CALLS = 24
_FLOW_WINDOW = 30.0

#: Sessions of ETF history requested per run. Idempotent on the primary key,
#: so a rolling window self-heals a day the job missed.
FLOW_DAYS = 30

#: Consecutive unit snapshots needed before a creation/redemption figure is
#: reported at all.
MIN_UNIT_SESSIONS = 20


@dataclass(frozen=True)
class SectorEtf:
    code: str
    label: str
    #: Plates this ETF is a fair flow proxy for. Empty = deliberately none;
    #: see the module docstring.
    plate_codes: tuple[str, ...]
    asset_class: str


#: Probed live 2026-08-30: all resolved, all `trust_valid`, all accepted
#: get_capital_flow. Plate codes checked against get_plate_list the same day.
ETFS: tuple[SectorEtf, ...] = (
    # --- broad sector SPDRs: no plate mapping, market context only --------
    SectorEtf("US.XLK", "Technology Select Sector", (), "sector"),
    SectorEtf("US.XLF", "Financial Select Sector", (), "sector"),
    SectorEtf("US.XLE", "Energy Select Sector", (), "sector"),
    SectorEtf("US.XLV", "Health Care Select Sector", (), "sector"),
    SectorEtf("US.XLI", "Industrial Select Sector", (), "sector"),
    SectorEtf("US.XLY", "Consumer Discretionary Select Sector", (), "sector"),
    SectorEtf("US.XLP", "Consumer Staples Select Sector", (), "sector"),
    SectorEtf("US.XLU", "Utilities Select Sector", ("US.LIST2472",), "sector"),
    SectorEtf("US.XLB", "Materials Select Sector", (), "sector"),
    SectorEtf("US.XLRE", "Real Estate Select Sector", (), "sector"),
    SectorEtf("US.XLC", "Communication Services Select Sector", (), "sector"),

    # --- AI hardware -----------------------------------------------------
    SectorEtf("US.SMH", "VanEck Semiconductor",
              ("US.LIST2015", "US.LIST2016", "US.LIST2548"), "theme"),
    SectorEtf("US.SOXX", "iShares Semiconductor",
              ("US.LIST2015", "US.LIST2016"), "theme"),

    # --- AI software and cloud -------------------------------------------
    SectorEtf("US.IGV", "iShares Expanded Tech-Software",
              ("US.LIST2508", "US.LIST2470", "US.LIST23492"), "theme"),
    SectorEtf("US.SKYY", "First Trust Cloud Computing",
              ("US.LIST2540", "US.LIST2521"), "theme"),
    SectorEtf("US.WCLD", "WisdomTree Cloud Computing", ("US.LIST2540",), "theme"),
    SectorEtf("US.BOTZ", "Global X Robotics & AI",
              ("US.LIST2653", "US.LIST2136"), "theme"),
    SectorEtf("US.ARKK", "ARK Innovation", ("US.LIST2153",), "theme"),
    SectorEtf("US.HACK", "Amplify Cybersecurity", ("US.LIST2570",), "theme"),
    SectorEtf("US.CIBR", "First Trust Cybersecurity", ("US.LIST2570",), "theme"),

    # --- transition materials --------------------------------------------
    SectorEtf("US.REMX", "VanEck Rare Earth & Strategic Metals",
              ("US.LIST23700", "US.LIST2501"), "theme"),
    SectorEtf("US.LIT", "Global X Lithium & Battery Tech",
              ("US.LIST23987",), "theme"),
    SectorEtf("US.XME", "SPDR Metals & Mining",
              ("US.LIST2101", "US.LIST22865"), "theme"),
    SectorEtf("US.URA", "Global X Uranium", ("US.LIST2430",), "theme"),
    SectorEtf("US.ICLN", "iShares Global Clean Energy", (), "theme"),
    SectorEtf("US.TAN", "Invesco Solar", ("US.LIST2047",), "theme"),
    SectorEtf("US.GDX", "VanEck Gold Miners", ("US.LIST2110",), "theme"),

    # --- legacy energy ---------------------------------------------------
    SectorEtf("US.XOP", "SPDR Oil & Gas Exploration & Production",
              ("US.LIST2058",), "theme"),
    SectorEtf("US.OIH", "VanEck Oil Services", ("US.LIST2257",), "theme"),

    # --- other sub-sectors ------------------------------------------------
    SectorEtf("US.IBB", "iShares Biotechnology", ("US.LIST2069",), "theme"),
    SectorEtf("US.XBI", "SPDR S&P Biotech", ("US.LIST2069",), "theme"),
    SectorEtf("US.KRE", "SPDR S&P Regional Banking", ("US.LIST2456",), "theme"),
    SectorEtf("US.ITA", "iShares Aerospace & Defense", ("US.LIST2089",), "theme"),
    SectorEtf("US.JETS", "US Global Jets", ("US.LIST2090",), "theme"),
    SectorEtf("US.FINX", "Global X FinTech", ("US.LIST2657",), "theme"),
    SectorEtf("US.IPAY", "Amplify Digital Payments",
              ("US.LIST2587", "US.LIST2657"), "theme"),
    SectorEtf("US.BLOK", "Amplify Transformational Data Sharing",
              ("US.LIST20010",), "theme"),
    SectorEtf("US.KWEB", "KraneShares CSI China Internet", (), "theme"),
)

ETF_BY_CODE: dict[str, SectorEtf] = {e.code: e for e in ETFS}


def plate_to_etfs() -> dict[str, list[SectorEtf]]:
    """Reverse index: which ETFs proxy for each plate."""
    out: dict[str, list[SectorEtf]] = {}
    for etf in ETFS:
        for plate in etf.plate_codes:
            out.setdefault(plate, []).append(etf)
    return out


def ingest_flows(
    gateway,
    days: int = FLOW_DAYS,
    limiter: RateLimiter | None = None,
) -> dict[str, Any]:
    """Fetch capital flow and unit counts for every registered ETF.

    Never raises on a partial failure — a flow panel covering 30 of 39 ETFs
    beats none. Failures are counted and returned rather than swallowed,
    because the rate limit reports itself as an ordinary error string and a
    silent partial result is exactly the trap being guarded against.
    """
    limiter = limiter or RateLimiter(_FLOW_CALLS, _FLOW_WINDOW, "etf flow")
    result: dict[str, Any] = {
        "etfs": len(ETFS),
        "flow_ok": 0,
        "flow_failed": [],
        "rows_written": 0,
        "units_captured": 0,
    }

    end = date.today()
    start = end - timedelta(days=days)
    rows: list[dict[str, Any]] = []

    for etf in ETFS:
        try:
            limiter.acquire()
            flows = gateway.get_capital_flow(
                etf.code, start=start.isoformat(), end=end.isoformat()
            )
        except Exception as exc:
            result["flow_failed"].append(f"{etf.code}: {exc}")
            continue
        result["flow_ok"] += 1
        for row in flows:
            flow_date = str(row.get("capital_flow_item_time") or "")[:10]
            if not flow_date:
                continue
            rows.append(
                {
                    "etf_code": etf.code,
                    "flow_date": flow_date,
                    "in_flow": _f(row.get("in_flow")),
                    "main_in_flow": _f(row.get("main_in_flow")),
                    "super_in_flow": _f(row.get("super_in_flow")),
                    "big_in_flow": _f(row.get("big_in_flow")),
                    "mid_in_flow": _f(row.get("mid_in_flow")),
                    "sml_in_flow": _f(row.get("sml_in_flow")),
                }
            )

    # Units and AUM ride a SEPARATE snapshot batch from the plates', because
    # get_market_snapshot fails a whole batch on one unsupported code and
    # mixing the two would risk losing both (the get_movers precedent).
    newest = max((r["flow_date"] for r in rows), default=date.today().isoformat())
    try:
        for row in gateway.get_snapshot([e.code for e in ETFS]):
            if not row.get("trust_valid"):
                continue
            rows.append(
                {
                    "etf_code": row.get("code"),
                    "flow_date": newest,
                    "trust_aum": _f(row.get("trust_aum")),
                    "trust_outstanding_units": _f(row.get("trust_outstanding_units")),
                    "last_price": _f(row.get("last_price")),
                }
            )
            result["units_captured"] += 1
    except Exception as exc:
        result["flow_failed"].append(f"snapshot: {exc}")

    result["rows_written"] = db.upsert_etf_flows(rows)
    logger.info(
        "etf flows: %d/%d ok, %d rows, %d unit snapshots, %d failures",
        result["flow_ok"], len(ETFS), result["rows_written"],
        result["units_captured"], len(result["flow_failed"]),
    )
    return result


def _f(value: Any) -> float | None:
    """None for anything unusable — including the literal string 'N/A',
    which is what INTRADAY returns in `main_in_flow`."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out or out in (float("inf"), float("-inf")) else out


def flow_for_plate(plate_code: str, days: int = 21) -> dict[str, Any] | None:
    """Signed ETF flow standing in for one sector, or None if none maps.

    Returns None rather than a zeroed structure when no ETF proxies for this
    plate: 238 of 262 plates have no proxy, and a zero would read as "no
    institutional flow" instead of "not measured here". That distinction is
    the whole reason this is reported beside the score and never inside it.
    """
    etfs = plate_to_etfs().get(plate_code)
    if not etfs:
        return None
    series = db.get_etf_flows([e.code for e in etfs], days=days)

    out_etfs = []
    for etf in etfs:
        rows = [r for r in series.get(etf.code, []) if r.get("in_flow") is not None]
        if not rows:
            continue
        total_in = sum(r["in_flow"] or 0.0 for r in rows)
        total_main = sum(r["main_in_flow"] or 0.0 for r in rows)
        # Share of gross activity that was block-sized. Uses ABSOLUTE values
        # in the denominator: a period whose inflows and outflows nearly
        # cancel has a near-zero net, and dividing by that would produce a
        # meaningless ratio in the hundreds.
        gross = sum(abs(r["in_flow"] or 0.0) for r in rows)
        out_etfs.append(
            {
                "code": etf.code,
                "label": etf.label,
                "sessions": len(rows),
                "net_flow": round(total_in, 2),
                "main_flow": round(total_main, 2),
                "institutional_share": (
                    round(abs(total_main) / gross, 4) if gross > 0 else None
                ),
                "units": _unit_change(series.get(etf.code, [])),
            }
        )
    if not out_etfs:
        return None
    return {
        "plate_code": plate_code,
        "days": days,
        "etfs": out_etfs,
        "note": (
            "Net flow is block-sized order flow from the ETF that tracks this "
            "sector, not fund creations and not the sector's own constituents."
        ),
    }


def _unit_change(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Net creation/redemption from the change in outstanding units.

    This is the genuine article — a change in units outstanding IS a net
    creation or redemption — but it has no history, so it can only be
    accumulated forward from the first capture. Until there are enough
    sessions it reports that it is still accumulating rather than showing a
    figure derived from two points.
    """
    have = [r for r in rows if r.get("trust_outstanding_units") is not None]
    if len(have) < MIN_UNIT_SESSIONS:
        return {
            "available": False,
            "reason": (
                f"accumulating: {len(have)} of {MIN_UNIT_SESSIONS} sessions captured"
            ),
            "sessions": len(have),
            "min_sessions": MIN_UNIT_SESSIONS,
        }
    first, last = have[0], have[-1]
    delta = last["trust_outstanding_units"] - first["trust_outstanding_units"]
    price = last.get("last_price")
    return {
        "available": True,
        "reason": None,
        "sessions": len(have),
        "min_sessions": MIN_UNIT_SESSIONS,
        "unit_change": round(delta, 2),
        "estimated_flow": round(delta * price, 2) if price else None,
        "note": "change in shares outstanding — a true net creation/redemption",
    }
