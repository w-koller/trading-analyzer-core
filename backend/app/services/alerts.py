"""Deterministic alerts on held positions.

Pure functions over stored data plus the cached position list. No model call,
no OpenD quote call, no arithmetic beyond comparing numbers that were already
computed elsewhere.

**The LLM does not phrase these, and that is a decision.** The seven rules
below are seven f-strings. A model call would cost 60-120s of GPU per
dashboard render, or a cache with its own invalidation, for text that has to
be *identical every poll* to be trustworthy — an alert that reads slightly
differently each time looks like new information, which is exactly what
causes fatigue. And the moment a model paraphrases "below the 170.00 stop"
there is a nonzero chance it writes 107.00. If a narrative is ever wanted, the
right shape is a "brief me" button that hands the already-computed list to the
model, reusing the chat plumbing.

Scoped to HELD positions throughout. A watchlist-wide engine would fire
constantly; this scoping is the discipline that stops it becoming one.

## Fingerprints

`id` is `f"{rule}:{code}:{discriminator}"` and it is the most load-bearing
detail here, because acknowledgement is keyed on it. The discriminator is
whatever makes this a *different fact*:

  stop_breached / target_reached / thesis_contradicts   the setup id
  earnings_imminent / earnings_passed_unreviewed        the earnings date
  shock_news                                            the newest article id
  drawdown                                              the severity bucket

So silencing "stop breached on setup 412" stays silenced while that is the
same fact, and re-fires when a new thesis breaches. A position sliding -9% to
-16% re-alerts once, at the new level. Keying on the ticker alone would
silence genuinely new events; a random id would never stick across a poll.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

Severity = str      # 'critical' | 'warn' | 'info'

_SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


def _age_days(iso: str | None) -> float | None:
    when = db.parse_iso(iso) if iso else None
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def _alert(rule: str, severity: Severity, code: str, name: str, title: str,
           detail: str, discriminator: Any, evidence: dict[str, Any],
           **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{rule}:{code}:{discriminator}",
        "rule": rule,
        "severity": severity,
        "code": code,
        "name": name,
        "title": title,
        "detail": detail,
        "evidence": evidence,
        "href": f"/ticker/{code}",
        **extra,
    }


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def _thesis_rules(position: dict, setup: dict | None) -> list[dict]:
    """stop_breached, target_reached, thesis_contradicts_position."""
    out: list[dict] = []
    if not setup:
        return out

    age = _age_days(setup.get("created_at"))
    if age is not None and age > settings.alerts_setup_stale_days:
        return out

    code = position["code"]
    name = position.get("name") or code
    last = position.get("last_price")
    direction = setup.get("trade_direction")
    stop = setup.get("suggested_stop")
    target = setup.get("suggested_target")
    delayed = bool(setup.get("is_delayed_data"))
    # A 15-minute-old price crossing a stop is a maybe, not a fact (rule #7).
    qualifier = " Quote is delayed, so treat the cross as provisional." if delayed else ""
    aged = f"{age:.0f}d old" if age is not None and age >= 1 else "today's"

    if last is not None and stop is not None and direction in ("Bullish", "Bearish"):
        # Neutral is skipped on purpose: a stop with no side cannot be breached.
        breached = last <= stop if direction == "Bullish" else last >= stop
        if breached:
            out.append(_alert(
                "stop_breached", "critical", code, name,
                "Past its suggested stop",
                f"Last {last:,.2f} against a {stop:,.2f} stop on the "
                f"{direction} thesis ({aged}).{qualifier}",
                setup["id"],
                {"last_price": last, "suggested_stop": stop,
                 "setup_id": setup["id"], "setup_age_days": round(age, 1) if age else 0.0,
                 "trade_direction": direction},
                is_delayed_data=delayed,
                data_as_of=setup.get("data_as_of"),
            ))

    if last is not None and target is not None and direction in ("Bullish", "Bearish"):
        reached = last >= target if direction == "Bullish" else last <= target
        if reached:
            # Warn, not critical. Good news is not time-critical, and making
            # it red would double the red count and dilute stop_breached.
            out.append(_alert(
                "target_reached", "warn", code, name,
                "Reached its suggested target",
                f"Last {last:,.2f} against a {target:,.2f} target on the "
                f"{direction} thesis ({aged}).{qualifier}",
                setup["id"],
                {"last_price": last, "suggested_target": target,
                 "setup_id": setup["id"], "trade_direction": direction},
                is_delayed_data=delayed,
                data_as_of=setup.get("data_as_of"),
            ))

    conviction = setup.get("conviction_score") or 0
    if (direction == "Bearish"
            and conviction >= settings.alerts_contradiction_min_conviction
            and (position.get("qty") or 0) > 0):
        # The conviction floor matters: a Bearish 4 is a shrug and would fire
        # on half the watchlist.
        out.append(_alert(
            "thesis_contradicts_position", "warn", code, name,
            "You hold it; the latest thesis is bearish",
            f"Conviction {conviction}/10 Bearish, written {aged}, while you "
            f"hold {position['qty']:,.4g} units.",
            setup["id"],
            {"conviction_score": conviction, "setup_id": setup["id"],
             "qty": position.get("qty")},
        ))

    return out


def _drawdown_rule(position: dict, suppressed: bool) -> list[dict]:
    pnl = position.get("unrealized_pnl_pct")
    if pnl is None or suppressed:
        # Suppressed by stop_breached on the same code — that already says it
        # louder, and two alerts for one fact is how a list stops being read.
        return []
    if pnl > settings.alerts_drawdown_warn_pct:
        return []

    severity = ("critical" if pnl <= settings.alerts_drawdown_critical_pct else "warn")
    code = position["code"]
    return [_alert(
        "drawdown", severity, code, position.get("name") or code,
        "Down materially against your cost",
        f"{pnl:,.2f}% on {position['qty']:,.4g} units at an average cost of "
        f"{position.get('avg_cost'):,.2f}"
        f"{' ' + position['currency'] if position.get('currency') else ''}.",
        # The bucket, not the percentage: -9% sliding to -16% should re-alert
        # once at the new level, not on every poll as the number moves.
        severity,
        {"unrealized_pnl_pct": pnl, "avg_cost": position.get("avg_cost"),
         "qty": position.get("qty"),
         "unrealized_pnl": position.get("unrealized_pnl")},
    )]


def _earnings_rules(position: dict, event: dict | None,
                    setup: dict | None) -> list[dict]:
    if not event:
        return []
    code = position["code"]
    name = position.get("name") or code
    try:
        d = datetime.fromisoformat(event["earnings_date"]).date()
    except ValueError:
        return []
    days = (d - datetime.now(timezone.utc).date()).days
    when = {"BEFORE": "before the open", "AFTER": "after the close",
            "REGULAR": "during the session"}.get(event.get("pub_type") or "",
                                                 "at an unstated time")

    if 0 <= days <= settings.alerts_earnings_warn_days:
        severity = "critical" if days <= 1 else "warn"
        away = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        iv = event.get("iv_rank")
        return [_alert(
            "earnings_imminent", severity, code, name,
            f"Reports {away}",
            f"{event['earnings_date']}, {when}"
            + (f". IV rank {iv:,.0f}." if iv is not None else "."),
            event["earnings_date"],
            {"earnings_date": event["earnings_date"], "days_until": days,
             "pub_type": event.get("pub_type"), "iv_rank": iv},
        )]

    # Already reported, and the stored thesis predates it — so the analysis on
    # the dashboard is quietly obsolete and nothing else says so.
    if -3 <= days < 0 and setup:
        created = db.parse_iso(setup.get("created_at"))
        if created and created.date() <= d:
            return [_alert(
                "earnings_passed_unreviewed", "info", code, name,
                "Reported since the last thesis",
                f"Reported {event['earnings_date']}; the stored thesis is from "
                f"{created.date().isoformat()} and has not seen the result.",
                event["earnings_date"],
                {"earnings_date": event["earnings_date"],
                 "setup_id": setup["id"], "setup_created_at": setup.get("created_at")},
            )]
    return []


def _shock_rule(position: dict, articles: list[dict]) -> list[dict]:
    if not articles:
        return []
    code = position["code"]
    newest = articles[0]
    more = len(articles) - 1
    basis = {"feed_query": "from its own news feed",
             "company_name": "linked by company name"}.get(
                 newest.get("match_basis") or "", "linked")
    return [_alert(
        # At most ONE per code. A busy filing day would otherwise produce six
        # alerts for one ticker, which is the likeliest fatigue source here.
        "shock_news", "warn", code, position.get("name") or code,
        "Market-shock coverage in the last day",
        f"{newest.get('source_label')}: {newest.get('title')}"
        + (f" (+{more} more)" if more > 0 else "")
        + f" — {basis}.",
        newest["id"],
        {"article_id": newest["id"], "url": newest.get("url"),
         "match_basis": newest.get("match_basis"), "also": more},
    )]


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _shock_articles_by_code(codes: list[str]) -> dict[str, list[dict]]:
    if not codes:
        return {}
    since = (datetime.now(timezone.utc)
             - timedelta(hours=settings.alerts_shock_window_hours)
             ).isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(codes))
    with db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""
            SELECT a.id, a.title, a.url, a.source_label, a.published_at,
                   t.code, t.match_basis
            FROM news_articles a
            JOIN news_article_tickers t ON t.article_id = a.id
            WHERE t.code IN ({placeholders})
              AND a.category = 'shocks'
              AND a.published_at >= ?
            ORDER BY a.published_at DESC
            """,
            (*codes, since),
        )]
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["code"], []).append(r)
    return out


def build_alerts(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every alert the held positions currently justify, most severe first."""
    codes = [p["code"] for p in positions]
    # Batched, not one lookup per position: this runs on every 60-second
    # dashboard poll AND every push cycle, and each db.get_connection() is a
    # fresh connect plus three PRAGMAs. Four queries now, whatever N is.
    history = db.get_setup_history(codes, per_code=1)
    setups = {c: (history.get(c) or [None])[0] for c in codes}
    events = db.get_next_earnings_for_codes(codes)
    # Past events for the "reported since the last thesis" rule.
    for row in db.get_upcoming_earnings(codes=codes, days_ahead=0, days_back=3):
        events.setdefault(row["code"], None)
        if events[row["code"]] is None:
            events[row["code"]] = row
    shocks = _shock_articles_by_code(codes)

    alerts: list[dict[str, Any]] = []
    for position in positions:
        code = position["code"]
        setup = setups.get(code)
        found = _thesis_rules(position, setup)
        breached = any(a["rule"] == "stop_breached" for a in found)
        found += _drawdown_rule(position, suppressed=breached)

        earnings = _earnings_rules(position, events.get(code), setup)
        # earnings_passed_unreviewed is suppressed by earnings_imminent on the
        # same code — one fact, one alert.
        if any(a["rule"] == "earnings_imminent" for a in earnings):
            earnings = [a for a in earnings if a["rule"] == "earnings_imminent"]
        found += earnings

        found += _shock_rule(position, shocks.get(code, []))
        alerts.extend(found)

    alerts.sort(key=lambda a: (_SEVERITY_ORDER.get(a["severity"], 9), a["code"]))
    return alerts


def get_alerts(trade_gateway) -> dict[str, Any]:
    """The full payload. Never raises."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        positions = trade_gateway.list_positions()
    except Exception as exc:                                  # noqa: BLE001
        # Mirrors /positions. Rendering an empty all-clear while the trade
        # session is dead is an actively dangerous lie — the user would read
        # "nothing wrong" from "we cannot see anything".
        logger.info("alerts: no position data (%s)", exc)
        return {
            "available": False,
            "reason": f"The trade session is unavailable, so holdings cannot "
                      f"be checked: {exc}",
            "alerts": [], "counts": {}, "acknowledged_count": 0,
            "truncated": 0, "generated_at": now,
        }

    alerts = build_alerts(positions)
    acks = db.active_alert_acks()

    for a in alerts:
        a["acknowledged"] = a["id"] in acks
        a["acknowledged_until"] = acks.get(a["id"])

    live = [a for a in alerts if not a["acknowledged"]]
    counts = {s: sum(1 for a in live if a["severity"] == s)
              for s in ("critical", "warn", "info")}
    cap = settings.alerts_max_rendered

    return {
        "available": True,
        "reason": None,
        "alerts": live[:cap],
        "counts": counts,
        "acknowledged_count": len(alerts) - len(live),
        "truncated": max(len(live) - cap, 0),
        "generated_at": now,
    }


def ttl_for(severity: str) -> float:
    return (settings.alerts_ack_ttl_critical_hours if severity == "critical"
            else settings.alerts_ack_ttl_hours)
