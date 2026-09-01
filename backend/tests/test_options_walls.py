"""Checks for options_walls.compute_walls (pure, no network).

Run from backend/:  .venv/bin/python -m tests.test_options_walls
"""

from app.services.options_walls import compute_walls

from tests.harness import check_eq as check, report


def opt(strike, oi, kind="CALL", volume=0):
    return {"option_strike_price": strike, "option_open_interest": oi,
            "option_type": kind, "volume": volume}


# --- basic wall selection --------------------------------------------
rows = [
    opt(100, 50, "CALL"), opt(110, 900, "CALL"), opt(120, 300, "CALL"),
    opt(90, 700, "PUT"),  opt(80, 120, "PUT"),
]
w = compute_walls(rows, expiry="2026-09-04", spot=100.0)
check("call wall is max-OI call strike", w.call_wall, 110.0)
check("call wall OI", w.call_wall_oi, 900)
check("put wall is max-OI put strike", w.put_wall, 90.0)
check("put wall OI", w.put_wall_oi, 700)
check("total call OI", w.total_call_oi, 1250)
check("total put OI", w.total_put_oi, 820)
check("put/call ratio", round(w.put_call_oi_ratio, 4), round(820 / 1250, 4))
check("strikes considered", w.strikes_considered, 5)
check("has_walls", w.has_walls, True)

# --- distances are signed relative to spot ---------------------------
check("call wall 10% above spot", round(w.call_wall_distance_pct, 4), 10.0)
check("put wall 10% below spot", round(w.put_wall_distance_pct, 4), -10.0)

# --- duplicate strikes are summed, not overwritten -------------------
dup = compute_walls(
    [opt(100, 300, "CALL"), opt(100, 400, "CALL"), opt(105, 600, "CALL")],
    expiry="x", spot=100.0,
)
check("duplicate strikes aggregate", dup.call_wall, 100.0)
check("aggregated OI summed", dup.call_wall_oi, 700)

# --- an all-zero-OI chain has no wall (must not pick an arbitrary strike)
zero = compute_walls([opt(100, 0), opt(110, 0), opt(120, 0, "PUT")], expiry="x", spot=100.0)
check("no call wall when all OI zero", zero.call_wall, None)
check("no put wall when all OI zero", zero.put_wall, None)
check("has_walls False on empty chain", zero.has_walls, False)
check("zero-OI warns", any("no call open interest" in x for x in zero.warnings), True)
check("no ratio when no call OI", zero.put_call_oi_ratio, None)

# --- empty input ------------------------------------------------------
empty = compute_walls([], expiry="x")
check("empty chain has no walls", empty.has_walls, False)
check("empty chain strikes 0", empty.strikes_considered, 0)

# --- malformed rows are skipped and reported -------------------------
bad = compute_walls(
    [opt(100, 500, "CALL"),
     {"option_strike_price": None, "option_open_interest": 999, "option_type": "CALL"},
     {"option_strike_price": 105, "option_open_interest": 5, "option_type": "WARRANT"},
     {"option_open_interest": 5}],
    expiry="x", spot=100.0,
)
check("valid row still counted", bad.call_wall, 100.0)
check("malformed rows skipped", bad.total_call_oi, 500)
check("skips are reported", any("skipped" in x for x in bad.warnings), True)

# --- missing spot leaves distances undefined, not zero ---------------
nospot = compute_walls(rows, expiry="x", spot=None)
check("no spot -> call distance None", nospot.call_wall_distance_pct, None)
check("no spot -> put distance None", nospot.put_wall_distance_pct, None)
check("walls still found without spot", nospot.call_wall, 110.0)

# --- OI arriving as float/str (SDK is loosely typed) -----------------
loose = compute_walls(
    [{"option_strike_price": "100", "option_open_interest": "250.0", "option_type": "call"}],
    expiry="x", spot=100.0,
)
check("string strike parsed", loose.call_wall, 100.0)
check("string OI parsed", loose.call_wall_oi, 250)

# --- tie between strikes resolves to the lower strike (deterministic) -
tie = compute_walls([opt(100, 500), opt(110, 500)], expiry="x", spot=100.0)
check("tie resolves deterministically to lower strike", tie.call_wall, 100.0)

# --- walls rank on OI + volume, not OI alone -------------------------
# 100 has more standing OI, but 110 has far more same-day flow, so 110 is
# the live wall. OI alone would have picked 100 and missed today's action.
vw = compute_walls(
    [opt(100, 900, "CALL", volume=10), opt(110, 400, "CALL", volume=800)],
    expiry="x", spot=100.0,
)
check("volume can outweigh stale OI", vw.call_wall, 110.0)
check("wall score is oi + volume", vw.call_wall_score, 1200)
check("wall reports its OI component", vw.call_wall_oi, 400)
check("wall reports its volume component", vw.call_wall_volume, 800)

# volume_weight=0 must reproduce the old OI-only behaviour exactly.
oi_only = compute_walls(
    [opt(100, 900, "CALL", volume=10), opt(110, 400, "CALL", volume=800)],
    expiry="x", spot=100.0, volume_weight=0.0,
)
check("volume_weight=0 falls back to OI alone", oi_only.call_wall, 100.0)

# A brand-new expiry that has traded but not yet settled OI still has a wall.
fresh = compute_walls(
    [opt(100, 0, "CALL", volume=250), opt(105, 0, "CALL", volume=40)],
    expiry="x", spot=100.0,
)
check("volume alone establishes a wall", fresh.call_wall, 100.0)
check("zero-OI wall reports OI 0", fresh.call_wall_oi, 0)

# Totals and the volume-side P/C ratio are tracked separately from OI.
both = compute_walls(
    [opt(100, 500, "CALL", volume=200), opt(90, 300, "PUT", volume=100)],
    expiry="x", spot=100.0,
)
check("total call volume", both.total_call_volume, 200)
check("total put volume", both.total_put_volume, 100)
check("put/call volume ratio", both.put_call_volume_ratio, 0.5)
check("put/call OI ratio still on OI", both.put_call_oi_ratio, 0.6)
check("no volume ratio when no call volume",
      compute_walls([opt(100, 500, "CALL")], expiry="x").put_call_volume_ratio, None)

# Duplicate strikes must aggregate volume as well as OI.
dupv = compute_walls(
    [opt(100, 100, "CALL", volume=30), opt(100, 200, "CALL", volume=70)],
    expiry="x", spot=100.0,
)
check("duplicate strikes aggregate volume", dupv.call_wall_volume, 100)
check("duplicate strikes aggregate score", dupv.call_wall_score, 400)

# Ties on the combined score still resolve to the lower strike.
tie2 = compute_walls(
    [opt(100, 300, "CALL", volume=200), opt(110, 200, "CALL", volume=300)],
    expiry="x", spot=100.0,
)
check("score ties resolve to lower strike", tie2.call_wall, 100.0)

# 'N/A' is a real value from this SDK build (option_net_open_interest).
na = compute_walls(
    [{"option_strike_price": 100, "option_open_interest": "N/A",
      "volume": "N/A", "option_type": "CALL"},
     opt(105, 50, "CALL", volume=5)],
    expiry="x", spot=100.0,
)
check("'N/A' coerces to 0, not a crash", na.call_wall, 105.0)

report("options_walls")
