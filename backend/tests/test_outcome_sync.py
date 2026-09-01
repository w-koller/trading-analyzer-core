"""Checks for fill-to-round-trip pairing, against hand-computed arithmetic.

Run from backend/:  .venv/bin/python -m tests.test_outcome_sync

Expected values are worked out by hand in the comments rather than taken from
the code, because this is the one place where a plausible-looking wrong
number does real damage: a bad pnl_pct becomes RAG context and then steers
every future thesis on a similar setup.

Pure functions over fixture fills — no OpenD, no network.
"""

from app.services.outcome_sync import pair_fills

from tests.harness import check, report


def fill(code, side, qty, price, time, deal_id=None, status="OK"):
    return {"code": code, "side": side, "qty": float(qty), "price": float(price),
            "time": time, "deal_id": deal_id or f"{code}-{side}-{time}",
            "status": status, "order_id": "o1"}


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) < tol


# --- simplest possible round trip --------------------------------------
# Buy 100 @ 10, sell 100 @ 12 -> +2/share = +20 abs, +20.00%
r = pair_fills([
    fill("US.A", "BUY", 100, 10.0, "2026-01-01 10:00:00"),
    fill("US.A", "SELL", 100, 12.0, "2026-01-03 10:00:00"),
])
check("one buy and one sell make one outcome", len(r.outcomes) == 1, str(r.to_dict()))
o = r.outcomes[0]
check("entry price is the cost basis", close(o["entry_price"], 10.0), str(o["entry_price"]))
check("exit price is the sell price", close(o["exit_price"], 12.0))
check("pnl_abs = (12-10)*100 = 200", close(o["pnl_abs"], 200.0), str(o["pnl_abs"]))
check("pnl_pct = +20.00%", close(o["pnl_pct"], 20.0), str(o["pnl_pct"]))
check("hold time is 48h", close(o["hold_time_hours"], 48.0), str(o["hold_time_hours"]))
check("opened_at is the BUY, closed_at is the SELL",
      o["opened_at"].startswith("2026-01-01") and o["closed_at"].startswith("2026-01-03"))

# --- a loss, to be sure the sign is right ------------------------------
# Buy 50 @ 20, sell 50 @ 18 -> -2/share = -100 abs, -10.00%
r = pair_fills([
    fill("US.B", "BUY", 50, 20.0, "2026-02-01 10:00:00"),
    fill("US.B", "SELL", 50, 18.0, "2026-02-02 10:00:00"),
])
check("a losing trade is negative", close(r.outcomes[0]["pnl_abs"], -100.0),
      str(r.outcomes[0]["pnl_abs"]))
check("a losing pct is -10.00%", close(r.outcomes[0]["pnl_pct"], -10.0))

# --- tranched entry: the averaging is the whole point -------------------
# Buy 100 @ 10 then 100 @ 20 -> 200 @ avg 15. Sell 200 @ 18:
#   pnl = (18-15)*200 = 600, pct = 3/15 = +20.00%
r = pair_fills([
    fill("US.C", "BUY", 100, 10.0, "2026-03-01 10:00:00"),
    fill("US.C", "BUY", 100, 20.0, "2026-03-02 10:00:00"),
    fill("US.C", "SELL", 200, 18.0, "2026-03-05 10:00:00"),
])
o = r.outcomes[0]
check("two tranches average to 15", close(o["entry_price"], 15.0), str(o["entry_price"]))
check("tranched pnl_abs = 600", close(o["pnl_abs"], 600.0), str(o["pnl_abs"]))
check("tranched pnl_pct = +20.00%", close(o["pnl_pct"], 20.0), str(o["pnl_pct"]))
check("hold time runs from the FIRST buy", close(o["hold_time_hours"], 96.0),
      str(o["hold_time_hours"]))

# --- partial close, then the rest ---------------------------------------
# Buy 100 @ 10. Sell 40 @ 15 -> (5)*40 = 200. Then sell 60 @ 20 -> (10)*60 = 600.
r = pair_fills([
    fill("US.D", "BUY", 100, 10.0, "2026-04-01 10:00:00"),
    fill("US.D", "SELL", 40, 15.0, "2026-04-02 10:00:00"),
    fill("US.D", "SELL", 60, 20.0, "2026-04-03 10:00:00"),
])
check("each closing fill is its own outcome", len(r.outcomes) == 2, str(len(r.outcomes)))
check("partial close pnl = 200", close(r.outcomes[0]["pnl_abs"], 200.0),
      str(r.outcomes[0]["pnl_abs"]))
check("the remainder keeps the SAME cost basis",
      close(r.outcomes[1]["entry_price"], 10.0), str(r.outcomes[1]["entry_price"]))
check("final close pnl = 600", close(r.outcomes[1]["pnl_abs"], 600.0),
      str(r.outcomes[1]["pnl_abs"]))
check("a partial close is labelled as one",
      r.outcomes[0]["exit_reason"] == "partial close", r.outcomes[0]["exit_reason"])
check("a full close is labelled as one",
      r.outcomes[1]["exit_reason"] == "closed on Moomoo", r.outcomes[1]["exit_reason"])

# --- multiple fills of one order (the real data does this) --------------
# AU.NXT sold in three fills of the same order; each realises separately.
r = pair_fills([
    fill("AU.X", "BUY", 360, 14.67, "2026-08-13 10:35:00"),
    fill("AU.X", "SELL", 107, 14.22, "2026-08-19 10:56:36.550"),
    fill("AU.X", "SELL", 175, 14.22, "2026-08-19 10:56:36.656"),
    fill("AU.X", "SELL", 78, 14.21, "2026-08-19 10:56:36.675"),
])
check("split fills of one order produce one outcome each", len(r.outcomes) == 3)
check("every split fill shares the same entry basis",
      all(close(o["entry_price"], 14.67) for o in r.outcomes))
total = sum(o["pnl_abs"] for o in r.outcomes)
# (14.22-14.67)*282 + (14.21-14.67)*78 = -126.9 + -35.88 = -162.78
check("split fills sum to the hand-computed total", close(total, -162.78, 1e-4),
      f"{total:.4f}")

# --- reopening after a flat position ------------------------------------
r = pair_fills([
    fill("US.E", "BUY", 10, 100.0, "2026-05-01 10:00:00"),
    fill("US.E", "SELL", 10, 110.0, "2026-05-02 10:00:00"),
    fill("US.E", "BUY", 10, 50.0, "2026-06-01 10:00:00"),
    fill("US.E", "SELL", 10, 60.0, "2026-06-02 10:00:00"),
])
check("a reopened position starts a fresh basis", len(r.outcomes) == 2
      and close(r.outcomes[1]["entry_price"], 50.0),
      str([o["entry_price"] for o in r.outcomes]))
check("the second holding period does not inherit the first's open time",
      r.outcomes[1]["opened_at"].startswith("2026-06-01"),
      str(r.outcomes[1]["opened_at"]))

# --- a sell with no position must NOT invent a cost basis ---------------
r = pair_fills([fill("US.F", "SELL", 10, 100.0, "2026-07-01 10:00:00")])
check("a sell with no position produces no outcome", r.outcomes == [])
check("and is counted for follow-up", r.skipped_sells_without_position == 1,
      str(r.to_dict()))

# Overselling closes what exists and refuses to short the remainder.
r = pair_fills([
    fill("US.G", "BUY", 10, 10.0, "2026-07-01 10:00:00"),
    fill("US.G", "SELL", 25, 12.0, "2026-07-02 10:00:00"),
])
check("overselling realises only the qty actually held",
      len(r.outcomes) == 1 and close(r.outcomes[0]["pnl_abs"], 20.0),
      str(r.outcomes[0]["pnl_abs"]) if r.outcomes else "none")
check("the un-owned remainder is not treated as a short",
      r.skipped_sells_without_position == 1, str(r.to_dict()))

# --- garbage in ----------------------------------------------------------
r = pair_fills([
    fill("US.H", "BUY", 0, 10.0, "2026-07-01 10:00:00"),
    {"code": "US.H", "side": "BUY", "qty": None, "price": 1.0,
     "time": "2026-07-01 10:00:00", "status": "OK"},
    fill("US.H", "CANCEL", 5, 10.0, "2026-07-01 11:00:00"),
])
check("unusable fills are counted, not crashed on", r.unusable_fills == 3,
      str(r.to_dict()))

# Cancelled/failed fills must never reach the pairing.
r = pair_fills([
    fill("US.I", "BUY", 10, 10.0, "2026-07-01 10:00:00"),
    fill("US.I", "SELL", 10, 12.0, "2026-07-02 10:00:00", status="CANCELLED"),
])
check("a cancelled sell does not realise a profit", r.outcomes == [],
      str(r.to_dict()))

# --- tickers do not bleed into each other --------------------------------
r = pair_fills([
    fill("US.J", "BUY", 10, 10.0, "2026-07-01 10:00:00"),
    fill("US.K", "BUY", 10, 99.0, "2026-07-01 10:30:00"),
    fill("US.J", "SELL", 10, 11.0, "2026-07-02 10:00:00"),
])
check("one ticker's basis never leaks into another",
      len(r.outcomes) == 1 and close(r.outcomes[0]["entry_price"], 10.0),
      str(r.outcomes))

# --- out-of-order input --------------------------------------------------
r = pair_fills([
    fill("US.L", "SELL", 10, 12.0, "2026-07-02 10:00:00"),
    fill("US.L", "BUY", 10, 10.0, "2026-07-01 10:00:00"),
])
check("fills are sorted before pairing, not trusted in order",
      len(r.outcomes) == 1 and close(r.outcomes[0]["pnl_abs"], 20.0),
      str(r.to_dict()))

report("outcome_sync")
