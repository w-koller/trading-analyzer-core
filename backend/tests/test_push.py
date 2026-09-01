"""Checks for the alert -> push pipeline.

Run from backend/:  .venv/bin/python -m tests.test_push

The rules themselves are alerts.py's and are tested there. What matters here
is everything AROUND them, because the failure modes are all silent:

  * an alert must be pushed exactly ONCE per fingerprint, or the feature
    becomes noise and gets muted, which is the same as not having it;
  * it must still push when there are more than alerts_max_rendered alerts —
    the dashboard truncates to 6 and the seventh on a bad day is not the
    least important one;
  * a dead subscription must be dropped, not retried forever;
  * a total delivery failure must NOT be recorded, or the alert is lost.

No network: web_push.send_one is stubbed.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="push-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.config import settings                                   # noqa: E402
from app.services import push_service, web_push                   # noqa: E402

from tests.harness import check, report  # noqa: E402


def alert(rule="drawdown", code="US.PLTR", disc="crit", severity="critical"):
    return {"id": f"{rule}:{code}:{disc}", "rule": rule, "code": code,
            "severity": severity, "title": f"{code} title",
            "detail": f"{code} detail", "href": f"/ticker/{code}"}


# --- notification shape --------------------------------------------------
n = push_service.to_notification(alert())
check("tag is the fingerprint, so the OS collapses a repeat",
      n["tag"] == "drawdown:US.PLTR:crit")
check("url comes from the alert's own href", n["url"] == "/ticker/US.PLTR")
check("title and body are the alert's own strings — the model never phrases these",
      n["title"] == "US.PLTR title" and n["body"] == "US.PLTR detail")

# --- severity floor ------------------------------------------------------
settings.push_min_severity = "warn"
check("critical passes a 'warn' floor", push_service._passes_severity("critical"))
check("warn passes a 'warn' floor", push_service._passes_severity("warn"))
check("info does NOT pass a 'warn' floor", not push_service._passes_severity("info"))
settings.push_min_severity = "info"
check("info passes when the floor is lowered", push_service._passes_severity("info"))
settings.push_min_severity = "warn"

# --- delivery, with the network stubbed ----------------------------------
outcomes: dict[str, str] = {}
sent_payloads: list[dict] = []


def fake_send(sub, payload):
    sent_payloads.append(payload)
    return outcomes.get(sub["endpoint"], "sent")


web_push.send_one = fake_send
web_push.configured = lambda: True

db.upsert_push_subscription("https://push/ok", "p", "a", "Chrome")
db.upsert_push_subscription("https://push/dead", "p", "a", "Chrome")

outcomes["https://push/dead"] = "gone"
tally = push_service.deliver(alert())
check("delivers to every subscription", tally["sent"] == 1 and tally["gone"] == 1,
      str(tally))
check("a 410 'gone' subscription is DELETED, not retried",
      [s["endpoint"] for s in db.list_push_subscriptions()] == ["https://push/ok"])

# --- failure retirement --------------------------------------------------
outcomes.clear()
outcomes["https://push/ok"] = "failed"
settings.push_max_failures = 3
for i in range(2):
    push_service.deliver(alert())
check("transient failures do not delete the subscription immediately",
      len(db.list_push_subscriptions()) == 1)
push_service.deliver(alert())
check("a persistently failing subscription is retired at the threshold",
      db.list_push_subscriptions() == [])

# --- the dedup contract --------------------------------------------------
outcomes.clear()
db.upsert_push_subscription("https://push/ok", "p", "a", "Chrome")


class Gateway:
    def __init__(self, positions):
        self._p = positions

    def list_positions(self):
        return self._p


_alerts: list[dict] = []
push_service.alerts_service.build_alerts = lambda positions: _alerts

_alerts = [alert()]
r1 = push_service.run_push_cycle(Gateway([{"code": "US.PLTR"}]))
check("first cycle pushes the alert", r1["pushed"] == 1, str(r1))

r2 = push_service.run_push_cycle(Gateway([{"code": "US.PLTR"}]))
check("second cycle pushes NOTHING — same fact, already said",
      r2["pushed"] == 0 and r2["considered"] == 0, str(r2))

# A deeper drawdown is a different discriminator, i.e. a different fact.
_alerts = [alert(disc="warn"), alert(disc="crit")]
r3 = push_service.run_push_cycle(Gateway([{"code": "US.PLTR"}]))
check("a new severity bucket IS a new fact and pushes once",
      r3["pushed"] == 1, str(r3))

# --- acks suppress push --------------------------------------------------
_alerts = [alert(code="US.IBM", disc="warn", severity="warn")]
db.acknowledge_alert("drawdown:US.IBM:warn", "drawdown", "US.IBM", "warn", 72.0)
r4 = push_service.run_push_cycle(Gateway([{"code": "US.IBM"}]))
check("an alert acknowledged in the UI is not pushed", r4["pushed"] == 0, str(r4))

# --- the truncation trap -------------------------------------------------
# get_alerts() caps at alerts_max_rendered (6). If the push path used it, the
# 7th alert would never be sent. This is the check that proves it does not.
settings.alerts_max_rendered = 6
_alerts = [alert(code=f"US.T{i}", disc=f"d{i}", severity="warn") for i in range(9)]
r5 = push_service.run_push_cycle(Gateway([{"code": "US.T0"}]))
check("all 9 alerts push even though the dashboard renders only 6",
      r5["pushed"] == 9, f"pushed {r5['pushed']}")

# --- a total delivery failure must not be recorded -----------------------
outcomes["https://push/ok"] = "failed"
settings.push_max_failures = 99          # keep the subscription alive
_alerts = [alert(code="US.RETRY", disc="x", severity="warn")]
r6 = push_service.run_push_cycle(Gateway([{"code": "US.RETRY"}]))
check("nothing is recorded when no send succeeded", r6["pushed"] == 0, str(r6))
outcomes.clear()
r7 = push_service.run_push_cycle(Gateway([{"code": "US.RETRY"}]))
check("so the next cycle RETRIES it rather than losing it",
      r7["pushed"] == 1, str(r7))

# --- no subscriptions means OpenD is never touched -----------------------
for s in db.list_push_subscriptions():
    db.delete_push_subscription(s["endpoint"])


class Exploding:
    def list_positions(self):
        raise AssertionError("must not query positions with no subscribers")


r8 = push_service.run_push_cycle(Exploding())
check("with no subscribers the cycle short-circuits before touching OpenD",
      r8["skipped_reason"] == "no subscriptions", str(r8))

# --- a dead trade session pushes nothing, rather than 'all clear' --------
db.upsert_push_subscription("https://push/ok", "p", "a", "Chrome")


class Broken:
    def list_positions(self):
        raise RuntimeError("trade session down")


r9 = push_service.run_push_cycle(Broken())
check("a dead trade session pushes nothing (never 'nothing is wrong')",
      r9["pushed"] == 0, str(r9))

report("push")
