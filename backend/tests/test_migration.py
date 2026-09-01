"""The one migration step: does an EXISTING database gain a new column?

Run from backend/:  .venv/bin/python -m tests.test_migration

Every other suite in this directory starts from a fresh `_SCHEMA`, which
already contains `suggested_entry` — so none of them exercise the path that
actually matters. The live database was created before the column existed,
`init_db()` runs only CREATE TABLE IF NOT EXISTS, and the failure mode is
silent until the first scan: the table keeps its old shape and every INSERT
naming the new column raises `no column named suggested_entry`.

This builds a database in the PRE-migration shape and migrates it, which is
the only way to see that happen before the live box does.

Runs against a throwaway SQLite file, never the live database.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="migration-")
db.DB_PATH = Path(_tmp) / "test.db"

from tests.harness import check, check_eq, report  # noqa: E402


def columns(table: str) -> set[str]:
    with db.get_connection() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# --- build the database as it existed BEFORE the column ----------------
# Derived from the real _SCHEMA rather than hand-written, so this cannot
# drift into testing a table shape the project never actually had: take the
# live schema and delete exactly the line the migration is meant to add back.
pre = db._SCHEMA.replace("    suggested_entry      REAL,\n", "")
check("the fixture actually removed suggested_entry from the schema text",
      "suggested_entry" not in pre,
      "otherwise this suite would silently test nothing")

# Same technique for the second added column. The comment block above
# `security_type` in _SCHEMA also contains the word, so the check below
# targets the COLUMN DEFINITION rather than the substring.
_SECURITY_TYPE_DDL = "    security_type   TEXT,\n"
check("the security_type column definition is where the fixture expects it",
      _SECURITY_TYPE_DDL in pre,
      "if _SCHEMA is reformatted this fixture must be updated with it")
pre = pre.replace(_SECURITY_TYPE_DDL, "")

with db.get_connection() as conn:
    conn.executescript(pre)

check("pre-migration table lacks suggested_entry",
      "suggested_entry" not in columns("trade_setups"))
check("pre-migration table is otherwise complete",
      {"suggested_stop", "suggested_target", "reasoning"} <= columns("trade_setups"))
check("pre-migration watchlist_cache lacks security_type",
      "security_type" not in columns("watchlist_cache"))
check("pre-migration watchlist_cache is otherwise complete",
      {"code", "name", "market", "enabled"} <= columns("watchlist_cache"))

# A row written by the old code, to prove the migration does not disturb it.
with db.get_connection() as conn:
    conn.execute(
        "INSERT INTO watchlist_cache (code, market, name) VALUES (?, ?, ?)",
        ("US.PLTR", "US", "Palantir"),
    )
    conn.execute(
        """
        INSERT INTO trade_setups (
            code, market, data_as_of, is_delayed_data, indicator_snapshot,
            feature_vector, trade_direction, conviction_score, reasoning,
            suggested_stop, suggested_target, key_levels_notes, similar_setup_ids
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("US.PLTR", "US", "2026-08-26T00:00:00+00:00", 0, "{}", "[]",
         "Bullish", 6, "One. Two. Three.", 172.0, 190.0, None, "[]"),
    )

# --- the failure this exists to prevent --------------------------------
# init_db() on an existing database is a no-op for the new column. Proving
# that is the whole reason migrate_schema() has to exist at all.
db.init_db()
check("init_db alone does NOT add the column to an existing table",
      "suggested_entry" not in columns("trade_setups"),
      "CREATE TABLE IF NOT EXISTS cannot alter a table that already exists")
check("...and the same is true of security_type",
      "security_type" not in columns("watchlist_cache"))

# --- migrate -----------------------------------------------------------
added = db.migrate_schema()
check_eq("migrate_schema reports what it added", added,
         ["trade_setups.suggested_entry", "watchlist_cache.security_type"])
check("the column now exists", "suggested_entry" in columns("trade_setups"))
check("security_type now exists", "security_type" in columns("watchlist_cache"))

with db.get_connection() as conn:
    row = conn.execute("SELECT * FROM trade_setups WHERE code = 'US.PLTR'").fetchone()
check("the pre-existing row survived", row is not None)
check("its suggested_entry is NULL, not 0.0",
      row["suggested_entry"] is None,
      "a thesis written before the field existed had no entry — not an empty one")
check_eq("its other columns are untouched", row["suggested_stop"], 172.0)

with db.get_connection() as conn:
    wl = conn.execute("SELECT * FROM watchlist_cache WHERE code = 'US.PLTR'").fetchone()
check("its security_type is NULL, not a guessed 'STOCK'",
      wl["security_type"] is None,
      "a ticker synced before the column existed has an UNKNOWN type, and "
      "owner_plates must treat that as unknown rather than assume it is safe")

# The write path must accept it, and must not clobber a known value with NULL.
db.upsert_watchlist_ticker(code="US.SMH", name="VanEck Semiconductor ETF",
                           market="US", security_type="ETF")
_smh = {t["code"]: t for t in db.get_enabled_tickers()}.get("US.SMH")
check("the ETF row was written", _smh is not None)
check_eq("security_type round-trips", _smh["security_type"], "ETF")
db.upsert_watchlist_ticker(code="US.SMH", name="VanEck Semiconductor ETF", market="US")
with db.get_connection() as conn:
    again_row = conn.execute(
        "SELECT security_type FROM watchlist_cache WHERE code = 'US.SMH'").fetchone()
check_eq("a later sync without a type does NOT erase a known one",
         again_row["security_type"], "ETF")

# --- idempotency: this runs on every boot ------------------------------
again = db.migrate_schema()
check_eq("a second run adds nothing", again, [])
check("the column is still there after a second run",
      "suggested_entry" in columns("trade_setups"))

# --- and the insert path now works -------------------------------------
setup_id = db.insert_trade_setup(
    scanner_run_id=None, code="US.PLTR", market="US",
    data_as_of="2026-08-26T00:00:00+00:00", is_delayed_data=False,
    indicator_snapshot={"spot": 179.94}, feature_vector=[0.1] * 13,
    trade_direction="Bullish", conviction_score=7,
    reasoning="One. Two. Three.",
    suggested_entry=178.0, suggested_stop=172.0, suggested_target=190.0,
    key_levels_notes=None, similar_setup_ids=[],
)
check("insert_trade_setup succeeds against the migrated table", setup_id > 0)

stored = db.get_latest_setup_for_code("US.PLTR")
check_eq("the entry round-trips as a float", stored["suggested_entry"], 178.0)

with db.get_connection() as conn:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
check_eq("the migrated database passes integrity_check", integrity, "ok")

# --- the guard for a table that does not exist yet ---------------------
# migrate_schema runs before init_db has ever touched a brand-new file in
# some orderings; ALTER on a missing table raises, so it must skip instead.
db.DB_PATH = Path(_tmp) / "empty.db"
check_eq("migrating an empty database is a no-op, not an error",
         db.migrate_schema(), [])

report("migration")
