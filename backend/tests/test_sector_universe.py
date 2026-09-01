"""Checks for the sector taxonomy and the ETF-safe plate lookup.

Run from backend/:  .venv/bin/python -m tests.test_sector_universe

The load-bearing ones are the taxonomy rule (membership in the enumeration,
NOT `plate_type`) and the get_owner_plate ETF fallback, because both encode a
failure the live server actually produces and neither is obvious from the
call signature.

Offline: temp database, fake gateway, no network, no model.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="sector-universe-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import sector_universe as su  # noqa: E402
from app.services.sdk_gateway import RateLimiter  # noqa: E402
from tests.harness import check, check_eq, report  # noqa: E402


# --- fakes -----------------------------------------------------------------

INDUSTRY_ROWS = [
    {"code": "US.LIST2015", "plate_name": "Semiconductors", "plate_id": "LIST2015"},
    {"code": "US.LIST2508", "plate_name": "Software - Infrastructure", "plate_id": "LIST2508"},
    {"code": "US.LIST2509", "plate_name": "Software - Application", "plate_id": "LIST2509"},
    {"code": "US.LIST2224", "plate_name": "Oil & Gas Integrated", "plate_id": "LIST2224"},
]
CONCEPT_ROWS = [
    {"code": "US.LIST2136", "plate_name": "Artificial Intelligence", "plate_id": "LIST2136"},
    {"code": "US.LIST2548", "plate_name": "AI Chip", "plate_id": "LIST2548"},
]

def _pad(prefix: str, n: int) -> list[str]:
    return [f"US.{prefix}{i:02d}" for i in range(n)]


# Realistically sized, because the relatedness floors are calibrated for real
# plates (20-600 constituents) and a 5-member "sector" is not one. Overlaps
# are deliberate: semis share heavily with the AI concepts and not at all with
# oil, which is what the assertions below actually check.
_SEMIS = ["US.NVDA", "US.AMD", "US.TSM", "US.INTC", "US.AVGO"] + _pad("SEM", 25)
MEMBERS = {
    # 30 names.
    "US.LIST2015": _SEMIS,
    # 20 names, no overlap with semis.
    "US.LIST2508": ["US.MSFT", "US.NOW", "US.CRM"] + _pad("SFT", 17),
    "US.LIST2509": ["US.CRM", "US.NOW"] + _pad("APP", 18),
    "US.LIST2224": ["US.CVX", "US.XOM"] + _pad("OIL", 18),
    # 20 names, 8 shared with semis -> jaccard 8/(30+20-8) = 0.19.
    "US.LIST2136": ["US.MSFT", "US.NOW"] + _SEMIS[:8] + _pad("AIX", 10),
    # 12 names, 10 shared with semis -> jaccard 10/(30+12-10) = 0.31, tighter.
    "US.LIST2548": _SEMIS[:10] + _pad("CHP", 2),
}


class FakeGateway:
    """Minimal stand-in. `etf_codes` reproduce the live all-or-nothing failure."""

    def __init__(self, etf_codes=(), owner_rows=None):
        self.etf_codes = set(etf_codes)
        self.owner_rows = owner_rows or {}
        self.owner_calls: list[list[str]] = []
        self.stock_calls: list[str] = []

    def get_plate_list(self, market, plate_class):
        name = getattr(plate_class, "name", str(plate_class))
        return INDUSTRY_ROWS if "INDUSTRY" in name.upper() else CONCEPT_ROWS

    def get_plate_stock(self, plate_code):
        self.stock_calls.append(plate_code)
        return [{"code": c, "stock_name": c.split(".")[-1]} for c in MEMBERS.get(plate_code, [])]

    def get_owner_plate(self, codes):
        self.owner_calls.append(list(codes))
        # The real server rejects the WHOLE batch if any member is an ETF.
        if any(c in self.etf_codes for c in codes):
            raise RuntimeError("Get Stock's Sector interface does not support ETFs type.")
        out = []
        for c in codes:
            out.extend(self.owner_rows.get(c, []))
        return out


FAST = RateLimiter(10_000, 0.001, "test")  # never actually sleeps


# --- derive_sector_group ---------------------------------------------------

check_eq("sector_group splits on ' - '",
         su.derive_sector_group("Software - Infrastructure"), "Software")
check_eq("sector_group keeps a name with no separator whole",
         su.derive_sector_group("Oil & Gas Integrated"), "Oil & Gas Integrated")
check_eq("sector_group is stable for the sibling plate",
         su.derive_sector_group("Software - Application"), "Software")
check_eq("sector_group of empty is empty", su.derive_sector_group(""), "")
check("a hyphen without spaces is NOT a separator",
      su.derive_sector_group("Real-Estate Services") == "Real-Estate Services",
      "only ' - ' splits, so hyphenated words survive")


# --- refresh_universe ------------------------------------------------------

gw = FakeGateway()
res = su.refresh_universe(gw, market="US", member_batch=100, limiter=FAST)
check_eq("universe stores every enumerated plate", res["plates"], 6)
check_eq("INDUSTRY count recorded", res["by_class"]["INDUSTRY"], 4)
check_eq("CONCEPT count recorded", res["by_class"]["CONCEPT"], 2)
check_eq("no failures on a clean refresh", res["failures"], [])
check_eq("universe reads back", len(su.plate_universe("US")), 6)
check_eq("universe filters by class", len(su.plate_universe("US", "CONCEPT")), 2)

stored = {p["plate_code"]: p for p in su.plate_universe("US")}
check_eq("sector_group persisted", stored["US.LIST2508"]["sector_group"], "Software")
check_eq("constituent_count filled from members",
         stored["US.LIST2015"]["constituent_count"], len(MEMBERS["US.LIST2015"]))

age = su.universe_age_days("US")
check("universe_age_days is ~0 right after a refresh", age is not None and age < 0.01,
      f"{age}")

# Idempotent: a second refresh must not duplicate rows.
su.refresh_universe(gw, market="US", member_batch=100, limiter=FAST)
check_eq("refresh is idempotent", len(su.plate_universe("US")), 6)

# A plate that LOSES a constituent must lose the membership row too.
db.replace_plate_members("US.LIST2015", [{"code": "US.NVDA", "stock_name": "NVIDIA"}])
check_eq("replace_plate_members drops departed constituents",
         len(db.get_plate_members("US.LIST2015")), 1)
su.refresh_universe(gw, market="US", member_batch=100, limiter=FAST)
check_eq("...and a refresh restores them",
         len(db.get_plate_members("US.LIST2015")), len(MEMBERS["US.LIST2015"]))


# --- the rotating member slice --------------------------------------------
#
# These run against a universe where SOME plates have already been visited,
# which is the state that matters and the one a fresh-database test never
# reaches. The first version of the sort key ordered on a short-circuited
# `and` that produced False for an unvisited plate and a string for a visited
# one — fine on run one, TypeError on run two. Same blind spot as the
# migration gotcha: the second call is the one worth testing.

_tmp2 = tempfile.mkdtemp(prefix="sector-rotate-")
db.DB_PATH = Path(_tmp2) / "rotate.db"
db.init_db()

gw2 = FakeGateway()
res2 = su.refresh_universe(gw2, market="US", member_batch=2, limiter=FAST)
check_eq("member refresh honours the batch cap", res2["members_refreshed"], 2)
check("the slice is a slice, not the whole universe",
      len(gw2.stock_calls) == 2, f"{len(gw2.stock_calls)} get_plate_stock calls for 6 plates")

pass1 = list(gw2.stock_calls)
gw2.stock_calls.clear()
res3 = su.refresh_universe(gw2, market="US", member_batch=2, limiter=FAST)
pass2 = list(gw2.stock_calls)
check("a SECOND refresh does not crash on a partly-visited universe",
      res3["members_refreshed"] == 2,
      "the sort key must order visited against unvisited, and a "
      "short-circuited `and` yields bool for one and str for the other")
check("...and advances to plates the first pass did not reach",
      not set(pass2) & set(pass1), f"pass1 {pass1} then pass2 {pass2}")

gw2.stock_calls.clear()
su.refresh_universe(gw2, market="US", member_batch=2, limiter=FAST)
pass3 = list(gw2.stock_calls)
check("...and keeps advancing on a third",
      not set(pass3) & (set(pass1) | set(pass2)), f"pass3 {pass3}")
check("three passes at 2/pass cover the whole 6-plate universe exactly once",
      len(set(pass1) | set(pass2) | set(pass3)) == 6,
      "the rotation converges rather than re-picking the same slice")

with db.get_connection() as conn:
    synced = conn.execute(
        "SELECT COUNT(*) FROM sector_plates WHERE members_synced_at IS NOT NULL"
    ).fetchone()[0]
check_eq("members_synced_at is recorded per plate, and only for visited ones",
         synced, 6)
check("members_synced_at is NOT the same field the list refresh rewrites",
      "members_synced_at" in db._SCHEMA and "last_seen_at" in db._SCHEMA,
      "ordering on last_seen_at would re-pick the same slice forever, "
      "because upsert_sector_plates stamps every plate on every run")

# Back to the main database for the rest of the suite.
db.DB_PATH = Path(_tmp) / "test.db"


# --- the taxonomy rule: MEMBERSHIP, not plate_type -------------------------

owner_rows = {
    # A real industry plate, correctly typed.
    "US.NVDA": [
        {"code": "US.NVDA", "plate_code": "US.LIST2015",
         "plate_name": "Semiconductors", "plate_type": "INDUSTRY"},
        # Typed OTHER by the server, but IS in the CONCEPT enumeration.
        # This must SURVIVE: the rule is membership, not plate_type.
        {"code": "US.NVDA", "plate_code": "US.LIST2548",
         "plate_name": "AI Chip", "plate_type": "OTHER"},
        # Broker product list: typed OTHER and NOT in the enumeration.
        {"code": "US.NVDA", "plate_code": "US.LIST92043",
         "plate_name": "FUTU-CA 美股定投", "plate_type": "OTHER"},
        # Novelty basket: same treatment.
        {"code": "US.NVDA", "plate_code": "US.LIST20883",
         "plate_name": "Nancy Pelosi Portfolio", "plate_type": "CONCEPT"},
    ],
}
gw3 = FakeGateway(owner_rows=owner_rows)
got = su.owner_plates(gw3, ["US.NVDA"], limiter=FAST)
names = sorted(p["plate_name"] for p in got.get("US.NVDA", []))
check_eq("enumerated plates survive", names, ["AI Chip", "Semiconductors"])
check("an OTHER-typed row IS kept when its code is in the universe",
      "AI Chip" in names, "membership is the test, not plate_type")
check("a broker product list is dropped", "FUTU-CA 美股定投" not in names)
check("a CONCEPT-typed row NOT in the enumeration is still dropped",
      "Nancy Pelosi Portfolio" not in names,
      "plate_type says CONCEPT; the enumeration does not list it")


# --- the ETF failure and the per-code fallback -----------------------------

many = [f"US.T{i:02d}" for i in range(25)] + ["US.SMH"]
rows = {c: [{"code": c, "plate_code": "US.LIST2015",
             "plate_name": "Semiconductors", "plate_type": "INDUSTRY"}]
        for c in many}
gw4 = FakeGateway(etf_codes={"US.SMH"}, owner_rows=rows)
got4 = su.owner_plates(gw4, many, limiter=FAST)
check_eq("every non-ETF code still resolves after a poisoned batch", len(got4), 25)
check("the ETF itself resolves to nothing", "US.SMH" not in got4)
check("the failure triggered a per-code retry",
      len(gw4.owner_calls) > 2, f"{len(gw4.owner_calls)} calls for 26 codes in chunks of 20")

# With security_type known, the ETF never enters a batch at all.
gw5 = FakeGateway(etf_codes={"US.SMH"}, owner_rows=rows)
got5 = su.owner_plates(gw5, many, security_types={"US.SMH": "ETF"}, limiter=FAST)
check_eq("security_type filter keeps every code resolving", len(got5), 25)
check_eq("...in exactly 2 batched calls, with no retry storm", len(gw5.owner_calls), 2)
check("the known ETF was never sent",
      all("US.SMH" not in c for c in gw5.owner_calls))
check_eq("owner_plates([]) is empty, not an error", su.owner_plates(gw5, [], limiter=FAST), {})


# --- relatedness -----------------------------------------------------------

su.refresh_universe(FakeGateway(), market="US", member_batch=100, limiter=FAST)
rel = su.related_plates("US.LIST2015")
rel_by = {r["plate_code"]: r for r in rel}
check("Semiconductors relates to AI Chip", "US.LIST2548" in rel_by,
      "10 shared constituents")
check("Semiconductors does NOT relate to Oil & Gas", "US.LIST2224" not in rel_by,
      "no shared constituents")
check("relatedness is ranked by Jaccard, not raw overlap",
      len(rel) > 1 and rel[0]["jaccard"] >= rel[-1]["jaccard"])
# Unconditional on purpose: this check previously guarded itself with an `if`
# and silently never ran, because the market-proxy ratio was excluding a
# legitimate plate on a small universe. A check that can skip itself is a
# check that reports PASS for a suite it never exercised.
check("both AI plates are related to Semiconductors",
      "US.LIST2548" in rel_by and "US.LIST2136" in rel_by,
      f"got {sorted(rel_by)}")
check("a tighter overlap outranks a looser one",
      rel_by["US.LIST2548"]["jaccard"] > rel_by["US.LIST2136"]["jaccard"],
      f"AI Chip {rel_by['US.LIST2548']['jaccard']} (10 shared of 32 union) vs "
      f"Artificial Intelligence {rel_by['US.LIST2136']['jaccard']} (8 of 42)")
# --- relatedness floors ----------------------------------------------------
#
# Measured in a real browser against the live corpus on 2026-08-30:
# Biotechnology (603 constituents) reported NVIDIA Portfolio and Crypto as its
# nearest sectors on a SINGLE shared ticker each, jaccard 0.0016. One
# coincidental overlap is not a relationship, and rendering it as one is worse
# than rendering nothing — a reader cannot tell it from a real neighbour.
db.upsert_sector_plates([{
    "plate_code": "US.LISTFLUKE", "market": "US", "plate_name": "Coincidental overlap",
    "plate_class": "CONCEPT", "plate_id": "LISTFLUKE", "sector_group": "Fluke",
}])
db.replace_plate_members("US.LISTFLUKE",
    [{"code": "US.NVDA", "stock_name": "NVIDIA"}]
    + [{"code": f"US.Z{i:04d}", "stock_name": f"Z{i}"} for i in range(399)])
check("a single shared ticker does NOT make two sectors related",
      "US.LISTFLUKE" not in {r["plate_code"] for r in su.related_plates("US.LIST2015")},
      f"needs {su.MIN_SHARED_MEMBERS} shared names AND jaccard "
      f"{su.MIN_JACCARD}; one name out of 400 is a coincidence")

# Clears the shared-name floor, nowhere near the share floor: still not related.
db.replace_plate_members("US.LISTFLUKE",
    [{"code": c, "stock_name": c} for c in ("US.NVDA", "US.AMD", "US.TSM")]
    + [{"code": f"US.Z{i:04d}", "stock_name": f"Z{i}"} for i in range(397)])
check("clearing the shared-name floor is not enough on its own",
      "US.LISTFLUKE" not in {r["plate_code"] for r in su.related_plates("US.LIST2015")},
      "3 shared of a 427-name union is jaccard 0.007 — both floors apply, "
      "not either")
check("...while a genuine neighbour still clears both",
      "US.LIST2548" in {r["plate_code"] for r in su.related_plates("US.LIST2015")},
      "AI Chip shares 10 constituents")
check("a plate with only weak matches returns nothing at all",
      su.related_plates("US.LISTFLUKE") == [],
      "an empty list is the honest answer while member lists are still "
      "loading; a one-ticker coincidence dressed as a neighbour is not")

check_eq("a plate never relates to itself",
         [r for r in rel if r["plate_code"] == "US.LIST2015"], [])

# A market-proxy plate must not relate everything to everything.
db.upsert_sector_plates([{
    "plate_code": "US.LISTALL", "market": "US", "plate_name": "Index Component",
    "plate_class": "CONCEPT", "plate_id": "LISTALL", "sector_group": "Index Component",
}])
every = sorted({c for codes in MEMBERS.values() for c in codes})
db.replace_plate_members("US.LISTALL", [{"code": c, "stock_name": c} for c in every])
rel2 = {r["plate_code"] for r in su.related_plates("US.LIST2015")}
check("a large-but-ordinary plate is NOT excluded as a market proxy",
      "US.LISTALL" in rel2,
      "measured on live data: Biotechnology has 603 constituents and Banks - "
      "Regional 361 against 1,894 known tickers, so an aggressive share rule "
      "excluded two perfectly ordinary sectors")

# Jaccard is what actually keeps a big plate from dominating, and it needs no
# help doing it — but only at realistic proportions, so this is checked at the
# scale the claim is actually made for rather than on the toy fixture above.
# (On a 10-ticker universe a basket holding all 10 really IS the most related
# thing to a 5-member sector, which is why the toy fixture cannot show this.)
db.upsert_sector_plates([{
    "plate_code": "US.LISTBIG", "market": "US", "plate_name": "Biotechnology-sized",
    "plate_class": "INDUSTRY", "plate_id": "LISTBIG", "sector_group": "Big",
}, {
    "plate_code": "US.LISTNEAR", "market": "US", "plate_name": "Tightly related",
    "plate_class": "CONCEPT", "plate_id": "LISTNEAR", "sector_group": "Near",
}, {
    "plate_code": "US.LISTSEC", "market": "US", "plate_name": "A 72-member sector",
    "plate_class": "INDUSTRY", "plate_id": "LISTSEC", "sector_group": "Sector",
}])
# The rest of the market has to exist, or a 603-member plate is 92% of a
# 655-ticker universe and trips the degenerate-basket cap for a reason that
# would never occur live (603 of several thousand US tickers is ~15%).
db.upsert_sector_plates([{
    "plate_code": "US.LISTREST", "market": "US", "plate_name": "Rest of market",
    "plate_class": "INDUSTRY", "plate_id": "LISTREST", "sector_group": "Rest",
}])
db.replace_plate_members("US.LISTREST", [
    {"code": f"US.R{i:04d}", "stock_name": f"R{i}"} for i in range(3000)])

sector72 = [{"code": f"US.S{i:03d}", "stock_name": f"S{i}"} for i in range(72)]
db.replace_plate_members("US.LISTSEC", sector72)
# 603 members, 40 of them shared — the live Biotechnology shape.
db.replace_plate_members("US.LISTBIG", sector72[:40] + [
    {"code": f"US.B{i:03d}", "stock_name": f"B{i}"} for i in range(563)])
# 40 members, 30 shared — a genuine neighbour.
db.replace_plate_members("US.LISTNEAR", sector72[:30] + [
    {"code": f"US.N{i:03d}", "stock_name": f"N{i}"} for i in range(10)])

rel_real = {r["plate_code"]: r for r in su.related_plates("US.LISTSEC")}
check("a 603-member plate sharing 40 names ranks BELOW a 40-member one sharing 30",
      rel_real["US.LISTBIG"]["jaccard"] < rel_real["US.LISTNEAR"]["jaccard"],
      f"big {rel_real['US.LISTBIG']['jaccard']} vs near "
      f"{rel_real['US.LISTNEAR']['jaccard']} — Jaccard divides by the UNION, "
      "so breadth is self-penalising and needs no exclusion rule to help it")
check("...and the tight neighbour is ranked first",
      su.related_plates("US.LISTSEC")[0]["plate_code"] == "US.LISTNEAR")
check("a large ordinary plate is still REPORTED, just ranked lower",
      "US.LISTBIG" in rel_real,
      "excluding it outright is what wrongly dropped Biotechnology")

# Only the pathological case is excluded: a basket holding essentially the
# entire known universe is related to everything by construction, so its
# overlap carries no information. Built from every code actually in the
# database rather than a made-up count, which is the only way the share is
# genuinely over the threshold.
with db.get_connection() as conn:
    all_known = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM sector_plate_members")]
db.replace_plate_members("US.LISTALL", [{"code": c, "stock_name": c} for c in all_known])
rel3 = {r["plate_code"] for r in su.related_plates("US.LIST2015")}
check("a basket holding essentially the whole universe IS excluded",
      "US.LISTALL" not in rel3,
      f"{len(all_known)} members = 100% of everything known, over the "
      f"{su.CONCEPT_MARKET_PROXY_SHARE:.0%} degenerate-basket cap")
check("...while the ordinary 603-member plate is still included",
      "US.LISTBIG" in {r["plate_code"] for r in su.related_plates("US.LISTSEC")},
      "the cap catches only the degenerate case; Jaccard handles the rest")

report("sector universe")
