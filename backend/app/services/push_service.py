"""Turn deterministic position alerts into push notifications.

The hard part was already built. `alerts.py` (decisions #53) produces alerts
that are deterministic, severity-ranked, and — crucially — carry a stable
fingerprint `rule:code:discriminator` whose discriminator is "whatever makes
this a DIFFERENT fact". That is exactly the property a notifier needs, so this
module adds no rules and changes no thresholds. It decides only *whether this
fact has been said yet*.

## Why build_alerts() and not get_alerts()

`get_alerts` is the dashboard's function and it truncates to
`alerts_max_rendered` (6), because past that a glance stops being a glance. A
notifier built on it would silently never push the seventh alert — and the
seventh alert on a bad day is not the least important one. So this calls
`build_alerts()` directly and applies the ack filter itself.

## Why this job does not take _scan_lock

`_scan_lock` is the QUOTE gateway's mutex. `list_positions()` goes through
`moomoo_trade_gateway`, a separate OpenSecTradeContext with its own bounded
lock (decisions #21), which the scanner never touches. The dashboard already
proves this is safe: routers/alerts.py, routers/positions.py and
routers/health.py all make this same call with no lock, every 30 seconds,
including in the middle of a pre-market scan.

Taking the lock would be actively harmful. A pre-market scan holds it for over
an hour (decisions #20), so every alert raised during that hour — the hour
right before the market opens — would be silently withheld. Silence is the one
failure mode a safety feature cannot have.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db
from app.config import settings
from app.services import alerts as alerts_service
from app.services import web_push

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}


def passes_severity(severity: str) -> bool:
    """Whether this severity clears the configured push floor.

    Public, because the cloud deployment's own delivery cycle applies the same
    floor and a private name imported across a package boundary is the worst of
    both worlds — decisions #63 made this exact call for `db.now_iso` and
    `prompt_blocks.age_label`, and cloud #28e made it again for
    `routers/setups.hydrate_setup`. Both deployments must agree on what is
    worth waking somebody for; two copies of this expression would be two
    things to forget to change.
    """
    floor = _SEVERITY_RANK.get(settings.push_min_severity, 1)
    return _SEVERITY_RANK.get(severity, 9) <= floor


def to_notification(alert: dict[str, Any]) -> dict[str, Any]:
    """The payload the service worker renders.

    `tag` is the fingerprint so the OS COLLAPSES a repeat rather than stacking
    it — belt and braces alongside the alert_pushes dedup, since a phone that
    was offline can be handed the same message twice by the push service.

    `url` comes straight from the alert; every one already carries
    href=/ticker/{code} for the dashboard's own click-through.
    """
    return {
        "title": alert["title"],
        "body": alert["detail"],
        "tag": alert["id"],
        "severity": alert["severity"],
        "url": alert.get("href") or "/",
    }


def pending_alerts(trade_gateway) -> list[dict[str, Any]]:
    """Alerts that are worth pushing and have not been pushed yet."""
    try:
        positions = trade_gateway.list_positions()
    except Exception as exc:                                     # noqa: BLE001
        # Mirrors get_alerts: no position data means we cannot see anything,
        # which is NOT the same as nothing being wrong. Push nothing.
        logger.info("push: no position data, skipping cycle (%s)", exc)
        return []

    alerts = alerts_service.build_alerts(positions)
    if not alerts:
        return []

    acked = db.active_alert_acks()
    already = db.pushed_fingerprints(settings.push_retention_days)

    return [
        a for a in alerts
        if passes_severity(a["severity"])
        and a["id"] not in acked          # silenced in the UI stays silenced
        and a["id"] not in already        # this exact fact was already sent
    ]


def deliver(alert: dict[str, Any]) -> dict[str, int]:
    """Send one alert to every subscription. Returns a small tally.

    The fingerprint is recorded by the CALLER, and only when at least one send
    succeeded — recording on total failure would swallow the alert forever.
    """
    payload = to_notification(alert)
    tally = {"sent": 0, "gone": 0, "failed": 0}

    for sub in db.list_push_subscriptions():
        outcome = web_push.send_one(sub, payload)
        tally[outcome] += 1

        if outcome == "gone":
            # The push service says this subscription no longer exists. That is
            # an answer, not an error — the PWA was uninstalled or the browser
            # rotated it. Retrying forever would be pure noise.
            db.delete_push_subscription(sub["endpoint"])
        elif outcome == "failed":
            n = db.record_push_failure(sub["endpoint"])
            if n >= settings.push_max_failures:
                logger.warning("push: retiring %s after %d consecutive failures",
                               sub["endpoint"][:48], n)
                db.delete_push_subscription(sub["endpoint"])
        else:
            db.record_push_success(sub["endpoint"])

    return tally


def run_push_cycle(trade_gateway) -> dict[str, Any]:
    """One pass. Never raises — it runs on a scheduler."""
    result: dict[str, Any] = {
        "subscriptions": 0, "considered": 0, "pushed": 0,
        "sent": 0, "gone": 0, "failed": 0, "skipped_reason": None,
    }

    if not web_push.configured():
        result["skipped_reason"] = "VAPID keys not configured"
        return result

    subs = db.list_push_subscriptions()
    result["subscriptions"] = len(subs)
    if not subs:
        # Nobody has enabled notifications. Do not touch OpenD for nothing.
        result["skipped_reason"] = "no subscriptions"
        return result

    try:
        pending = pending_alerts(trade_gateway)
    except Exception as exc:                                     # noqa: BLE001
        logger.exception("push: cycle failed")
        result["skipped_reason"] = str(exc)
        return result

    result["considered"] = len(pending)

    for alert in pending:
        tally = deliver(alert)
        result["sent"] += tally["sent"]
        result["gone"] += tally["gone"]
        result["failed"] += tally["failed"]

        if tally["sent"] > 0:
            db.record_alert_push(alert["id"], alert["rule"],
                                 alert["code"], alert["severity"])
            result["pushed"] += 1
        else:
            # Left unrecorded on purpose, so the next cycle tries again.
            logger.warning("push: no delivery for %s, will retry", alert["id"])

    if result["pushed"]:
        logger.info("push: %d alert(s) to %d subscription(s)",
                    result["pushed"], len(subs))
    return result
