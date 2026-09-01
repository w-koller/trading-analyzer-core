"""Reconciliation: do the service modules' outputs actually fit db.py's schema?

Run from backend/:  .venv/bin/python -m tests.test_db_reconciliation

market_hours, indicators, options_walls and moomoo_gateway were written
before db.py existed. This proves their outputs survive a real round trip
through the real schema — `indicator_snapshot` as JSON, `feature_vector` as
a float list, `data_as_of` as TEXT, the CHECK constraints, and the foreign
key from trade_setups.code back to watchlist_cache.

Runs against a throwaway SQLite file, never the live database.
"""

import json
import math
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import db

# Redirect every connection at a scratch file BEFORE anything touches it.
_tmp = tempfile.mkdtemp(prefix="reconcile-")
db.DB_PATH = Path(_tmp) / "test.db"

from app.services.indicators import compute                      # noqa: E402
from app.services.options_walls import compute_walls             # noqa: E402
from app.services.similarity import build_feature_vector         # noqa: E402
from app.utils import market_hours                               # noqa: E402

from tests.harness import check, report  # noqa: E402


db.init_db()
check("init_db creates schema on a fresh file", db.DB_PATH.exists())

# --- build a realistic setup from the actual service modules ----------
closes = [100 + i * 0.4 + (3 if i % 7 == 0 else 0) for i in range(260)]
bars = pd.DataFrame({
    "time_key": pd.date_range("2025-01-01", periods=260, freq="D").astype(str),
    "open": closes, "high": [c * 1.01 for c in closes],
    "low": [c * 0.99 for c in closes], "close": closes,
    "volume": [1_000_000] * 260,
})
ind = compute(bars)
walls = compute_walls(
    [{"option_strike_price": 110, "option_open_interest": 900, "volume": 120, "option_type": "CALL"},
     {"option_strike_price": 95, "option_open_interest": 700, "volume": 80, "option_type": "PUT"}],
    expiry="2026-09-18", spot=ind.close,
)

# --- indicator_snapshot must be a JSON-serialisable dict --------------
snapshot = {"indicators": ind.to_dict(), "walls": walls.to_dict()}
try:
    encoded = json.dumps(snapshot)
    check("IndicatorSnapshot + OptionWalls serialise to JSON", True,
          f"{len(encoded)} bytes")
except TypeError as exc:
    check("IndicatorSnapshot + OptionWalls serialise to JSON", False, str(exc))

check("no NaN leaks into the JSON blob",
      "NaN" not in encoded and "Infinity" not in encoded)

# --- feature_vector must be a plain list of finite floats -------------
vec = build_feature_vector(ind, walls)
check("feature_vector is list[float]",
      isinstance(vec, list) and all(isinstance(x, float) for x in vec))
check("feature_vector components finite and bounded",
      all(math.isfinite(x) and -1.0 <= x <= 1.0 for x in vec))

# --- market_hours -> schema types -------------------------------------
code = "US.PLTR"
market = market_hours.market_of(code)
as_of = market_hours.data_as_of(market)
check("market_of returns a value the CHECK accepts", market in ("US", "HK", "AU"),
      f"market={market!r}")
check("data_as_of returns datetime, needs .isoformat() for TEXT",
      isinstance(as_of, datetime), type(as_of).__name__)
check("data_as_of is timezone-aware UTC", as_of.tzinfo is not None)
check("is_delayed_data is bool -> int for the schema",
      isinstance(market_hours.is_delayed_data(market), bool))

# --- the foreign key: a setup needs its ticker in watchlist_cache -----
try:
    db.insert_trade_setup(
        scanner_run_id=None, code="US.NOPE", market="US",
        data_as_of=as_of.isoformat(), is_delayed_data=False,
        indicator_snapshot=snapshot, feature_vector=vec,
        trade_direction="Bullish", conviction_score=5, reasoning="x",
        suggested_entry=None, suggested_stop=None, suggested_target=None,
        key_levels_notes=None, similar_setup_ids=[],
    )
    check("unknown ticker is rejected by the FK", False, "insert succeeded")
except sqlite3.IntegrityError:
    check("unknown ticker is rejected by the FK", True,
          "watchlist sync must run before the scanner")

# --- full round trip ---------------------------------------------------
db.upsert_watchlist_ticker(code=code, name="Palantir", market="US")
run_id = db.insert_scanner_run()
setup_id = db.insert_trade_setup(
    scanner_run_id=run_id, code=code, market=market,
    data_as_of=as_of.isoformat(),
    is_delayed_data=market_hours.is_delayed_data(market),
    indicator_snapshot=snapshot, feature_vector=vec,
    trade_direction="Bullish", conviction_score=7,
    reasoning="One. Two. Three.",
    suggested_entry=round(ind.close * 0.98, 2),
    suggested_stop=round(ind.close * 0.95, 2),
    suggested_target=round(ind.close * 1.08, 2),
    key_levels_notes="call wall 110", similar_setup_ids=[],
)
check("insert_trade_setup accepts the real payload", setup_id > 0, f"id={setup_id}")

row = db.get_latest_setup_for_code(code)
check("round trip returns the row", row is not None)
check("indicator_snapshot round-trips as JSON",
      json.loads(row["indicator_snapshot"])["indicators"]["close"] == ind.close)
check("suggested_entry round-trips as a REAL",
      row["suggested_entry"] == round(ind.close * 0.98, 2),
      f"{row['suggested_entry']}")
check("the three levels come back ordered as the validator required them",
      row["suggested_stop"] < row["suggested_entry"] < row["suggested_target"])
check("feature_vector round-trips exactly", json.loads(row["feature_vector"]) == vec)
check("is_delayed_data stored as int", row["is_delayed_data"] == 0)
check("data_as_of stored as TEXT", isinstance(row["data_as_of"], str))

# --- timestamps must share one comparable format ----------------------
db.log_outcome(setup_id=setup_id, source="manual", entry_price=100.0,
               exit_price=110.0, pnl_abs=10.0, pnl_pct=10.0, exit_reason="target")
with db.get_connection() as conn:
    s_ts = conn.execute("SELECT created_at FROM trade_setups").fetchone()[0]
    o_ts = conn.execute("SELECT created_at FROM trade_outcomes").fetchone()[0]
check("setup and outcome timestamps share a format",
      len(s_ts) == len(o_ts) and ("T" in s_ts) == ("T" in o_ts),
      f"setup={s_ts} outcome={o_ts}")
check("timestamps sort lexicographically as chronological",
      sorted([s_ts, o_ts]) == [s_ts, o_ts])

# --- RAG retrieval (rule #3) -------------------------------------------
similar = db.get_similar_setups(vec, top_k=3)
check("get_similar_setups accepts our vector", len(similar) == 1, f"{len(similar)} hits")
check("retrieved setup carries its realized outcome",
      similar[0]["outcome"] is not None and similar[0]["outcome"]["pnl_pct"] == 10.0)
check("self-similarity is 1.0", similar[0]["similarity"] == 1.0)

# A length-mismatched vector (an older FEATURE_VERSION) scores 0, which is
# below the similarity floor — so it is dropped rather than injected. Scoring
# 0 and still being handed to the model as a "similar setup" was the old
# behaviour; returning nothing is the honest answer.
check("stale-length vector scores 0 without crashing",
      db.get_similar_setups(vec[:-2], top_k=3, min_similarity=-1.0)[0]["similarity"] == 0.0)
check("a 0-similarity vector is not injected as a precedent",
      db.get_similar_setups(vec[:-2], top_k=3) == [])
check("exclude_setup_id filters self out",
      db.get_similar_setups(vec, exclude_setup_id=setup_id) == [])

# The floor itself: an unrelated setup scoring below it must be dropped, even
# when it is the only candidate. With one outcome on record this was returning
# that row for every ticker at ~0.89-0.95 and calling it similar.
check("a below-floor candidate is dropped even when it is the only one",
      db.get_similar_setups(vec, top_k=3, min_similarity=1.01) == [])

# --- constraint enforcement -------------------------------------------
for bad_score in (0, 11):
    try:
        db.insert_trade_setup(
            scanner_run_id=run_id, code=code, market="US",
            data_as_of=as_of.isoformat(), is_delayed_data=False,
            indicator_snapshot=snapshot, feature_vector=vec,
            trade_direction="Bullish", conviction_score=bad_score,
            reasoning="x", suggested_entry=None, suggested_stop=None,
            suggested_target=None,
            key_levels_notes=None, similar_setup_ids=[],
        )
        check(f"conviction_score {bad_score} rejected", False, "insert succeeded")
    except (ValueError, sqlite3.IntegrityError):
        check(f"conviction_score {bad_score} rejected", True)

try:
    db.upsert_watchlist_ticker(code="SG.D05", name="DBS", market="SG")
    check("unsupported market rejected by CHECK", False, "insert succeeded")
except sqlite3.IntegrityError:
    check("unsupported market rejected by CHECK", True,
          "scanner must filter to US/HK/AU")

# --- rule #4: system groups are read-only ------------------------------
db.upsert_group("sys-1", "US", is_system=True)
db.upsert_group("cus-1", "To Buy", is_system=False)
for fn, label in ((db.add_ticker_to_group, "add"), (db.remove_ticker_from_group, "remove")):
    try:
        fn("sys-1", code)
        check(f"{label} to system group blocked", False, "call succeeded")
    except ValueError:
        check(f"{label} to system group blocked", True)
db.add_ticker_to_group("cus-1", code)
check("custom group accepts writes", len(db.get_group_members("cus-1")) == 1)

db.finish_scanner_run(run_id, 1, 1, 0)

report("db reconciliation")
