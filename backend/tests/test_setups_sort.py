"""Checks for /setups ordering.

Run from backend/:  .venv/bin/python -m tests.test_setups_sort

The tie-breaks are the point. Most theses land on conviction 5 or 6, so
"highest conviction first" without a secondary key returns an arbitrary slice
of the 6s — and `created_at` has SECOND granularity (`db.now_iso` uses
timespec="seconds"), so a scan writes several rows sharing a timestamp. Both
degenerate cases are constructed here deliberately rather than hoped for.

Offline: temp database, no HTTP, no model.
"""

import json
import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="setups-sort-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.routers.setups import _SORTS, _recent                    # noqa: E402

from tests.harness import check, report  # noqa: E402


def seed(code: str, conviction: int, created_at: str) -> int:
    """Insert a setup with an exact created_at, bypassing the DEFAULT.

    `trade_setups.code` is a FK to `watchlist_cache`, so the ticker has to
    exist before a thesis about it can.
    """
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_cache "
            "(code, name, market, enabled, last_synced_at, updated_at) "
            "VALUES (?, ?, 'US', 1, ?, ?)",
            (code, code, created_at, created_at),
        )
        cur = conn.execute(
            """
            INSERT INTO trade_setups
                (scanner_run_id, code, market, created_at, data_as_of,
                 is_delayed_data, indicator_snapshot, feature_vector,
                 trade_direction, conviction_score, reasoning,
                 suggested_stop, suggested_target, key_levels_notes,
                 similar_setup_ids)
            VALUES (NULL, ?, 'US', ?, ?, 0, ?, ?, 'Neutral', ?,
                    'One. Two. Three.', NULL, NULL, NULL, ?)
            """,
            (code, created_at, created_at, json.dumps({"indicators": {}}),
             json.dumps([0.0] * 13), conviction, json.dumps([])),
        )
        return int(cur.lastrowid)


OLD = "2026-01-01T00:00:00+00:00"
NEW = "2026-08-24T00:00:00+00:00"
SAME = "2026-06-01T12:00:00+00:00"

old_high = seed("US.OLDHIGH", 9, OLD)     # old but the model liked it most
new_low = seed("US.NEWLOW", 2, NEW)       # newest, but rated poorly
tie_a = seed("US.TIEA", 6, SAME)          # same score AND same second
tie_b = seed("US.TIEB", 6, SAME)

# --- conviction order --------------------------------------------------
rows = _recent(limit=50, code=None, min_conviction=None, sort="conviction")
order = [r["code"] for r in rows]
check("conviction sort puts the highest score first", order[0] == "US.OLDHIGH",
      str(order))
check("an old high-conviction thesis outranks a new low one",
      order.index("US.OLDHIGH") < order.index("US.NEWLOW"),
      "otherwise the ranking is just a feed with extra steps")
check("the lowest score sorts last", order[-1] == "US.NEWLOW", str(order))

# --- recency order -----------------------------------------------------
rows = _recent(limit=50, code=None, min_conviction=None, sort="recent")
order = [r["code"] for r in rows]
check("recent sort puts the newest first", order[0] == "US.NEWLOW", str(order))
check("recent sort ignores conviction entirely",
      order.index("US.NEWLOW") < order.index("US.OLDHIGH"))

# --- the tie-breaks ----------------------------------------------------
# Same conviction AND same created_at second: only `id DESC` separates them,
# and without it a limit/offset page could repeat or skip a row.
rows = _recent(limit=50, code=None, min_conviction=None, sort="conviction")
ids = [r["id"] for r in rows if r["code"] in ("US.TIEA", "US.TIEB")]
check("a full conviction+timestamp tie breaks on id, newest first",
      ids == [tie_b, tie_a], f"{ids} (expected {[tie_b, tie_a]})")

check("the same tie breaks identically under recent sort",
      [r["id"] for r in _recent(limit=50, code=None, min_conviction=None,
                                sort="recent")
       if r["code"] in ("US.TIEA", "US.TIEB")] == [tie_b, tie_a])

# Conviction ties must still be time-ordered, not arbitrary.
tie_older = seed("US.TIEOLD", 6, "2026-05-01T00:00:00+00:00")
rows = _recent(limit=50, code=None, min_conviction=None, sort="conviction")
sixes = [r["code"] for r in rows if r["conviction_score"] == 6]
check("within one conviction score, newer comes first",
      sixes.index("US.TIEB") < sixes.index("US.TIEOLD"), str(sixes))

# --- ordering is monotonic, not just first/last ------------------------
rows = _recent(limit=50, code=None, min_conviction=None, sort="conviction")
scores = [r["conviction_score"] for r in rows]
check("conviction scores are non-increasing across the whole page",
      scores == sorted(scores, reverse=True), str(scores))

rows = _recent(limit=50, code=None, min_conviction=None, sort="recent")
stamps = [r["created_at"] for r in rows]
check("timestamps are non-increasing across the whole page",
      stamps == sorted(stamps, reverse=True), str(stamps))

# --- filters still compose with either sort ----------------------------
rows = _recent(limit=50, code=None, min_conviction=6, sort="conviction")
check("min_conviction composes with the sort",
      all(r["conviction_score"] >= 6 for r in rows) and len(rows) == 4,
      f"{len(rows)} rows")
rows = _recent(limit=2, code=None, min_conviction=None, sort="conviction")
check("limit applies after ordering, not before",
      [r["code"] for r in rows] == ["US.OLDHIGH", "US.TIEB"],
      str([r["code"] for r in rows]))

# --- the whitelist is the only path into the ORDER BY -------------------
check("only known sorts exist", set(_SORTS) == {"conviction", "recent"})
try:
    _recent(limit=5, code=None, min_conviction=None, sort="id; DROP TABLE trade_setups")
    check("an unknown sort cannot reach the SQL", False, "no exception")
except KeyError:
    # The router turns this into a 400; the point here is that it never
    # reaches string interpolation.
    check("an unknown sort cannot reach the SQL", True)
    with db.get_connection() as conn:
        still = conn.execute("SELECT count(*) FROM trade_setups").fetchone()[0]
    check("the table survived the injection attempt", still == 5, f"{still} rows")

# --- latest_per_code: one row per ticker -------------------------------
# The rotation re-analyses every enabled ticker roughly hourly, so the browse
# page was showing seven near-identical cards of one ticker while the rest of
# the watchlist never appeared. These rows reproduce that shape deliberately:
# a ticker whose conviction FELL over time, so "newest" and "highest" disagree.
dup_old = seed("US.DUP", 9, "2026-07-01T00:00:00+00:00")   # best score, stale
dup_mid = seed("US.DUP", 3, "2026-07-02T00:00:00+00:00")
dup_new = seed("US.DUP", 5, "2026-07-03T00:00:00+00:00")   # the current read

rows = _recent(limit=50, code=None, min_conviction=None, sort="recent",
               latest_per_code=True)
dups = [r for r in rows if r["code"] == "US.DUP"]
check("latest_per_code returns exactly one row per ticker",
      len(dups) == 1, f"{len(dups)} rows for US.DUP")
check("and it is the NEWEST, not the highest-conviction",
      dups and dups[0]["id"] == dup_new,
      f"got id {dups[0]['id'] if dups else None}, expected {dup_new}")

codes = [r["code"] for r in rows]
check("no ticker appears twice anywhere in the page",
      len(codes) == len(set(codes)), str(codes))

# thesis_count is the true stored total, not the size of this page.
check("thesis_count reports every stored thesis for the ticker",
      dups and dups[0]["thesis_count"] == 3,
      f"{dups[0].get('thesis_count') if dups else None} (expected 3)")

# ROW_NUMBER()'s scaffolding must not reach the API response.
check("_rn never leaks into a returned row",
      all("_rn" not in r for r in rows),
      str([k for k in rows[0] if k.startswith("_")]))

# --- min_conviction must not resurrect a superseded thesis -------------
# US.DUP's current read is a 5. Its old 9 is superseded advice. Filtering
# INSIDE the window would return that 9 and present it as current, which is
# the whole reason the filter is applied after the collapse.
rows = _recent(limit=50, code=None, min_conviction=6, sort="conviction",
               latest_per_code=True)
check("a ticker whose CURRENT thesis is below the floor is excluded",
      not any(r["code"] == "US.DUP" for r in rows),
      "the stale 9 was resurrected — the filter ran inside the window")
check("min_conviction still admits tickers that genuinely qualify",
      any(r["code"] == "US.OLDHIGH" for r in rows),
      str([r["code"] for r in rows]))
check("every surviving row really is above the floor",
      all(r["conviction_score"] >= 6 for r in rows))

# --- the same-second tie-break -----------------------------------------
# db.now_iso() has second granularity. This has never actually collided in
# the live corpus (a thesis takes 25-120s), but the collapse must still be
# deterministic rather than returning whichever row SQLite reached first.
SEC = "2026-07-04T09:00:00+00:00"
same_a = seed("US.SAMESEC", 4, SEC)
same_b = seed("US.SAMESEC", 7, SEC)
rows = _recent(limit=50, code="US.SAMESEC", min_conviction=None,
               sort="recent", latest_per_code=True)
check("a same-second pair collapses to one row",
      len(rows) == 1, f"{len(rows)} rows")
check("and it breaks on id DESC, deterministically",
      rows and rows[0]["id"] == same_b,
      f"got {rows[0]['id'] if rows else None}, expected {same_b}")

# --- the regression guard for the three recency-dependent callers -------
# The dashboard, the "Watch today" list and the scan-runner progress bar all
# read this endpoint WITHOUT the flag. The scan dialog counts rows carrying a
# run id, so collapsing by default would pin its progress bar near-flat.
plain = _recent(limit=50, code=None, min_conviction=None, sort="recent")
check("without the flag, duplicates are still returned",
      len([r for r in plain if r["code"] == "US.DUP"]) == 3,
      "the default shape changed — three frontend callers depend on it")
check("without the flag, no thesis_count column is invented",
      all("thesis_count" not in r for r in plain))
check("without the flag, no _rn column appears either",
      all("_rn" not in r for r in plain))

# Both flags compose with either sort, and the collapse is stable across them.
for s in ("conviction", "recent"):
    got = _recent(limit=50, code=None, min_conviction=None, sort=s,
                  latest_per_code=True)
    ids = [r["id"] for r in got if r["code"] == "US.DUP"]
    check(f"the collapse picks the same row under sort={s}",
          ids == [dup_new], f"{ids} (expected {[dup_new]})")

check("latest_per_code composes with an explicit code filter",
      [r["id"] for r in _recent(limit=50, code="US.DUP", min_conviction=None,
                                sort="conviction", latest_per_code=True)]
      == [dup_new])

# limit applies to the COLLAPSED set, not to the rows scanned before it.
check("limit counts collapsed rows, not raw ones",
      len(_recent(limit=2, code=None, min_conviction=None, sort="recent",
                  latest_per_code=True)) == 2)

# --- the two "latest" answers must agree -------------------------------
# This is the real reason db.get_latest_setup_for_code gained an id tie-break.
# Once the Theses page collapses per code, two endpoints answer the same
# question by different routes: /setups?latest_per_code=1 (window function)
# and /setups/latest/{code} (ORDER BY ... LIMIT 1). The ticker page's "Latest
# thesis" card and the Theses page's row for that ticker would disagree with
# no way to tell which was right from the UI. Pin the invariant rather than
# trusting that the two orderings were written to match.
deduped = _recent(limit=50, code=None, min_conviction=None, sort="recent",
                  latest_per_code=True)
mismatched = [
    (r["code"], r["id"], (db.get_latest_setup_for_code(r["code"]) or {}).get("id"))
    for r in deduped
    if (db.get_latest_setup_for_code(r["code"]) or {}).get("id") != r["id"]
]
check("get_latest_setup_for_code agrees with the deduped list for every code",
      not mismatched, str(mismatched))

report("setups sort")
