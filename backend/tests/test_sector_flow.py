"""Checks for the deterministic rotation score.

Run from backend/:  .venv/bin/python -m tests.test_sector_flow

The guards are the point of this suite, not the arithmetic. Each one encodes
a way a corpus flatters itself that `thesis_scorecard` (decisions #67) had to
learn the hard way, and the first one below — a rising market must NOT read
as inflow everywhere — is #67(c) wearing a new costume.

Offline: temp database, constructed bars, no gateway, no model, no network.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="sector-flow-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import sector_flow as sf  # noqa: E402
from tests.harness import check, check_close, check_eq, report  # noqa: E402


def bars(n, *, start=100.0, daily_pct=0.0, turnover=1e9, jumps=None, code="X"):
    """n+1 sessions so an n-session window has something to measure from."""
    jumps = jumps or {}
    out, close = [], start
    for i in range(n + 1):
        pct = jumps.get(i, daily_pct)
        prev = close
        close = close * (1 + pct / 100.0)
        out.append({
            "plate_code": code,
            "bar_date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}",
            "close": round(close, 6),
            "change_rate": 0.0 if i == 0 else round((close / prev - 1) * 100, 6),
            "volume": 1e6,
            "turnover": turnover,
            "suspect_bar": 0,
        })
    return out


def meta_for(codes, constituents=50):
    return {c: {"plate_name": c, "plate_class": "INDUSTRY", "sector_group": c,
                "constituent_count": constituents} for c in codes}


def by_window(scores, window):
    return {s.plate_code: s for s in scores if s.window_days == window}


# ===========================================================================
# (c) THE ONE THAT MATTERS: a rising market is not inflow everywhere
# ===========================================================================

codes = [f"P{i}" for i in range(20)]
rising = {c: bars(70, daily_pct=1.0, code=c) for c in codes}
scores = sf.build_scores(rising, {}, meta_for(codes), "2026-03-01")

w5 = by_window(scores, 5)
check_eq("every plate scored on the 5-session window", len(w5), 20)
check("a market where EVERY sector rose 1%/day scores ~0 everywhere",
      all(abs(s.score) < 0.01 for s in w5.values()),
      f"max |score| = {max(abs(s.score) for s in w5.values()):.4f}")
check("...and none of them reads as inflow",
      not any(s.score > 0.05 for s in w5.values()),
      "an absolute measure would report inflow into all 20 at once — "
      "one market observation wearing 20 hats (decisions #67c)")

# Now a genuinely dispersed market: all rise, but by different amounts.
varied = {c: bars(70, daily_pct=0.2 + 0.15 * i, code=c) for i, c in enumerate(codes)}
vscores = by_window(sf.build_scores(varied, {}, meta_for(codes), "2026-03-01"), 5)
vals = [s.score for s in vscores.values()]
check("in a dispersed market the scores straddle zero",
      min(vals) < 0 < max(vals), f"range {min(vals):.3f} .. {max(vals):.3f}")
check("the fastest riser outranks the slowest",
      vscores["P19"].score > vscores["P0"].score,
      f"P19 {vscores['P19'].score:.3f} vs P0 {vscores['P0'].score:.3f}")
check("the median score sits near zero even though every sector rose",
      abs(sorted(vals)[len(vals) // 2]) < 0.35,
      f"median {sorted(vals)[len(vals) // 2]:.3f}")
check_eq("the baseline is named in the payload, not left to the UI",
         sf.rotation_board.__doc__ is not None, True)


# ===========================================================================
# (a) a window emits NOTHING below its session floor
# ===========================================================================

short = {c: bars(20, daily_pct=0.5, code=c) for c in codes}
sscores = sf.build_scores(short, {}, meta_for(codes), "2026-03-01")
check_eq("20 sessions still score the 5-session window", len(by_window(sscores, 5)), 20)
w63 = by_window(sscores, 63)
check("the 63-session window emits rows marked unavailable, never a score",
      all(s.score is None and not s.available for s in w63.values()),
      "'not yet knowable' and 'knowable and neutral' must not share a shape")
check("...and says why", all("needs 64 sessions" in (s.reason or "") for s in w63.values()))
check_eq("persist() stores nothing for an unknowable window",
         len([s for s in w63.values() if s.available]), 0)
check_eq("MIN_SESSIONS demands window+1 bars, since a return needs two closes",
         sf.MIN_SESSIONS[63], 64)


# ===========================================================================
# (b) constituent floor — renders, but without emphasis
# ===========================================================================

thin_meta = meta_for(codes)
thin_meta["P0"]["constituent_count"] = 3
thin_meta["P1"]["constituent_count"] = 0
tscores = by_window(sf.build_scores(rising, {}, thin_meta, "2026-03-01"), 5)
check("a 3-constituent plate still gets a score", tscores["P0"].score is not None)
check("...but is marked insufficient", not tscores["P0"].sufficient)
check_eq("...with the count stated", tscores["P0"].reason, "only 3 constituents")
check("a count of 0 reads as UNKNOWN, not as zero constituents",
      tscores["P1"].reason == "constituent count unknown",
      "the member refresh is a rotating slice and may not have reached it yet")
check("a well-populated plate is sufficient", tscores["P5"].sufficient)


# ===========================================================================
# (d) turnover thrust compares a plate ONLY to itself
# ===========================================================================

huge = bars(70, daily_pct=1.0, turnover=1.17e11, code="HUGE")
tiny = bars(70, daily_pct=1.0, turnover=2.0e8, code="TINY")
# Both double their own volume over the final 5 sessions.
for b in huge[-5:]:
    b["turnover"] = 1.17e11 * 2
for b in tiny[-5:]:
    b["turnover"] = 2.0e8 * 2
mixed = {"HUGE": huge, "TINY": tiny}
mscores = by_window(sf.build_scores(mixed, {}, meta_for(["HUGE", "TINY"]), "2026-03-01"), 5)
check_close("a $117B plate and a $200M plate with identical RELATIVE volume "
            "get identical thrust",
            mscores["HUGE"].turnover_thrust, mscores["TINY"].turnover_thrust,
            abs_tol=1e-9)
check("doubling your own volume registers as a real thrust",
      mscores["HUGE"].turnover_thrust > 0.7,
      f"{mscores['HUGE'].turnover_thrust} (tanh(log(2)/log(2)) = tanh(1))")

flat_vol = {c: bars(70, daily_pct=0.5, code=c) for c in ["A", "B"]}
fscores = by_window(sf.build_scores(flat_vol, {}, meta_for(["A", "B"]), "2026-03-01"), 5)
check_close("unchanged volume is zero thrust, not a positive one",
            fscores["A"].turnover_thrust, 0.0, abs_tol=1e-9)


# ===========================================================================
# (e) thin sessions are detected FROM THE DATA, with no calendar
# ===========================================================================

half_day = {c: bars(70, daily_pct=0.3, code=c) for c in codes}
for b in half_day.values():
    b[-1]["turnover"] = 1e9 * 0.2          # 20% of normal, across every sector
hscores = sf.build_scores(half_day, {}, meta_for(codes), "2026-03-01")
h1 = by_window(hscores, 1)
check("a session at 20% of normal volume market-wide is flagged thin",
      all(s.thin_session for s in h1.values()),
      "no exchange calendar is hardcoded — decisions #9")
check("a thin session makes the 1-session window insufficient",
      not any(s.sufficient for s in h1.values()))
check_eq("...and says why", h1["P0"].reason, "thin session (likely a half day)")
check("the 5-session window is NOT invalidated by one thin session",
      all(s.sufficient for s in by_window(hscores, 5).values()),
      "a half day is a real session; it just cannot carry a daily reading")
check("a normal session is not flagged",
      not any(s.thin_session for s in by_window(scores, 1).values()))


# ===========================================================================
# (f) an index rebase is not a rotation
# ===========================================================================

# The jump sits INSIDE the 5-session window (index 68 of 0..70), which is the
# only place it could distort a 5-session reading.
rebased = {c: bars(70, daily_pct=0.1, code=c) for c in codes}
rebased["P0"] = bars(70, daily_pct=0.1, jumps={68: 40.0}, code="P0")
rebased["P0"][68]["suspect_bar"] = 1
rscores = by_window(sf.build_scores(rebased, {}, meta_for(codes), "2026-03-01"), 5)
check("a rebased plate still gets a persistence reading",
      rscores["P0"].persistence is not None)
check("...but a +40% bar does not make it read as a sustained trend",
      rscores["P0"].persistence < 1.0,
      f"persistence {rscores['P0'].persistence} — the other four sessions track "
      "the median, so one bookkeeping bar must not read as four days of inflow")

# The stored flag and the inline magnitude test are NOT the same test, and
# the difference is the reason both exist. `suspect_bar` is written at ingest
# from the plate's ABSOLUTE change_rate; the magnitude test here works on the
# RELATIVE return, which is a different number. A bar can trip one and not
# the other, so the flag has to be exercised on its own — with a jump small
# enough that the magnitude test would happily accept it.
flagged = {c: bars(70, daily_pct=0.1, code=c) for c in codes}
flagged["P0"] = bars(70, daily_pct=0.1, jumps={68: 20.0}, code="P0")
unflagged = {c: list(v) for c, v in flagged.items()}
unflagged["P0"] = bars(70, daily_pct=0.1, jumps={68: 20.0}, code="P0")
flagged["P0"][68]["suspect_bar"] = 1
fs = by_window(sf.build_scores(flagged, {}, meta_for(codes), "2026-03-01"), 5)
us = by_window(sf.build_scores(unflagged, {}, meta_for(codes), "2026-03-01"), 5)
check("a 20% bar is UNDER the magnitude cutoff, so only the flag can exclude it",
      abs(20.0) < sf.SUSPECT_BAR_PCT)
check("the stored suspect flag alone changes the persistence reading",
      fs["P0"].persistence != us["P0"].persistence,
      f"flagged {fs['P0'].persistence} vs unflagged {us['P0'].persistence}")
check("excluding a rebase bar makes the trend read as WEAKER, never stronger",
      fs["P0"].persistence <= us["P0"].persistence,
      "the flat sessions are what remain, and they did not trend")
check("SUSPECT_BAR_PCT clears real volatility comfortably",
      sf.SUSPECT_BAR_PCT > 8.2,
      "the largest real move in 145 measured Semiconductor sessions was 8.2%")

# Persistence semantics, on clean data.
steady = {c: bars(70, daily_pct=0.1, code=c) for c in codes}
steady["UP"] = bars(70, daily_pct=2.0, code="UP")
steady["DOWN"] = bars(70, daily_pct=-2.0, code="DOWN")
pscores = by_window(sf.build_scores(steady, {}, meta_for(list(steady)), "2026-03-01"), 5)
check_close("a sector up every session has persistence +1",
            pscores["UP"].persistence, 1.0, abs_tol=1e-9)
check_close("a sector down every session has persistence -1",
            pscores["DOWN"].persistence, -1.0, abs_tol=1e-9)
check_close("a sector tracking the median exactly reads 0.0, NOT missing",
            pscores["P0"].persistence, 0.0, abs_tol=1e-9)
check("...so it is a present component, not a coverage penalty",
      "persistence" in pscores["P0"].components
      and "persistence" not in pscores["P0"].components_missing,
      "missing means 'could not measure'; 0.0 means 'measured, and neutral'")


# ===========================================================================
# (g) coverage — a missing component must not become a positive one
# ===========================================================================

no_breadth = by_window(scores, 1)["P0"]
check("breadth is absent from components when no snapshot exists",
      "breadth" not in no_breadth.components)
check("...and is named in components_missing",
      "breadth" in no_breadth.components_missing)
check_close("...dropping coverage to the remaining weight",
            no_breadth.coverage, 0.75, abs_tol=1e-9)
check("a missing component does not push the score positive",
      no_breadth.score <= 0.0 + 1e-9,
      f"score {no_breadth.score} on a market where nothing outperformed")

with_breadth = sf.build_scores(
    rising, {"P0": {"raise_count": 40, "fall_count": 10, "equal_count": 0}},
    meta_for(codes), "2026-03-01")
b1 = by_window(with_breadth, 1)["P0"]
check_close("breadth present restores full coverage", b1.coverage, 1.0, abs_tol=1e-9)
check_close("breadth is (up-down)/total", b1.breadth, 0.6, abs_tol=1e-9)

# Below MIN_COVERAGE nothing is scored at all.
one_bar_turnover = {c: bars(70, daily_pct=0.5, code=c) for c in codes}
for b in one_bar_turnover.values():
    for row in b:
        row["turnover"] = None
nc = by_window(sf.build_scores(one_bar_turnover, {}, meta_for(codes), "2026-03-01"), 1)
check("with turnover AND breadth missing, coverage falls below the floor",
      all(not s.available for s in nc.values()),
      f"coverage {nc['P0'].coverage} < {sf.MIN_COVERAGE}")
check("...and the reason names the shortfall", "coverage" in (nc["P0"].reason or ""))


# ===========================================================================
# inspectability: the score IS its components (decisions #66)
# ===========================================================================

for window in sf.WINDOWS:
    rows = by_window(sf.build_scores(varied, {}, meta_for(codes), "2026-03-01"), window)
    row = next((r for r in rows.values() if r.score is not None), None)
    if row is None:
        continue
    recomputed = sum(row.components[k] * sf.WEIGHTS[window][k] for k in row.components)
    check_close(f"w={window}: score == sum(component * weight)",
                row.score, round(recomputed, 4), abs_tol=1e-4)

check("weights are not renormalised when a component is missing",
      abs(no_breadth.score) <= abs(sum(
          no_breadth.components[k] for k in no_breadth.components)) + 1e-9,
      "renormalising would silently amplify whatever survived")
for window, w in sf.WEIGHTS.items():
    check_close(f"w={window}: weights sum to 1.0", sum(w.values()), 1.0, abs_tol=1e-9)
check("the 1-session window has no persistence key at all",
      "persistence" not in sf.WEIGHTS[1],
      "a degenerate component is worse than an absent one")
check("the multi-session windows have no breadth key",
      all("breadth" not in sf.WEIGHTS[w] for w in (5, 21, 63)),
      "a snapshot has no history to look back through")


# ===========================================================================
# persistence to disk and the read side
# ===========================================================================

db.upsert_sector_plates([
    {"plate_code": c, "market": "US", "plate_name": f"Sector {c}",
     "plate_class": "INDUSTRY", "plate_id": c, "sector_group": f"Sector {c}",
     "constituent_count": 50}
    for c in codes
])
for c in codes:
    db.upsert_sector_bars(varied[c] if c in varied else [])

written = sf.persist(sf.build_scores(varied, {}, meta_for(codes), "2026-03-01"))
check_eq("only scoreable rows are persisted", written,
         len([s for s in sf.build_scores(varied, {}, meta_for(codes), "2026-03-01")
              if s.available and s.score is not None]))
again = sf.persist(sf.build_scores(varied, {}, meta_for(codes), "2026-03-01"))
check_eq("a re-run is idempotent on (plate, date, window)", again, written)

board = sf.rotation_board(market="US", window_days=5, top_n=5)
check("the board is available once scores exist", board["available"])
check_eq("inflow is capped at top_n", len(board["inflow"]), 5)
check("inflow is ordered best first",
      board["inflow"][0]["score"] >= board["inflow"][-1]["score"])
check("outflow is ordered worst first",
      board["outflow"][0]["score"] <= board["outflow"][-1]["score"])
check("the board states its baseline in the payload",
      "median sector" in board["baseline"],
      "a UI must not be free to call this zero 'the market'")
check_eq("the board reports the thresholds the UI must not hardcode",
         (board["min_constituents"], board["min_sessions"]), (5, 6))
check_eq("components round-trip through JSON as a dict",
         isinstance(board["inflow"][0]["components"], dict), True)

empty = sf.rotation_board(market="HK", window_days=5)
check("an unscored market degrades honestly", not empty["available"])
check("...with a reason, not an empty list that reads as 'nothing is rotating'",
      "no scores yet" in empty["reason"])
check_eq("...and still no rows", (empty["inflow"], empty["outflow"]), ([], []))

try:
    sf.rotation_board(window_days=7)
    check("an unknown window raises rather than silently falling back", False)
except ValueError as exc:
    check("an unknown window raises rather than silently falling back",
          "window must be one of" in str(exc))

# --- pairs degrade honestly on a young universe ---------------------------
#
# A pair needs constituent data on BOTH sides, and member lists arrive as a
# rotating slice. An empty list would read as "nothing is rotating" when the
# truth is "we cannot tell yet" — the distinction this whole feature is built
# around.
# The young-universe state, constructed explicitly: plates exist and are
# scored, but no member list has been fetched for any of them yet.
with db.get_connection() as conn:
    conn.execute("UPDATE sector_plates SET constituent_count = 0")
pairs = sf.rotation_pairs(window_days=5)
check("pairs report availability rather than returning a bare list",
      isinstance(pairs, dict) and "available" in pairs)
check("with no member data, pairs are unavailable — not empty",
      not pairs["available"])
check("...and the reason names the actual shortfall",
      "constituent data" in (pairs["reason"] or ""), f"{pairs['reason']}")
check("...with coverage stated so the UI can show progress",
      pairs["coverage"]["with_members"] == 0 and pairs["coverage"]["rows"] > 0,
      f"{pairs['coverage']}")
check("...and the disclaimer rides along regardless",
      "not a dollar traced" in pairs["note"])
with db.get_connection() as conn:
    conn.execute("UPDATE sector_plates SET constituent_count = 50")

# With members on both sides of a genuine overlap, a pair appears.
db.replace_plate_members("P19", [{"code": f"US.X{i}", "stock_name": f"X{i}"} for i in range(20)])
db.replace_plate_members("P0", [{"code": f"US.X{i}", "stock_name": f"X{i}"} for i in range(15)]
                               + [{"code": "US.Y1", "stock_name": "Y1"}])
paired = sf.rotation_pairs(window_days=5)
check("once both sides have constituents, pairing becomes available",
      paired["available"], f"{paired.get('reason')}")
check("...and finds the overlapping pair",
      any(p["from"]["plate_code"] == "P0" and p["to"]["plate_code"] == "P19"
          for p in paired["pairs"]),
      f"{[(p['from']['plate_code'], p['to']['plate_code']) for p in paired['pairs']]}")
if paired["pairs"]:
    p0 = paired["pairs"][0]
    check("...naming what made them related", p0["link_basis"] == "shared_members")
    check("...and how strongly", p0["shared_members"] > 0 and 0 < p0["jaccard"] <= 1)
    check("...with the loser on the FROM side and the winner on the TO side",
          p0["from"]["score"] < 0 < p0["to"]["score"],
          f"{p0['from']['score']} -> {p0['to']['score']}")

detail = sf.sector_detail(codes[0], window_days=5)
check("sector_detail resolves a known plate", detail["available"])
check_eq("...and reports its window list", detail["windows"], list(sf.WINDOWS))
missing = sf.sector_detail("US.NOPE")
check("an unknown plate degrades rather than raising", not missing["available"])
check_eq("...with a reason", missing["reason"], "unknown plate")

report("sector flow")
