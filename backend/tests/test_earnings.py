"""Earnings window splitting, row normalisation, and the outlook validator.

No network. The live behaviour these encode — AU rejected, an 8-day window
rejected, `security` arriving as "US.PDD" — was confirmed against the real
server before these were written; the point here is that the code keeps
honouring it.
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import earnings_service as es  # noqa: E402
from app.services.moomoo_gateway import GatewayError  # noqa: E402

from tests.harness import check, report  # noqa: E402


# --- window splitting ---------------------------------------------------
start = dt.date(2026, 8, 24)
w = es.windows(start, 14)
check("a 14-day horizon becomes several calls", len(w) == 3, str(w))
check("no window exceeds the server's 7-day limit",
      all((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
          < es.MAX_WINDOW_DAYS for a, b in w),
      "the server answers 'Date range must not exceed 7 days'")
check("windows are contiguous with no gap and no overlap",
      all(dt.date.fromisoformat(w[i + 1][0])
          - dt.date.fromisoformat(w[i][1]) == dt.timedelta(days=1)
          for i in range(len(w) - 1)), str(w))
check("the first window starts on the start date", w[0][0] == start.isoformat())
check("the last window ends exactly on the horizon",
      w[-1][1] == (start + dt.timedelta(days=14)).isoformat(), w[-1][1])
check("a horizon inside one window is a single call", len(es.windows(start, 6)) == 1)


# --- 'N/A' handling -----------------------------------------------------
check("'N/A' becomes None, not a crash and not 0.0", es._f("N/A") is None)
check("an empty string becomes None", es._f("  ") is None)
check("NaN becomes None", es._f(float("nan")) is None)
check("a real number survives", es._f("2.0612") == 2.0612)
check("zero is kept, not treated as missing", es._f(0) == 0.0)


# --- normalisation ------------------------------------------------------
row = {"security": "US.NVDA", "name": "NVIDIA", "earnings_date": "2026-08-26",
       "pub_type": "AFTER", "period_text": "2027Q2", "eps_predict": "2.0612",
       "eps_actual": "N/A", "iv_rank": 39.491, "price": 180.0}
n = es._normalise(row, "US.NVDA", "US")
check("pub_type passes through when it is one of the known values",
      n["pub_type"] == "AFTER")
check("an unpublished actual is None", n["eps_actual"] is None)
check("the calendar's price lands in price_at_fetch, not a quote field",
      n["price_at_fetch"] == 180.0 and "last_price" not in n,
      "it is not a quote and must never be rendered as one")
check("an unrecognised pub_type degrades to UNKNOWN rather than failing the CHECK",
      es._normalise({**row, "pub_type": "SOMETIME"}, "US.NVDA", "US")["pub_type"]
      == "UNKNOWN")
check("a row with no date is dropped",
      es._normalise({**row, "earnings_date": ""}, "US.NVDA", "US") is None)
check("a datetime-shaped date is trimmed to the day",
      es._normalise({**row, "earnings_date": "2026-08-26 00:00:00"},
                    "US.NVDA", "US")["earnings_date"] == "2026-08-26")


# --- per-market and per-window degradation ------------------------------
class FakeGateway:
    """Fails AU the way the real server does, and one US window as well."""

    def __init__(self):
        self.calls = []

    def get_earnings_calendar(self, market, begin, end):
        self.calls.append((market, begin, end))
        if market == "AU":
            raise GatewayError("get_earnings_calendar returned error: Invalid "
                               "market type, supported: HK/US/CNSH/CNSZ/SG/JP")
        # Fail the SECOND US window, whichever it happens to be. This used to
        # key on `begin.endswith("-31")`, which made the test depend on
        # today's date: a 14-day horizon is split into 7-day windows from
        # today, so a window only began on the 31st for a few days a month and
        # the check silently passed on every other day. Counting calls is the
        # same scenario without the calendar dependency.
        if len([c for c in self.calls if c[0] == market]) == 2:
            raise GatewayError("get_earnings_calendar returned error: transient")
        return [
            {"security": "US.NVDA", "name": "NVIDIA", "earnings_date": "2026-08-26",
             "pub_type": "AFTER", "eps_predict": 2.06, "price": 180.0},
            # A whole-market feed carries hundreds of names the watchlist
            # does not hold; they must not be stored.
            {"security": "US.ZZZZ", "name": "Not Watched",
             "earnings_date": "2026-08-27", "pub_type": "BEFORE"},
        ]


import app.db as db  # noqa: E402

_real_enabled, _real_upsert = db.get_enabled_tickers, db.upsert_earnings
db.get_enabled_tickers = lambda *a, **k: [
    {"code": "US.NVDA", "name": "NVIDIA", "market": "US"},
    {"code": "AU.CSL", "name": "CSL Ltd", "market": "AU"},
]
written = {}
db.upsert_earnings = lambda rows, **k: (
    written.update({"rows": rows}) or {"inserted": len(rows), "updated": 0, "pruned": 0}
)
es.db = db
try:
    gw = FakeGateway()
    res = es.refresh(gw, horizon_days=14)
finally:
    db.get_enabled_tickers, db.upsert_earnings = _real_enabled, _real_upsert

check("AU is reported as a structured skip, never as a failure",
      "AU" in res["skipped_markets"] and "Invalid market type" in res["skipped_markets"]["AU"],
      str(list(res["skipped_markets"])))
check("AU is never even queried, since the answer is known",
      not any(m == "AU" for m, _, _ in gw.calls), str(gw.calls))
check("one failing window does not cost the others",
      res["events"] == 1 and any("transient" in v for v in res["skipped_markets"].values()),
      f"events={res['events']} skipped={list(res['skipped_markets'])}")
check("tickers that are not on the watchlist are dropped",
      {r["code"] for r in written["rows"]} == {"US.NVDA"},
      "a whole-market feed is hundreds of names")


# --- the outlook validator ----------------------------------------------
good = {
    "headline": "NVDA reports on Aug 26; guidance is the swing factor.",
    "what_to_watch": ["Guidance", "Pricing", "Supply"],
    "news_summary": "Two sentences. Here is the second.",
    "uncertainty": "Wrong if a factor absent from the headlines drives it.",
}
check("a well-formed outlook validates", es.validate_outlook(dict(good))["headline"])


def rejects(label, payload, why):
    try:
        es.validate_outlook(payload)
        check(label, False, "was accepted")
    except es.OutlookError as exc:
        check(label, True, f"{why}: {exc}")


rejects("a missing key is rejected",
        {k: v for k, v in good.items() if k != "uncertainty"}, "missing")
rejects("an EXTRA key is rejected, not ignored",
        {**good, "conviction_score": 7},
        "an outlook must not look like a thesis")
rejects("only two what_to_watch items is rejected",
        {**good, "what_to_watch": ["a", "b"]}, "needs 3-5")
rejects("six what_to_watch items is rejected",
        {**good, "what_to_watch": ["a", "b", "c", "d", "e", "f"]}, "needs 3-5")
rejects("a string where a list belongs is rejected, not split",
        {**good, "what_to_watch": "Guidance, Pricing, Supply"}, "wrong type")
rejects("an over-long headline is rejected rather than truncated",
        {**good, "headline": "x" * 200}, "max 160")
rejects("an empty string is rejected", {**good, "news_summary": "   "}, "empty")
rejects("a list item that is not a string is rejected",
        {**good, "what_to_watch": ["a", 2, "c"]}, "wrong type")
rejects("a bare list instead of an object is rejected", [], "not an object")

check("the schema deliberately has no thesis fields",
      not (es.REQUIRED_KEYS & {"conviction_score", "trade_direction",
                               "suggested_entry", "suggested_stop",
                               "suggested_target"}),
      "an unvalidated opinion must not sit beside a validated one looking comparable")

report("earnings")
