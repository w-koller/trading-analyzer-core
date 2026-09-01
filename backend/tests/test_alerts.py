"""Alert rules at their boundaries, the two suppressions, and fingerprints.

Every threshold is checked at, just below and just above, because an
off-by-one in a comparison here is invisible: the alert simply does not
appear, and nothing says it should have.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import alerts  # noqa: E402

from tests.harness import check, report  # noqa: E402


def iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds")


def pos(**kw):
    base = {"code": "US.PLTR", "name": "Palantir", "qty": 40.0, "avg_cost": 172.4,
            "last_price": 175.0, "unrealized_pnl_pct": 1.5,
            "unrealized_pnl": 100.0, "currency": "USD", "market": "US"}
    return {**base, **kw}


def setup(**kw):
    base = {"id": 412, "trade_direction": "Bullish", "conviction_score": 6,
            "suggested_stop": 170.0, "suggested_target": 190.0,
            "created_at": iso(1), "is_delayed_data": 0,
            "data_as_of": iso(1), "reasoning": "A. B. C."}
    return {**base, **kw}


def rules(p, s):
    got = alerts._thesis_rules(p, s)
    return {a["rule"] for a in got}, got


# --- stop_breached -------------------------------------------------------
names, _ = rules(pos(last_price=169.99), setup())
check("a price below the stop breaches it", "stop_breached" in names, str(names))
names, got = rules(pos(last_price=170.0), setup())
check("a price exactly AT the stop breaches it (<=, not <)",
      "stop_breached" in names, str(names))
names, _ = rules(pos(last_price=170.01), setup())
check("a price just above the stop does not", "stop_breached" not in names, str(names))

names, _ = rules(pos(last_price=175.0), setup(trade_direction="Bearish",
                                              suggested_stop=174.0,
                                              suggested_target=160.0))
check("a Bearish stop breaches upward, not downward", "stop_breached" in names, str(names))

names, _ = rules(pos(last_price=100.0), setup(trade_direction="Neutral"))
check("a Neutral thesis never breaches — a stop with no side cannot be crossed",
      "stop_breached" not in names, str(names))

names, _ = rules(pos(last_price=100.0), setup(suggested_stop=None))
check("no stop on the thesis means no stop alert", "stop_breached" not in names)
names, _ = rules(pos(last_price=None), setup())
check("a missing price yields no alert rather than a crash",
      "stop_breached" not in names)

stale = settings.alerts_setup_stale_days
names, _ = rules(pos(last_price=100.0), setup(created_at=iso(stale - 0.1)))
check(f"a setup just under {stale}d old still fires", "stop_breached" in names)
names, _ = rules(pos(last_price=100.0), setup(created_at=iso(stale + 1)))
check(f"a setup older than {stale}d is ignored entirely",
      names == set(), "a stop from a two-week-old thesis is not a level anyone trades")

_, got = rules(pos(last_price=100.0), setup(is_delayed_data=1))
check("a delayed quote says the cross is provisional (rule #7)",
      "provisional" in got[0]["detail"], got[0]["detail"])


# --- target_reached ------------------------------------------------------
names, got = rules(pos(last_price=190.0), setup())
check("a price at the target reaches it", "target_reached" in names, str(names))
check("target_reached is warn, not critical — good news is not time-critical",
      next(a for a in got if a["rule"] == "target_reached")["severity"] == "warn")
names, _ = rules(pos(last_price=189.99), setup())
check("just below the target does not", "target_reached" not in names)


# --- thesis_contradicts_position ----------------------------------------
floor = settings.alerts_contradiction_min_conviction
names, _ = rules(pos(), setup(trade_direction="Bearish", conviction_score=floor,
                              suggested_stop=200.0, suggested_target=150.0))
check(f"a Bearish {floor}/10 against a holding fires",
      "thesis_contradicts_position" in names, str(names))
names, _ = rules(pos(), setup(trade_direction="Bearish", conviction_score=floor - 1,
                              suggested_stop=200.0, suggested_target=150.0))
check(f"a Bearish {floor - 1}/10 does not — a low-conviction bear is a shrug",
      "thesis_contradicts_position" not in names, str(names))
names, _ = rules(pos(qty=0), setup(trade_direction="Bearish", conviction_score=9,
                                   suggested_stop=200.0, suggested_target=150.0))
check("no holding means no contradiction", "thesis_contradicts_position" not in names)


# --- drawdown ------------------------------------------------------------
warn, crit = settings.alerts_drawdown_warn_pct, settings.alerts_drawdown_critical_pct
d = alerts._drawdown_rule(pos(unrealized_pnl_pct=warn + 0.01), False)
check(f"just better than {warn}% does not alert", d == [], str(d))
d = alerts._drawdown_rule(pos(unrealized_pnl_pct=warn), False)
check(f"exactly {warn}% alerts as warn", d and d[0]["severity"] == "warn", str(d))
d = alerts._drawdown_rule(pos(unrealized_pnl_pct=crit), False)
check(f"exactly {crit}% escalates to critical",
      d and d[0]["severity"] == "critical", str(d))
d = alerts._drawdown_rule(pos(unrealized_pnl_pct=crit + 0.01), False)
check(f"just better than {crit}% stays warn", d and d[0]["severity"] == "warn")
check("a missing P/L yields nothing, not a zero",
      alerts._drawdown_rule(pos(unrealized_pnl_pct=None), False) == [])


# --- suppression ---------------------------------------------------------
p = pos(last_price=160.0, unrealized_pnl_pct=-20.0)
found = alerts._thesis_rules(p, setup())
breached = any(a["rule"] == "stop_breached" for a in found)
check("a deep drawdown alone would alert",
      alerts._drawdown_rule(p, suppressed=False) != [])
check("but it is suppressed when the stop is already breached",
      breached and alerts._drawdown_rule(p, suppressed=breached) == [],
      "one fact, one alert — stop_breached already says it louder")


# --- earnings ------------------------------------------------------------
def ev(days, **kw):
    d = (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
    return {"earnings_date": d, "pub_type": "AFTER", "iv_rank": 65.0, **kw}


def erules(days, s=None, **kw):
    got = alerts._earnings_rules(pos(), ev(days, **kw), s)
    return {a["rule"] for a in got}, got

n, g = erules(0)
check("reporting today is critical", g and g[0]["severity"] == "critical", str(g))
n, g = erules(1)
check("reporting tomorrow is critical", g and g[0]["severity"] == "critical")
n, g = erules(settings.alerts_earnings_warn_days)
check(f"reporting in {settings.alerts_earnings_warn_days}d is warn",
      g and g[0]["severity"] == "warn", str(g))
n, g = erules(settings.alerts_earnings_warn_days + 1)
check("further out than the window is not an alert", g == [], str(g))

old = setup(created_at=iso(5))
n, g = erules(-1, old)
check("a report that has happened since the thesis raises an info alert",
      "earnings_passed_unreviewed" in n, str(n))
n, g = erules(-1, setup(created_at=iso(0)))
check("a thesis written AFTER the report does not",
      g == [], "the analysis has already seen the result")
n, g = erules(-5, old)
check("a report from a week ago is not raised — the window is 3 days", g == [])


# --- shock news ----------------------------------------------------------
arts = [{"id": 9, "title": "Big filing", "source_label": "SEC EDGAR",
         "match_basis": "company_name", "url": "u"},
        {"id": 8, "title": "Another", "source_label": "CNBC",
         "match_basis": "feed_query", "url": "u"},
        {"id": 7, "title": "Third", "source_label": "Yahoo",
         "match_basis": "feed_query", "url": "u"}]
s = alerts._shock_rule(pos(), arts)
check("three shock articles produce exactly ONE alert", len(s) == 1,
      "a busy filing day is the likeliest fatigue source here")
check("the newest headline is shown with a count of the rest",
      "Big filing" in s[0]["detail"] and "+2 more" in s[0]["detail"], s[0]["detail"])
check("the match basis is stated, so a wrong link is legible",
      "linked by company name" in s[0]["detail"])
check("the fingerprint uses the newest article id",
      s[0]["id"].endswith(":9"), s[0]["id"])
check("no articles means no alert", alerts._shock_rule(pos(), []) == [])


# --- fingerprints --------------------------------------------------------
a1 = alerts._thesis_rules(pos(last_price=160.0), setup())[0]
a2 = alerts._thesis_rules(pos(last_price=161.0), setup())[0]
check("the same fact keeps the same fingerprint across polls",
      a1["id"] == a2["id"], f"{a1['id']} vs {a2['id']}")
check("the fingerprint is rule:code:discriminator",
      a1["id"] == "stop_breached:US.PLTR:412", a1["id"])
a3 = alerts._thesis_rules(pos(last_price=160.0), setup(id=431))[0]
check("a NEW thesis breaching produces a new fingerprint, so it re-fires",
      a1["id"] != a3["id"], f"{a1['id']} vs {a3['id']}")

d_warn = alerts._drawdown_rule(pos(unrealized_pnl_pct=-9.0), False)[0]
d_same = alerts._drawdown_rule(pos(unrealized_pnl_pct=-12.0), False)[0]
d_worse = alerts._drawdown_rule(pos(unrealized_pnl_pct=-16.0), False)[0]
check("drawdown keeps one fingerprint while it stays in the same band",
      d_warn["id"] == d_same["id"], f"{d_warn['id']} vs {d_same['id']}")
check("crossing into critical is a new fingerprint, so it re-alerts once",
      d_warn["id"] != d_worse["id"], f"{d_warn['id']} vs {d_worse['id']}")


# --- the unavailable path -----------------------------------------------
class DeadGateway:
    def list_positions(self):
        raise RuntimeError("trade session not logged in")


out = alerts.get_alerts(DeadGateway())
check("a dead trade session reports available:false, never an empty all-clear",
      out["available"] is False and out["alerts"] == [],
      "rendering 'nothing wrong' from 'we cannot see anything' is a dangerous lie")
check("and it says why", "trade session" in (out["reason"] or ""), str(out["reason"]))


# --- ack TTLs ------------------------------------------------------------
check("a critical ack expires sooner than a warn one",
      alerts.ttl_for("critical") < alerts.ttl_for("warn"),
      "it matters more, so it comes back sooner")

fp = "test_rule:US.TEST:1"
db.acknowledge_alert(fp, "test_rule", "US.TEST", "warn", 1.0)
check("an ack is stored and active", fp in db.active_alert_acks())
db.acknowledge_alert(fp, "test_rule", "US.TEST", "warn", -1.0)
check("an expired ack is pruned on read", fp not in db.active_alert_acks(),
      "a fact still true tomorrow must be raised again")
db.unacknowledge_alert(fp)

report("alerts")
