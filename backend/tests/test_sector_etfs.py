"""Checks for the sector-ETF flow leg.

Run from backend/:  .venv/bin/python -m tests.test_sector_etfs

Two things matter here beyond arithmetic. First, the registry has to stay
honest — `news_feeds.py`'s standard is that every entry is probed before it
is committed, and a mapping to a plate that does not exist would silently
attach a sector's flow panel to nothing. Second, the unit-change figure must
refuse to report until it has enough sessions, because it is the one number
here with no history behind it.

Offline: temp database, fake gateway, no network.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="sector-etf-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import sector_etfs as se  # noqa: E402
from app.services.sdk_gateway import RateLimiter  # noqa: E402
from tests.harness import check, check_close, check_eq, report  # noqa: E402

FAST = RateLimiter(10_000, 0.001, "test")


# --- registry hygiene ------------------------------------------------------

codes = [e.code for e in se.ETFS]
check_eq("no duplicate ETF codes", len(codes), len(set(codes)))
check("every code is market-prefixed like watchlist_cache.code",
      all(c.startswith("US.") for c in codes))
check("every entry has a human label", all(e.label.strip() for e in se.ETFS))
check("every entry declares an asset class",
      all(e.asset_class in ("sector", "theme") for e in se.ETFS))
check("plate mappings are tuples, so the registry stays immutable",
      all(isinstance(e.plate_codes, tuple) for e in se.ETFS))
check("mapped plate codes are plate-shaped, not ticker-shaped",
      all(p.startswith("US.LIST") for e in se.ETFS for p in e.plate_codes),
      "a ticker here would map a sector panel to a stock")

broad = [e for e in se.ETFS if not e.plate_codes]
check("the broad sector SPDRs map to no plate, deliberately",
      len(broad) >= 10,
      "XLK spans semis, both software industries and consumer electronics — "
      "claiming it proxies any one of them attaches a number to a sector "
      "that is mostly about three others")
check("...and they are still registered, for market context",
      all(e.code in se.ETF_BY_CODE for e in broad))

rev = se.plate_to_etfs()
check("the reverse index covers every mapped plate",
      len(rev) == len({p for e in se.ETFS for p in e.plate_codes}))
check("a plate with two proxies keeps both",
      len(rev.get("US.LIST2069", [])) == 2,
      "IBB and XBI both track Biotechnology")

# The canonical rotations from the feature request must be representable.
check("AI hardware has a proxy", "US.LIST2548" in rev, "AI Chip <- SMH")
check("AI software has a proxy", "US.LIST23492" in rev,
      "AI application software <- IGV")
check("legacy energy has a proxy", "US.LIST2058" in rev, "Oil & Gas E&P <- XOP")
check("transition materials have a proxy", "US.LIST23700" in rev,
      "Rare Earth Stocks <- REMX")


# --- ingest ---------------------------------------------------------------

class FakeGateway:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.flow_calls = []
        self.snapshot_calls = []

    def get_capital_flow(self, code, period_type=None, start=None, end=None):
        self.flow_calls.append(code)
        if code in self.fail:
            raise RuntimeError(
                "Get Capital Flow request failed due to high frequency. "
                "Maximum 30 times per 30 s"
            )
        return [
            {"capital_flow_item_time": f"2026-08-{20 + i} 00:00:00",
             "in_flow": 1000.0 * (i + 1), "main_in_flow": 600.0 * (i + 1),
             "super_in_flow": 400.0 * (i + 1), "big_in_flow": 200.0 * (i + 1),
             "mid_in_flow": 250.0 * (i + 1), "sml_in_flow": 150.0 * (i + 1)}
            for i in range(5)
        ]

    def get_snapshot(self, codes):
        self.snapshot_calls.append(list(codes))
        return [
            {"code": c, "trust_valid": True, "trust_aum": 1e10,
             "trust_outstanding_units": 1e8, "last_price": 100.0}
            for c in codes
        ]


gw = FakeGateway()
res = se.ingest_flows(gw, limiter=FAST)
check_eq("every ETF is asked for flow", res["flow_ok"], len(se.ETFS))
check_eq("no failures on a clean run", res["flow_failed"], [])
check("rows were written", res["rows_written"] > 0)
check_eq("units come from ONE snapshot call, not one per ETF",
         len(gw.snapshot_calls), 1)
check("the ETF snapshot is a SEPARATE batch from the plates'",
      set(gw.snapshot_calls[0]) == set(codes),
      "get_market_snapshot fails a whole batch on one bad code, so mixing "
      "plates and ETFs would risk losing both (the get_movers precedent)")
check_eq("units captured for every ETF", res["units_captured"], len(se.ETFS))

again = se.ingest_flows(gw, limiter=FAST)
check_eq("a re-run is idempotent on (etf, date)",
         again["rows_written"], res["rows_written"])

# A rate-limit refusal must be COUNTED, never swallowed: the server reports
# it as an ordinary error string, so a silent partial result is the trap.
gw2 = FakeGateway(fail={"US.SMH", "US.IGV"})
res2 = se.ingest_flows(gw2, limiter=FAST)
check_eq("a refused call is reported, not swallowed", len(res2["flow_failed"]), 2)
check_eq("...and the rest still ingest", res2["flow_ok"], len(se.ETFS) - 2)
check("...with the server's own wording preserved",
      any("high frequency" in f for f in res2["flow_failed"]))
check_eq("pacing stays below the measured 30/30s ceiling", se._FLOW_CALLS, 24)


# --- flow_for_plate -------------------------------------------------------

flow = se.flow_for_plate("US.LIST2015")
check("a mapped plate gets a flow reading", flow is not None)
check_eq("...naming the ETFs behind it",
         sorted(e["code"] for e in flow["etfs"]), ["US.SMH", "US.SOXX"])
check("...and says what the number is",
      "not fund creations" in flow["note"],
      "main_in_flow is block-sized order flow, not creations and not 13F")

etf = flow["etfs"][0]
check_close("net flow sums the sessions", etf["net_flow"], 15000.0, abs_tol=1e-6)
check_close("main flow sums the block-sized share", etf["main_flow"], 9000.0, abs_tol=1e-6)
check_close("institutional share is main over GROSS activity",
            etf["institutional_share"], 0.6, abs_tol=1e-6)
check("institutional share cannot exceed 1",
      all(e["institutional_share"] is None or e["institutional_share"] <= 1.0
          for e in flow["etfs"]),
      "dividing by NET would give a ratio in the hundreds when a period's "
      "inflows and outflows nearly cancel")

unmapped = se.flow_for_plate("US.LIST9999")
check("an unmapped plate returns None, not a zeroed structure",
      unmapped is None,
      "238 of 262 plates have no proxy; a zero would read as 'no "
      "institutional flow' instead of 'not measured here'")


# --- the unit-change figure refuses to guess ------------------------------

units = flow["etfs"][0]["units"]
check("units are not reported off a handful of sessions", not units["available"])
check_eq("...and say how far along they are",
         units["reason"],
         f"accumulating: 1 of {se.MIN_UNIT_SESSIONS} sessions captured")
check_eq("...reporting the threshold rather than hiding it",
         units["min_sessions"], se.MIN_UNIT_SESSIONS)

# A flow row and a units row land on the SAME (etf, date) key, each carrying
# only its own half. Neither may erase the other.
with db.get_connection() as conn:
    merged = conn.execute(
        """SELECT in_flow, trust_outstanding_units FROM sector_etf_flows
           WHERE etf_code = 'US.SMH' AND trust_outstanding_units IS NOT NULL"""
    ).fetchone()
check("a units snapshot does NOT erase that day's flow",
      merged is not None and merged["in_flow"] is not None,
      "the two arrive from different calls on the same key; without COALESCE "
      "on both sides the snapshot's NULL silently overwrote a real day")

# Enough sessions, and it reports a real creation/redemption. Cleared first so
# the fixture is not measured against the ingest rows above.
with db.get_connection() as conn:
    conn.execute("DELETE FROM sector_etf_flows")
rows = [
    {"etf_code": "US.SMH", "flow_date": f"2026-07-{d:02d}",
     "trust_outstanding_units": 1.0e8 + d * 1.0e5, "last_price": 100.0,
     "in_flow": 1.0, "main_in_flow": 1.0}
    for d in range(1, se.MIN_UNIT_SESSIONS + 1)
]
db.upsert_etf_flows(rows)
flow2 = se.flow_for_plate("US.LIST2015", days=60)
smh = next(e for e in flow2["etfs"] if e["code"] == "US.SMH")
check("with enough sessions the unit change is reported", smh["units"]["available"])
check_close("...as the change in shares outstanding",
            smh["units"]["unit_change"], 1.9e6, abs_tol=1.0)
check_close("...valued at the latest price",
            smh["units"]["estimated_flow"], 1.9e8, abs_tol=100.0)
check("...and labelled as what it actually is",
      "net creation/redemption" in smh["units"]["note"])

report("sector etfs")
