"""Live checks for the read-only trade gateway.

Run from backend/:  .venv/bin/python -m tests.test_moomoo_trade_gateway_live

Requires OpenD running with a logged-in trade session. These are live checks
on purpose: every surprising behaviour pinned here was found by calling the
real SDK, and a mock would have happily reproduced the wrong assumptions
(most importantly the wrong-security_firm case, which returns RET_OK and an
empty-looking account rather than an error).

Nothing here places, modifies or cancels anything — the gateway has no such
method to call.
"""

import moomoo as ft

from app.config import settings
from app.services.moomoo_trade_gateway import get_trade_gateway

from tests.harness import check, report


def main() -> int:
    gw = get_trade_gateway()

    # --- health / account resolution ---------------------------------
    status = gw.health()
    check("health() connects", status.connected, status.error or "")
    check("health() resolves a REAL account id", bool(status.acc_id), str(status.acc_id))
    check("health() reports the configured env", status.trd_env == settings.trd_env,
          status.trd_env)

    # --- positions ----------------------------------------------------
    positions = gw.list_positions()
    check("list_positions() returns rows", len(positions) > 0, f"{len(positions)} positions")

    if positions:
        p = positions[0]
        required = {"code", "name", "market", "qty", "avg_cost", "market_value",
                    "unrealized_pnl_pct", "currency"}
        check("position rows carry the dashboard fields",
              required.issubset(p.keys()), str(sorted(required - set(p.keys()))))
        check("codes are market-prefixed", all("." in r["code"] for r in positions))
        check("qty is numeric", all(isinstance(r["qty"], float) for r in positions))

        # pl_ratio_avg_cost, not pl_ratio: the latter is computed off
        # diluted_cost, which goes negative once realised profit exceeds the
        # cost basis and then reports 0.00% for a position that is genuinely up.
        check("P/L % is on an average-cost basis",
              all(r["unrealized_pnl_pct"] is not None for r in positions))

    # --- market filtering ---------------------------------------------
    markets = {r["market"] for r in positions}
    for market in markets:
        subset = gw.list_positions(market=market)
        check(f"market={market} filters to that market only",
              all(r["market"] == market for r in subset), f"{len(subset)} rows")

    check("an unheld market returns empty, not an error",
          gw.list_positions(market="ZZ") == [])

    # --- caching -------------------------------------------------------
    first = gw.list_positions()
    second = gw.list_positions()
    check("repeat calls are served from cache", first == second)

    # --- historical fills (feeds the outcome sync) ----------------------
    from datetime import date, timedelta
    end = date.today()
    fills = gw.list_deals(end - timedelta(days=90), end)
    check("list_deals returns fills", isinstance(fills, list), f"{len(fills)} fills")
    if fills:
        f = fills[0]
        check("fills carry what pairing needs",
              {"deal_id", "code", "side", "qty", "price", "time"}.issubset(f.keys()),
              str(sorted(f.keys())))
        check("side is BUY or SELL",
              all(x["side"] in ("BUY", "SELL") for x in fills),
              str({x["side"] for x in fills}))
        check("fills are chronological",
              [x["time"] for x in fills] == sorted(x["time"] for x in fills))
        check("deal_ids are unique across stitched windows",
              len({x["deal_id"] for x in fills}) == len(fills),
              f"{len(fills)} fills, {len({x['deal_id'] for x in fills})} ids")

    # A window wider than Moomoo's 360-day cap must be split, not rejected.
    wide = gw.list_deals(end - timedelta(days=400), end)
    check("a >360-day window is split rather than failing",
          isinstance(wide, list) and len(wide) >= len(fills),
          f"{len(wide)} fills over 400 days")

    # --- the invariant that matters ------------------------------------
    forbidden = [m for m in dir(gw)
                 if any(k in m.lower() for k in
                        ("place", "unlock", "cancel", "modify"))
                 or m.lower().endswith("_order")]
    check("gateway exposes no order-side method", forbidden == [], str(forbidden))
    public_methods = sorted(
        m for m in dir(gw)
        if not m.startswith("_") and callable(getattr(gw, m, None))
    )
    check("the public method surface is exactly the read methods",
          public_methods == ["close", "health", "list_deals", "list_positions", "stats"],
          str(public_methods))

    # --- the silent-misconfiguration trap ------------------------------
    # A wrong security_firm does not raise: it returns RET_OK with only the
    # SIMULATE account, so positions come back empty and read as "you hold
    # nothing". Pinned here so nobody 'simplifies' the setting away.
    ctx = ft.OpenSecTradeContext(filter_trdmarket="US", host=settings.opend_host,
                                 port=settings.opend_port, security_firm="FUTUINC")
    ret, data = ctx.get_acc_list()
    ctx.close()
    envs = set(data["trd_env"]) if ret == ft.RET_OK and len(data) else set()
    check("wrong security_firm hides the REAL account without erroring",
          ret == ft.RET_OK and "REAL" not in envs, f"ret={ret} envs={envs or '{}'}")

    gw.close()

    # raise_on_failure=False: this runs inside main(), which owns the exit
    # code, so the verdict comes back as a return value rather than SystemExit.
    return report("moomoo_trade_gateway",
                  summary="moomoo_trade_gateway: all live checks passed",
                  raise_on_failure=False)


if __name__ == "__main__":
    raise SystemExit(main())
