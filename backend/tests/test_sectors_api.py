"""Checks for the /sectors endpoints.

Run from backend/:  .venv/bin/python -m tests.test_sectors_api

Three things need HTTP to test honestly and are the reason this suite uses
TestClient the way `test_auth` does:

  1. **Route ordering.** `/sectors/{plate_code}` is a catch-all registered
     alongside `/sectors/rotation`, `/sectors/pairs` and `/sectors/etfs`.
     FastAPI matches in registration order, so a reorder would silently turn
     "rotation" into a plate code and return "unknown plate" forever — a 200
     with a plausible body, which is the worst kind of regression.
  2. **Bounds.** Every out-of-range parameter must be an explicit 400, never
     a silent clamp.
  3. **The 409 contract**, which the UI renders as a wait rather than a
     failure.

Offline: temp database, no gateway, no network.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="sectors-api-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import sector_flow  # noqa: E402
from tests.harness import check, check_eq, report  # noqa: E402

# An empty API_TOKEN plus no session would 401 everything; the auth surface
# has its own suite, so this one authenticates with the shared secret.
settings.api_token = "test-token"
client = TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": "test-token"})


def seed():
    db.upsert_sector_plates([
        {"plate_code": f"US.LIST{2000 + i}", "market": "US",
         "plate_name": f"Sector {i}", "plate_class": "INDUSTRY" if i % 2 else "CONCEPT",
         "plate_id": f"LIST{2000 + i}", "sector_group": f"Sector {i}",
         "constituent_count": 50}
        for i in range(12)
    ])
    for i in range(12):
        db.upsert_rotation_scores([{
            "plate_code": f"US.LIST{2000 + i}", "as_of_date": "2026-08-28",
            "window_days": 5, "score": round(0.5 - i * 0.1, 4),
            "components": {"rel_return": 0.5 - i * 0.1}, "sessions_used": 10,
            "constituents": 50, "coverage": 1.0, "sufficient": True,
        }])


# --- route ordering: the specific GETs must not fall to the catch-all ------

seed()
r = client.get("/sectors/rotation")
check_eq("/sectors/rotation is the board, not a plate lookup", r.status_code, 200)
check("...and returns board keys, not detail keys",
      "inflow" in r.json() and "plate_code" not in r.json(),
      "if the catch-all shadowed it this would be 'unknown plate' with a 200")

r = client.get("/sectors/pairs")
check_eq("/sectors/pairs resolves to the pairs route", r.status_code, 200)
check("...returning a pairs body", "pairs" in r.json())
check("...that reports availability rather than an ambiguous empty list",
      "available" in r.json() and "coverage" in r.json(),
      "an empty list would read as 'nothing is rotating' when the truth is "
      "'member lists are still being fetched'")

r = client.get("/sectors/etfs")
check_eq("/sectors/etfs resolves to the ETF route", r.status_code, 200)
check("...returning an etfs body", "etfs" in r.json())

r = client.get("/sectors/US.LIST2001")
check_eq("a real plate code still reaches the detail route", r.status_code, 200)
check_eq("...and resolves", r.json()["available"], True)
check_eq("...as the right plate", r.json()["plate_code"], "US.LIST2001")


# --- the universe endpoint ------------------------------------------------

r = client.get("/sectors")
body = r.json()
check_eq("GET /sectors returns the universe", r.status_code, 200)
check_eq("...counting by class", body["counts"]["total"], 12)
check("...reporting how stale the universe is",
      "universe_age_days" in body and "universe_max_age_days" in body)
check("...and how many plates the member refresh has not reached",
      body["members_unvisited"] == 0,
      "a 0-constituent plate is UNVISITED, not an empty sector, and is "
      "counted separately so the UI can say which")


# --- bounds are explicit 400s, never silent clamps ------------------------

check_eq("an unknown window is a 400", client.get("/sectors/rotation?window=7").status_code, 400)
check("...naming the legal values",
      str(list(sector_flow.WINDOWS)) in client.get("/sectors/rotation?window=7").json()["detail"])
check_eq("every real window is accepted",
         [client.get(f"/sectors/rotation?window={w}").status_code for w in sector_flow.WINDOWS],
         [200] * len(sector_flow.WINDOWS))
check_eq("top_n of 0 is a 400", client.get("/sectors/rotation?top_n=0").status_code, 400)
check_eq("top_n of 51 is a 400", client.get("/sectors/rotation?top_n=51").status_code, 400)
check_eq("an unknown plate_class is a 400",
         client.get("/sectors/rotation?plate_class=OTHER").status_code, 400)
check_eq("a real plate_class is accepted",
         client.get("/sectors/rotation?plate_class=INDUSTRY").status_code, 200)
check_eq("pairs top_n is bounded too", client.get("/sectors/pairs?top_n=21").status_code, 400)
check_eq("etf days is bounded", client.get("/sectors/etfs?days=91").status_code, 400)
check_eq("an unknown window on detail is a 400",
         client.get("/sectors/US.LIST2001?window=7").status_code, 400)


# --- degradation is a 200 with a reason, never a 502 or a fake zero -------

r = client.get("/sectors/rotation?market=AU")
check_eq("an unsupported market is a 200, not a 502", r.status_code, 200)
check_eq("...marked unavailable", r.json()["available"], False)
check("...with a reason naming what IS supported", "supported: US" in r.json()["reason"])
check_eq("...and no fabricated rows", (r.json()["inflow"], r.json()["outflow"]), ([], []))

r = client.get("/sectors/US.NOPE")
check_eq("an unknown plate is a 200", r.status_code, 200)
check_eq("...marked unavailable", r.json()["available"], False)
check_eq("...with a reason", r.json()["reason"], "unknown plate")

r = client.get("/sectors/rotation?window=63")
check_eq("a window with no scores is still a 200", r.status_code, 200)
check_eq("...marked unavailable rather than empty-and-silent",
         r.json()["available"], False)
check("...saying the refresh has not run for it", "no scores yet" in r.json()["reason"])


# --- the board's own contract ---------------------------------------------

board = client.get("/sectors/rotation?window=5&top_n=3").json()
check_eq("inflow is capped at top_n", len(board["inflow"]), 3)
check("inflow leads with the strongest score",
      board["inflow"][0]["score"] >= board["inflow"][1]["score"])
check("outflow leads with the weakest",
      board["outflow"][0]["score"] <= board["outflow"][1]["score"])
check("the board ships the thresholds so the UI need not hardcode them",
      {"min_constituents", "min_sessions"} <= set(board),
      "a second copy of a rule in TypeScript is a second thing to forget")
check("...and states what its zero means",
      "median sector" in board["baseline"],
      "calling it 'the market' would be a different and wrong claim")

pairs = client.get("/sectors/pairs").json()
check("the pairs body carries its own disclaimer",
      "not a dollar traced from one to the other" in pairs["note"],
      "this is the caption that keeps a correlation from reading as a flow")


# --- POST /refresh: the 409 the UI renders as a wait ----------------------

check_eq("refresh rejects an unsupported market",
         client.post("/sectors/refresh?market=AU").status_code, 400)

scheduler._scan_lock.acquire()
try:
    r = client.post("/sectors/refresh")
    check_eq("refresh 409s while a scan holds the gateway", r.status_code, 409)
    check("...with a detail the UI can render as a wait, not a failure",
          "can wait for it" in r.json()["detail"])
finally:
    scheduler._scan_lock.release()
check("the lock is released after the 409, not leaked",
      not scheduler._scan_lock.locked())


# --- narratives -----------------------------------------------------------

r = client.get("/sectors/US.LIST2001/narrative")
check_eq("a sector with no narrative is a 200", r.status_code, 200)
check_eq("...marked unavailable", r.json()["available"], False)
check("...with a reason, so the UI renders nothing rather than a shell",
      "no narrative written" in r.json()["reason"], r.json()["reason"])

db.upsert_sector_narrative(
    plate_code="US.LIST2001", as_of_date="2026-08-28", window_days=5,
    headline="A headline.", candidate_driver="One. Two.",
    supporting_headlines=["Fed holds rates steady"],
    contradicts="Nothing does.", confidence_label="news is consistent",
    model="test-model", sources={"ticker_news": 0},
)
r = client.get("/sectors/US.LIST2001/narrative")
body = r.json()
check_eq("a written narrative is returned", body["available"], True)
check_eq("...with its citations as a list", body["supporting_headlines"],
         ["Fed holds rates steady"])
check_eq("...and the model that wrote it", body["model"], "test-model")
check("...carrying the disclaimer in the PAYLOAD, not left to the UI",
      "Interpretation, not measurement" in body["disclaimer"],
      "this is the one endpoint here whose content a model wrote, sitting "
      "beside numbers a model did not")
check("no numeric score rides along with the narrative",
      "score" not in body and "confidence" not in {k.lower() for k in body
                                                   if k != "confidence_label"},
      "the score lives on /sectors/rotation and nowhere else")

check_eq("the narrative route is not shadowed by the plate catch-all",
         client.get("/sectors/US.LIST2001/narrative").status_code, 200)
check_eq("an unknown window on the narrative route is a 400",
         client.get("/sectors/US.LIST2001/narrative?window=7").status_code, 400)

# POST /narratives/run must NOT take the scan lock — it makes no OpenD calls.
scheduler._scan_lock.acquire()
try:
    r = client.post("/sectors/narratives/run?top_n=1")
    check("narratives/run does NOT 409 while a scan holds the gateway",
          r.status_code == 200,
          f"got {r.status_code} — it reads stored rows only, so taking the "
          "quote mutex would delay a pre-market scan for nothing")
finally:
    scheduler._scan_lock.release()

check_eq("narratives/run bounds top_n",
         client.post("/sectors/narratives/run?top_n=11").status_code, 400)
check_eq("narratives/run bounds the window",
         client.post("/sectors/narratives/run?window=7").status_code, 400)
check_eq("narratives/run rejects an unsupported market",
         client.post("/sectors/narratives/run?market=AU").status_code, 400)

report("sectors api")
