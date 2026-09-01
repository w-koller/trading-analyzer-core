"""
db.py — SQLite persistence layer for the trading analyzer.

Design notes
------------
- Single-file SQLite, WAL mode, foreign keys enforced on every connection
  (SQLite applies PRAGMA foreign_keys per-connection, not globally).
- Synchronous stdlib sqlite3, intended to be called from FastAPI route/service
  code via `starlette.concurrency.run_in_threadpool` (or a thread executor).
  WAL mode makes this safe for the scanner writing while the API reads.
- Similarity search (`get_similar_setups`) does a full-table scan + cosine
  similarity in pure Python. It reads only the six columns it scores on, not
  `SELECT *` — a setup row averages ~2KB and the feature vector is ~200 bytes
  of it. That keeps SQLite as the only moving part; revisit if the table grows
  into the tens of thousands of rows WITH OUTCOMES, which is what the scan is
  actually sized by.
- Ticker codes use Moomoo's own "MARKET.CODE" convention, e.g. "US.AAPL",
  "HK.00700", "AU.BHP". Market is also stored as its own column for
  cheap filtering/indexing.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence, TypedDict

from app.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Resolved via config.settings, not os.environ directly: pydantic-settings
# reads backend/.env without exporting into the process environment, so
# reading os.environ here silently ignored TRADING_DB_PATH and dropped the
# database wherever the current working directory happened to be.
DB_PATH = Path(settings.trading_db_path).resolve()

Market = Literal["US", "HK", "AU"]
TradeDirection = Literal["Bullish", "Bearish", "Neutral"]
OutcomeSource = Literal["moomoo", "manual"]
ScannerStatus = Literal["running", "completed", "failed"]
# The reader's three questions, not a publisher taxonomy. See news_feeds.py.
NewsCategory = Literal["shocks", "themes", "macro"]
# How an article got linked to a ticker. Recorded because a wrong link feeds
# the AI someone else's news, and this column is what lets a noisy basis be
# excluded later with a WHERE clause instead of a re-ingest.
NewsMatchBasis = Literal["feed_query", "company_name"]
FeedStatus = Literal["ok", "http_error", "unparseable", "empty", "timeout", "unknown"]


# These three are PUBLIC on purpose. They were `_`-prefixed and then called
# from six other modules anyway, which is the worst of both worlds: the name
# says "do not depend on this" while six things depend on it. They are part of
# db's interface — every timestamp in the schema is written by now_iso() and
# read by parse_iso(), so anything comparing against a stored timestamp has to
# use the same pair or it is comparing different things.


def now_iso() -> str:
    """The one timestamp format every write in this schema uses.

    Returns:
        UTC ISO-8601 to SECOND granularity, e.g. "2026-08-25T14:03:11+00:00".

    The granularity is load-bearing in both directions: one scan writes
    several rows inside the same second, so `created_at` alone cannot order
    them (see routers/setups.py's tie-break on id), and the trailing "+00:00"
    means a consumer must never append "Z" — that yields an invalid Date whose
    comparisons are silently false rather than erroring.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(raw: str | None) -> datetime | None:
    """Parse a stored timestamp back to an aware datetime.

    Args:
        raw: a timestamp as stored — either now_iso()'s offset-qualified form
            or SQLite's own bare `datetime('now')` output. Both shapes exist
            in this database, so both are accepted.

    Returns:
        A timezone-aware UTC datetime, or None if `raw` is empty or
        unparseable. None rather than an exception because callers use this
        for range maths, where an unreadable timestamp should widen the search
        rather than crash a sync.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace(" ", "T"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------

@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    Context-managed SQLite connection.

    - WAL journal mode (readers don't block the writer, and vice versa).
    - Foreign keys ON (must be set per-connection).
    - Row factory returns sqlite3.Row so callers can access columns by name.
    - Commits on clean exit, rolls back on exception, always closes.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    # FULL, not NORMAL. Under WAL, NORMAL survives a process crash but can
    # lose the last transactions to a power cut or an LXC hard-stop — the
    # characteristic failure of a Proxmox homelab, and the one case where
    # losing a thesis matters. The extra fsync is unmeasurable here: each row
    # is preceded by 60-120s of model inference, so write volume is trivial.
    conn.execute("PRAGMA synchronous = FULL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups_cache (
    group_id        TEXT PRIMARY KEY,
    group_name      TEXT NOT NULL,
    is_system       INTEGER NOT NULL DEFAULT 0,   -- 1 = Moomoo system group, read-only for writes
    last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_cache (
    code            TEXT PRIMARY KEY,             -- e.g. "US.AAPL", "HK.00700", "AU.BHP"
    name            TEXT NOT NULL,
    market          TEXT NOT NULL CHECK (market IN ('US','HK','AU')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- Moomoo's own 'STOCK' / 'ETF' classification, straight from
    -- get_user_security. Load-bearing rather than decorative: get_owner_plate
    -- fails the WHOLE batch on one ETF, so this is what keeps ETFs out of
    -- those batches. NULL means "synced before this column existed".
    security_type   TEXT,
    last_synced_at  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))
);

-- Many-to-many: a ticker can belong to several Moomoo groups at once.
CREATE TABLE IF NOT EXISTS watchlist_group_members (
    group_id  TEXT NOT NULL REFERENCES groups_cache(group_id) ON DELETE CASCADE,
    code      TEXT NOT NULL REFERENCES watchlist_cache(code) ON DELETE CASCADE,
    PRIMARY KEY (group_id, code)
);

CREATE TABLE IF NOT EXISTS scanner_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    tickers_scanned     INTEGER NOT NULL DEFAULT 0,
    tickers_succeeded    INTEGER NOT NULL DEFAULT 0,
    tickers_failed      INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','completed','failed')),
    error_summary       TEXT
);

CREATE TABLE IF NOT EXISTS trade_setups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    scanner_run_id       INTEGER REFERENCES scanner_runs(id) ON DELETE SET NULL,
    code                 TEXT NOT NULL REFERENCES watchlist_cache(code) ON DELETE CASCADE,
    market               TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
    data_as_of           TEXT NOT NULL,        -- timestamp of the bar data actually used
    is_delayed_data      INTEGER NOT NULL DEFAULT 0,  -- 1 for ASX 15-min-delayed data
    indicator_snapshot   TEXT NOT NULL,        -- JSON: SMA cross state, MACD, BB width pct, walls, distances
    feature_vector       TEXT NOT NULL,        -- JSON array[float], normalized, for cosine similarity
    trade_direction      TEXT NOT NULL CHECK (trade_direction IN ('Bullish','Bearish','Neutral')),
    conviction_score     INTEGER NOT NULL CHECK (conviction_score BETWEEN 1 AND 10),
    reasoning            TEXT NOT NULL,
    suggested_entry      REAL,
    suggested_stop       REAL,
    suggested_target     REAL,
    key_levels_notes     TEXT,
    similar_setup_ids    TEXT                  -- JSON array of setup ids injected into the prompt (audit trail)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id         INTEGER NOT NULL REFERENCES trade_setups(id) ON DELETE CASCADE,
    source           TEXT NOT NULL CHECK (source IN ('moomoo','manual')),
    moomoo_deal_id   TEXT UNIQUE,               -- dedup key when synced from Moomoo; NULL for manual entries
    entry_price      REAL,
    exit_price       REAL,
    pnl_abs          REAL,
    pnl_pct          REAL,
    hold_time_hours  REAL,
    exit_reason      TEXT,
    opened_at        TEXT,
    closed_at        TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))
);

-- How a past thesis actually turned out, measured against the bars that came
-- after it. NOT trade_outcomes: that table means "how did this thesis turn
-- out for a real trade someone made", requires a matching Moomoo deal, and
-- by decisions #36 can only ever grow prospectively. This one needs no trade
-- at all — every setup stores the price at thesis time and the daily klines
-- contain what happened next, so the model's directional record is
-- measurable today.
--
-- Persisted rather than computed on demand for the reason decisions #45
-- gives about feed health: an in-memory scorecard resets on every restart,
-- destroying exactly the accumulated history that makes it worth having.
--
-- One row per (setup, horizon). `resolution` is the path-dependent answer —
-- which of the thesis's own stop/target the price reached first.
CREATE TABLE IF NOT EXISTS setup_scores (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id          INTEGER NOT NULL REFERENCES trade_setups(id) ON DELETE CASCADE,
    horizon_days      INTEGER NOT NULL,
    entry_price       REAL NOT NULL,        -- indicator_snapshot.spot at thesis time
    exit_price        REAL,                 -- close `horizon_days` trading bars later
    forward_return_pct REAL,
    -- 1 = the thesis's direction was right over this horizon, 0 = wrong,
    -- NULL = Neutral, which makes no directional claim to be scored.
    directional_hit   INTEGER,
    -- 'target_first' | 'stop_first' | 'unresolved' | NULL when the thesis
    -- gave no stop/target to test.
    resolution        TEXT,
    bars_used         INTEGER NOT NULL,
    scored_at         TEXT NOT NULL,
    UNIQUE (setup_id, horizon_days)
);

-- Small key/value store for state that must outlive a restart. Added as a
-- new table rather than columns on an existing one, because init_db() only
-- runs CREATE TABLE IF NOT EXISTS, so altering an existing table would
-- silently not apply on this box. `migrate_schema()` below now covers that
-- for one specific case, but it handles only additive nullable columns —
-- a new table remains the right shape for anything with structure to it.
CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Ingested public RSS/Atom items. Read as context and as a reading list;
-- nothing here is parsed for numbers or allowed to drive an indicator (rule #1).
CREATE TABLE IF NOT EXISTS news_articles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key           TEXT NOT NULL UNIQUE,   -- sha256 of the normalised URL, else feed_key|title_norm
    url                 TEXT,                   -- entry.link; NULL only if the feed gave none
    title               TEXT NOT NULL,
    title_norm          TEXT NOT NULL,          -- lowercased, punctuation/whitespace collapsed
    summary             TEXT,
    feed_key            TEXT NOT NULL,          -- registry key, e.g. 'sec_8k'
    source_label        TEXT NOT NULL,          -- display name, e.g. 'SEC EDGAR'
    category            TEXT NOT NULL CHECK (category IN ('shocks','themes','macro')),
    -- ALWAYS now_iso()'s exact shape: UTC, '+00:00', seconds. SQLite sorts
    -- TEXT lexicographically, and '...Z', '...+00:00' and '....123+00:00' do
    -- not sort together — "newest first" silently breaks if these mix.
    published_at        TEXT NOT NULL,
    -- 1 when the feed gave no usable date and fetch time was substituted.
    -- NOT NULL + a flag rather than nullable: nullable makes "newest first"
    -- indeterminate for exactly the feeds most likely to break, and the
    -- flag's rate per feed is itself a health signal.
    published_estimated INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);

-- Article <-> watchlist ticker. A junction, not a column: one article can
-- concern several holdings.
CREATE TABLE IF NOT EXISTS news_article_tickers (
    article_id   INTEGER NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    code         TEXT NOT NULL REFERENCES watchlist_cache(code) ON DELETE CASCADE,
    match_basis  TEXT NOT NULL CHECK (match_basis IN ('feed_query','company_name')),
    PRIMARY KEY (article_id, code)
);

-- Per-feed liveness. PERSISTED, not a module dict: the dead Reuters feed
-- logged a warning every 15 minutes for months and nothing surfaced it. An
-- in-memory dict resets on restart, destroying last_success_at — the one
-- value that separates "failed once, just now" from "dead since May".
CREATE TABLE IF NOT EXISTS news_feed_health (
    feed_key             TEXT PRIMARY KEY,
    url                  TEXT NOT NULL,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_status          TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (last_status IN ('ok','http_error','unparseable','empty','timeout','unknown')),
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    articles_last_run    INTEGER NOT NULL DEFAULT 0
);

-- Upcoming earnings for watchlist tickers, filtered down from Moomoo's
-- WHOLE-MARKET calendar (a 7-day US window is ~300 rows; the watchlist is ~45).
-- A new table rather than columns on watchlist_cache: init_db() runs only
-- CREATE TABLE IF NOT EXISTS, so an ALTER would silently not apply to a
-- database that already exists (see `migrate_schema()`, which covers exactly
-- one additive column and is not a general mechanism) — and a ticker
-- legitimately has several dated rows over time.
CREATE TABLE IF NOT EXISTS earnings_calendar (
    code             TEXT NOT NULL REFERENCES watchlist_cache(code) ON DELETE CASCADE,
    -- Exchange-local calendar date, 'YYYY-MM-DD'. The DATE is the identity,
    -- not a timestamp: a company reports once per date and the announced time
    -- slides within it (pub_type is the coarse when). That makes the refresh
    -- idempotent without needing a dedup key.
    earnings_date    TEXT NOT NULL,
    market           TEXT NOT NULL CHECK (market IN ('US','HK','AU')),
    name             TEXT NOT NULL DEFAULT '',
    -- BEFORE = before the open, AFTER = after the close, REGULAR = during the
    -- session, UNKNOWN = the feed did not say. CHECKed rather than free text
    -- because the alert rules branch on it.
    pub_type         TEXT NOT NULL DEFAULT 'UNKNOWN'
                     CHECK (pub_type IN ('BEFORE','AFTER','REGULAR','UNKNOWN')),
    period_text      TEXT,
    eps_predict      REAL,
    eps_actual       REAL,
    revenue_predict  REAL,
    revenue_actual   REAL,
    -- Options-implied expectation as published. Stored, never recomputed
    -- (rule #1). NULL where the market does not publish it.
    iv               REAL,
    iv_rank          REAL,
    iv_percentile    REAL,
    market_cap       REAL,
    -- The price the CALENDAR carried at fetch time. This is NOT a quote: it
    -- must never be rendered as the current price, and no move may be
    -- computed from it.
    price_at_fetch   REAL,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    PRIMARY KEY (code, earnings_date)
);

-- One AI outlook per reporting event. Deliberately NOT a trade_setups row: an
-- outlook has no validated direction, conviction, stop or target, and
-- get_similar_setups() reads trade_setups as the RAG corpus — a row without
-- those fields would either break that contract or need fake values invented
-- to satisfy it. UNIQUE(code, earnings_date) means a regeneration replaces in
-- place, so the table cannot grow without bound and there is never a stale
-- second copy to pick the wrong one from.
CREATE TABLE IF NOT EXISTS earnings_outlooks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL REFERENCES watchlist_cache(code) ON DELETE CASCADE,
    earnings_date  TEXT NOT NULL,
    generated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
    -- Which model wrote it. Same reasoning as indicator_snapshot.model
    -- (decisions #38): output that silently mixes models is uninterpretable.
    model          TEXT NOT NULL,
    headline       TEXT NOT NULL,
    what_to_watch  TEXT NOT NULL,        -- JSON array[str]
    news_summary   TEXT NOT NULL,
    uncertainty    TEXT NOT NULL,
    -- Audit trail of what was injected, so a bad outlook can be explained
    -- rather than guessed at.
    sources        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (code, earnings_date)
);

-- Alerts the user has seen and chosen to silence. The key is the alert's
-- deterministic fingerprint (rule:code:discriminator), NOT a row id — an
-- alert is the same alert while the fact behind it is unchanged, and it must
-- re-fire when a new thesis, a new earnings date or a deeper drawdown makes
-- it a different fact.
CREATE TABLE IF NOT EXISTS alert_acks (
    fingerprint      TEXT PRIMARY KEY,
    rule             TEXT NOT NULL,
    code             TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('critical','warn','info')),
    acknowledged_at  TEXT NOT NULL,
    -- Acks EXPIRE. A permanent dismiss is how a safety feature becomes
    -- decorative: if the fact is still true tomorrow the user should be told
    -- again, and if that is annoying the answer is to fix the position, not to
    -- silence the tool. Absolute, and stored at write time, so the window is
    -- legible in the row rather than implied by whatever queried it.
    expires_at       TEXT NOT NULL
);

-- ------------------------------------------------------------------
-- Authentication. The dashboard is reachable from the public internet, so
-- the X-API-Key shared secret in app/auth.py is no longer sufficient on its
-- own: it is delivered to whoever loads the page, which is the whole point
-- of a browser and the reason auth.py's own docstring calls it "not
-- authentication". These tables back a real session.
-- ------------------------------------------------------------------

-- One row per logged-in browser. The PRIMARY KEY is a SHA-256 of the cookie
-- value, never the value itself: read access to this file (or to an hourly
-- backup of it) must not hand anybody a working session.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    -- ABSOLUTE, written once at login and never touched again. A sliding
    -- expiry would mean a write on every authenticated request, and
    -- get_connection() runs PRAGMA synchronous = FULL on the explicit premise
    -- that "write volume is trivial, every row follows 60-120s of inference".
    -- A per-request UPDATE breaks that premise the same way the news refresh
    -- did before it was batched into one transaction.
    expires_at   TEXT NOT NULL,
    user_agent   TEXT,
    created_ip   TEXT
);

-- Failed login attempts only, for per-IP lockout. Successes are not recorded
-- here: a successful login DELETEs the IP's rows, so "how many rows in the
-- window" is the whole lockout question and needs no status column.
CREATE TABLE IF NOT EXISTS auth_login_failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ip           TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

-- ------------------------------------------------------------------
-- Web Push
-- ------------------------------------------------------------------

-- One row per installed PWA that has granted notification permission.
-- `endpoint` is UNIQUE because it IS the browser's identity for the
-- subscription — re-subscribing on the same device returns the same endpoint,
-- so an upsert on it is idempotent and a device cannot accumulate duplicates.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint        TEXT NOT NULL UNIQUE,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_success_at TEXT,
    user_agent      TEXT,
    -- A 404/410 from the push service means "gone" and deletes the row
    -- outright. This counts the *other* failures, so a persistently broken
    -- endpoint can be dropped without treating one network blip as death.
    failure_count   INTEGER NOT NULL DEFAULT 0
);

-- Which alert fingerprints have already been pushed. Deliberately keyed on
-- the SAME rule:code:discriminator fingerprint as alert_acks, so the two
-- mechanisms agree by construction: an acked alert is already filtered out
-- upstream, and a drawdown sliding into a deeper severity bucket is a new
-- fingerprint and therefore pushes exactly once at the new level.
CREATE TABLE IF NOT EXISTS alert_pushes (
    fingerprint  TEXT PRIMARY KEY,
    rule         TEXT NOT NULL,
    code         TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('critical','warn','info')),
    pushed_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Sector rotation (decisions #71)
--
-- The taxonomy is NOT ours and is deliberately not curated by hand. Moomoo
-- publishes a plate universe per market; `sector_plates` is a snapshot of the
-- INDUSTRY and CONCEPT enumerations, and membership in THIS table is what
-- makes a plate a sector. Anything Moomoo returns from get_owner_plate whose
-- plate_code is absent here is discarded — which is how broker product lists
-- ('FUTU-CA 美股定投') and novelty baskets ('Nancy Pelosi Portfolio') stay out
-- without anyone maintaining a blocklist that would rot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sector_plates (
    plate_code        TEXT PRIMARY KEY,            -- e.g. "US.LIST2015"
    market            TEXT NOT NULL CHECK (market IN ('US','HK','AU')),
    plate_name        TEXT NOT NULL,               -- e.g. "Software - Infrastructure"
    plate_class       TEXT NOT NULL CHECK (plate_class IN ('INDUSTRY','CONCEPT')),
    plate_id          TEXT,
    -- Display only, and never an input to any score. Derived by splitting
    -- plate_name on " - " and taking the prefix, so it re-derives itself when
    -- Moomoo renames a plate. A name without a separator becomes its own
    -- group: that fails to group, which is much better than grouping wrongly.
    sector_group      TEXT,
    constituent_count INTEGER NOT NULL DEFAULT 0,
    -- When this plate's MEMBER list was last fetched, which is not the same
    -- as last_seen_at and cannot share it: last_seen_at is rewritten for
    -- every plate on every list refresh, so ordering the rotating member
    -- slice by it would re-pick the same 40 plates forever. NULL = never
    -- fetched, which sorts first.
    members_synced_at TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);

-- `code` is deliberately NOT a foreign key to watchlist_cache. A plate's
-- members are overwhelmingly off-watchlist (Semiconductors has 72, of which
-- this account follows a handful), foreign_keys is ON per connection, so an
-- FK would both IntegrityError on insert and cascade-delete real membership
-- the moment a ticker leaves the watchlist.
CREATE TABLE IF NOT EXISTS sector_plate_members (
    plate_code    TEXT NOT NULL REFERENCES sector_plates(plate_code) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    stock_name    TEXT,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (plate_code, code)
);

-- One row per plate per session, written from the KLINE bar.
--
-- NEVER from a mid-session snapshot: a snapshot taken at 11:00 ET carries
-- half a session's turnover, and comparing that against full-session history
-- would manufacture an outflow every single morning. Same instinct as
-- decisions #32 (data_as_of comes from the newest BAR, never the clock).
CREATE TABLE IF NOT EXISTS sector_bars (
    plate_code   TEXT NOT NULL REFERENCES sector_plates(plate_code) ON DELETE CASCADE,
    bar_date     TEXT NOT NULL,                    -- 'YYYY-MM-DD'
    close        REAL,
    change_rate  REAL,
    volume       REAL,
    turnover     REAL,
    -- A plate index that is rebased on reconstitution would show a jump that
    -- is bookkeeping, not price, and would render as a spectacular fake
    -- rotation. Measured 2026-08-30 over 145 Semiconductor bars: max move
    -- 8.2%, no bar over 15%. The guard is cheap and stays.
    suspect_bar  INTEGER NOT NULL DEFAULT 0,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (plate_code, bar_date)
);

-- Breadth is the one thing klines do not carry, so it comes from the plate
-- snapshot and is therefore only ever "as of now" — hence partial_session,
-- which is 1 unless the capture happened after the session closed.
CREATE TABLE IF NOT EXISTS sector_breadth (
    plate_code       TEXT NOT NULL REFERENCES sector_plates(plate_code) ON DELETE CASCADE,
    as_of_date       TEXT NOT NULL,
    raise_count      INTEGER,
    fall_count       INTEGER,
    equal_count      INTEGER,
    last_price       REAL,
    turnover         REAL,
    partial_session  INTEGER NOT NULL DEFAULT 1,
    captured_at      TEXT NOT NULL,
    PRIMARY KEY (plate_code, as_of_date)
);

-- The deterministic rotation score. `components` is the JSON breakdown the
-- score was summed from — decisions #66's rule that a ranking whose ranking
-- cannot be inspected is a black box, and the weights here are PRIORS with
-- nothing fitted to outcomes.
CREATE TABLE IF NOT EXISTS sector_rotation_scores (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_code       TEXT NOT NULL REFERENCES sector_plates(plate_code) ON DELETE CASCADE,
    as_of_date       TEXT NOT NULL,
    window_days      INTEGER NOT NULL,
    score            REAL,
    components       TEXT NOT NULL,                -- JSON {name: signed value}
    rel_return_pct   REAL,
    turnover_thrust  REAL,
    breadth          REAL,
    persistence      REAL,
    news_thrust      REAL,
    sessions_used    INTEGER NOT NULL,
    constituents     INTEGER,
    coverage         REAL NOT NULL,
    thin_session     INTEGER NOT NULL DEFAULT 0,
    sufficient       INTEGER NOT NULL DEFAULT 0,
    computed_at      TEXT NOT NULL,
    UNIQUE (plate_code, as_of_date, window_days)
);

-- Sector ETFs are the ONLY place signed institutional-vs-retail flow exists:
-- get_capital_flow refuses plate codes ("Only stocks, warrants, and funds are
-- supported") but accepts funds. main_in_flow == super + big, i.e. net
-- block-sized order flow. trust_aum / trust_outstanding_units ride along from
-- the same snapshot at zero extra call cost, because the DIFFERENCE in
-- outstanding units across sessions is the textbook definition of a net
-- creation/redemption — which has no history and can only accumulate forward.
CREATE TABLE IF NOT EXISTS sector_etf_flows (
    etf_code                 TEXT NOT NULL,
    flow_date                TEXT NOT NULL,
    in_flow                  REAL,
    main_in_flow             REAL,
    super_in_flow            REAL,
    big_in_flow              REAL,
    mid_in_flow              REAL,
    sml_in_flow              REAL,
    trust_aum                REAL,
    trust_outstanding_units  REAL,
    last_price               REAL,
    ingested_at              TEXT NOT NULL,
    PRIMARY KEY (etf_code, flow_date)
);

-- The qualitative layer over the rotation score (decisions #72).
--
-- Deliberately carries NO NUMERIC FIELD OF ANY KIND. The score in
-- sector_rotation_scores is computed in Python from price and volume; this
-- table holds what a model made of the news around it, and the two must never
-- look comparable. A `confidence_label` of three fixed words rather than a
-- 1-10 rating is the load-bearing part of that: a number would be averaged,
-- plotted, and eventually set beside a conviction_score (decisions #52).
--
-- `supporting_headlines` is a JSON array of article titles the model cited,
-- and the validator only accepts titles that were actually in its prompt — so
-- a citation here cannot refer to an article that does not exist.
CREATE TABLE IF NOT EXISTS sector_narratives (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_code           TEXT NOT NULL REFERENCES sector_plates(plate_code) ON DELETE CASCADE,
    as_of_date           TEXT NOT NULL,
    window_days          INTEGER NOT NULL,
    headline             TEXT NOT NULL,
    candidate_driver     TEXT NOT NULL,
    supporting_headlines TEXT NOT NULL,          -- JSON array of verbatim titles
    contradicts          TEXT NOT NULL,
    confidence_label     TEXT NOT NULL CHECK (confidence_label IN (
                             'news explains it',
                             'news is consistent',
                             'no news explains it')),
    model                TEXT NOT NULL,          -- provenance: a corpus that
                                                 -- silently mixes models is
                                                 -- uninterpretable (#38)
    sources              TEXT NOT NULL DEFAULT '{}',
    generated_at         TEXT NOT NULL,
    UNIQUE (plate_code, as_of_date, window_days)
);

CREATE INDEX IF NOT EXISTS idx_trade_setups_code        ON trade_setups(code);
CREATE INDEX IF NOT EXISTS idx_trade_setups_created_at   ON trade_setups(created_at);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_setup_id   ON trade_outcomes(setup_id);
CREATE INDEX IF NOT EXISTS idx_setup_scores_setup_id     ON setup_scores(setup_id);
CREATE INDEX IF NOT EXISTS idx_setup_scores_horizon      ON setup_scores(horizon_days);
CREATE INDEX IF NOT EXISTS idx_watchlist_market          ON watchlist_cache(market);
CREATE INDEX IF NOT EXISTS idx_wgm_code                  ON watchlist_group_members(code);
CREATE INDEX IF NOT EXISTS idx_news_published_at         ON news_articles(published_at);
CREATE INDEX IF NOT EXISTS idx_news_category             ON news_articles(category);
CREATE INDEX IF NOT EXISTS idx_news_feed_key             ON news_articles(feed_key);
CREATE INDEX IF NOT EXISTS idx_news_title_norm           ON news_articles(title_norm);
CREATE INDEX IF NOT EXISTS idx_news_tickers_code         ON news_article_tickers(code);
CREATE INDEX IF NOT EXISTS idx_earnings_date             ON earnings_calendar(earnings_date);
CREATE INDEX IF NOT EXISTS idx_earnings_outlook_date     ON earnings_outlooks(earnings_date);
CREATE INDEX IF NOT EXISTS idx_alert_acks_expires        ON alert_acks(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires    ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_login_failures_ip   ON auth_login_failures(ip, attempted_at);
CREATE INDEX IF NOT EXISTS idx_alert_pushes_pushed_at   ON alert_pushes(pushed_at);
CREATE INDEX IF NOT EXISTS idx_sector_scores_asof     ON sector_rotation_scores(as_of_date, window_days);
CREATE INDEX IF NOT EXISTS idx_sector_scores_plate    ON sector_rotation_scores(plate_code);
CREATE INDEX IF NOT EXISTS idx_sector_members_code    ON sector_plate_members(code);
CREATE INDEX IF NOT EXISTS idx_sector_bars_date       ON sector_bars(bar_date);
CREATE INDEX IF NOT EXISTS idx_sector_plates_market   ON sector_plates(market, plate_class);
CREATE INDEX IF NOT EXISTS idx_sector_narratives_asof ON sector_narratives(as_of_date, window_days);
"""


def init_db() -> None:
    """Create the schema if it doesn't exist yet. Safe to call on every startup.

    Deliberately side-effect-free beyond schema creation — the tests call it,
    so anything that mutates data belongs in the app's lifespan instead (see
    `reconcile_interrupted_runs`).
    """
    with get_connection() as conn:
        conn.executescript(_SCHEMA)


# Columns added to a table that already exists in the wild. `init_db()` runs
# only CREATE TABLE IF NOT EXISTS, so a column added to `_SCHEMA` reaches a
# fresh database and NEVER reaches an existing one — the live box would keep
# its old table shape and every INSERT naming the new column would fail.
#
# Each entry is (table, column, type). Type carries no NOT NULL and no
# DEFAULT, deliberately: SQLite adds a nullable column with no default by
# rewriting nothing at all, and NULL is the honest value for every row that
# predates the feature. A row written before `suggested_entry` existed did
# not have an entry suggestion — it did not have an empty one.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("trade_setups", "suggested_entry", "REAL"),
    ("watchlist_cache", "security_type", "TEXT"),
    # sector_plates is a new table, so a fresh database gets this from
    # _SCHEMA. The entry is here anyway because a database created by an
    # intermediate build already has the table WITHOUT the column, and that
    # is exactly the case migrate_schema exists for.
    ("sector_plates", "members_synced_at", "TEXT"),
)


def migrate_schema() -> list[str]:
    """Add columns that `_SCHEMA` gained after this database was created.

    This is the project's only migration step and is deliberately the
    smallest one that works: additive, nullable columns, discovered by
    comparing `PRAGMA table_info` against `_ADDED_COLUMNS`. It cannot drop,
    rename, retype or backfill anything, and it should not learn how — a
    change needing any of those wants a new table instead (which is why the
    schema comments above still say so).

    Idempotent, so it runs on every boot. Called from `main.py`'s lifespan
    rather than from `init_db()`, which the tests call and which is
    documented to stay free of anything beyond schema creation.

    Returns:
        The `table.column` names actually added, for the caller to log. An
        empty list is the normal steady state.
    """
    added: list[str] = []
    with get_connection() as conn:
        for table, column, decl in _ADDED_COLUMNS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not cols:
                # The table does not exist at all, so init_db() will create
                # it complete. Nothing to migrate, and ALTER would raise.
                continue
            if column in cols:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


# --------------------------------------------------------------------------
# Small persisted state
# --------------------------------------------------------------------------

def get_app_state(key: str, default: str | None = None) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def delete_app_state(key: str) -> None:
    """Forget a persisted choice, so its code-level default applies again."""
    with get_connection() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))


def set_app_state(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )


def reconcile_interrupted_runs() -> int:
    """Close out scan runs that a restart killed mid-flight.

    `shutdown_scheduler()` shuts down with `wait=False`, so a SIGTERM during
    a 90s inference abandons the cycle and leaves its `scanner_runs` row at
    status 'running' forever — the scan history then shows a run that never
    ends and never will.

    Marked 'failed' rather than a truer 'interrupted', because the schema's
    CHECK constrains status to three values and there is no migration path
    to add a fourth; the reason lives in `error_summary` instead.

    Correct only while exactly one process writes this database. If uvicorn
    ever ran with --workers > 1, the second worker's boot would mark the
    first worker's genuinely-live run as failed.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE scanner_runs
               SET status = 'failed',
                   finished_at = ?,
                   error_summary = COALESCE(error_summary || '; ', '')
                                   || 'interrupted by restart'
             WHERE status = 'running'
            """,
            (now_iso(),),
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Watchlist / groups helpers
# --------------------------------------------------------------------------

def upsert_watchlist_ticker(
    code: str,
    name: str,
    market: Market,
    enabled: bool = True,
    security_type: str | None = None,
) -> None:
    """Insert or update a ticker's master record. Called during watchlist sync.

    `security_type` is Moomoo's 'STOCK' / 'ETF' label. It is COALESCEd rather
    than overwritten so a sync that cannot determine it does not erase a value
    an earlier sync established — the same instinct as `enabled` never being
    clobbered here.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO watchlist_cache
                (code, name, market, enabled, security_type, last_synced_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                market = excluded.market,
                security_type = COALESCE(excluded.security_type, watchlist_cache.security_type),
                last_synced_at = excluded.last_synced_at,
                updated_at = excluded.updated_at
            """,
            (code, name, market, int(enabled), security_type, now_iso(), now_iso()),
        )


def set_ticker_enabled(code: str, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE watchlist_cache SET enabled = ?, updated_at = ? WHERE code = ?",
            (int(enabled), now_iso(), code),
        )


def get_enabled_tickers(market: Market | None = None) -> list[dict[str, Any]]:
    """Tickers the scanner should process this cycle."""
    with get_connection() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM watchlist_cache WHERE enabled = 1 AND market = ? ORDER BY code",
                (market,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watchlist_cache WHERE enabled = 1 ORDER BY code"
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_group(group_id: str, group_name: str, is_system: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO groups_cache (group_id, group_name, is_system, last_synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name = excluded.group_name,
                is_system = excluded.is_system,
                last_synced_at = excluded.last_synced_at
            """,
            (group_id, group_name, int(is_system), now_iso()),
        )


def get_all_groups() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM groups_cache ORDER BY is_system DESC, group_name"
        ).fetchall()
        return [dict(r) for r in rows]


def get_group_members(group_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT w.* FROM watchlist_cache w
            JOIN watchlist_group_members m ON m.code = w.code
            WHERE m.group_id = ?
            ORDER BY w.code
            """,
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_group_members(group_id: str, codes: list[str]) -> None:
    """
    Replace the full membership list for a group in one transaction.
    Used after a sync pass to reconcile local cache with Moomoo's current state.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM watchlist_group_members WHERE group_id = ?", (group_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_group_members (group_id, code) VALUES (?, ?)",
            [(group_id, code) for code in codes],
        )


def _assert_group_writable(conn: sqlite3.Connection, group_id: str) -> None:
    """Enforce rule #4: Moomoo system groups are read-only for writes.

    Args:
        conn:     an open connection; the check must share the caller's
                  transaction, or the group could change between the check and
                  the write it guards.
        group_id: the group about to be modified.

    Raises:
        ValueError: the group does not exist, or it is a Moomoo system group.

    Both messages are surfaced verbatim by the API, so their wording is part
    of the contract rather than an implementation detail.
    """
    row = conn.execute(
        "SELECT is_system FROM groups_cache WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown group_id: {group_id}")
    if row["is_system"]:
        raise ValueError(f"Group '{group_id}' is a Moomoo system group and is read-only.")


def add_ticker_to_group(group_id: str, code: str) -> None:
    """Add one ticker to a custom group.

    Raises ValueError if the target group is a Moomoo system group
    (read-only for writes) or does not exist.
    """
    with get_connection() as conn:
        _assert_group_writable(conn, group_id)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_group_members (group_id, code) VALUES (?, ?)",
            (group_id, code),
        )


def remove_ticker_from_group(group_id: str, code: str) -> None:
    """Remove one ticker from a custom group.

    Raises ValueError if the target group is a Moomoo system group
    (read-only for writes) or does not exist.
    """
    with get_connection() as conn:
        _assert_group_writable(conn, group_id)
        conn.execute(
            "DELETE FROM watchlist_group_members WHERE group_id = ? AND code = ?",
            (group_id, code),
        )


# --------------------------------------------------------------------------
# Scanner run helpers
# --------------------------------------------------------------------------

def insert_scanner_run() -> int:
    """Call at the start of a scan cycle. Returns the new run's id."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO scanner_runs (started_at, status) VALUES (?, 'running')",
            (now_iso(),),
        )
        return int(cur.lastrowid)


def finish_scanner_run(
    run_id: int,
    tickers_scanned: int,
    tickers_succeeded: int,
    tickers_failed: int,
    status: ScannerStatus = "completed",
    error_summary: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE scanner_runs
            SET finished_at = ?, tickers_scanned = ?, tickers_succeeded = ?,
                tickers_failed = ?, status = ?, error_summary = ?
            WHERE id = ?
            """,
            (now_iso(), tickers_scanned, tickers_succeeded, tickers_failed,
             status, error_summary, run_id),
        )


# --------------------------------------------------------------------------
# Trade setups
# --------------------------------------------------------------------------

def insert_trade_setup(
    *,
    scanner_run_id: int | None,
    code: str,
    market: Market,
    data_as_of: str,
    is_delayed_data: bool,
    indicator_snapshot: dict[str, Any],
    feature_vector: list[float],
    trade_direction: TradeDirection,
    conviction_score: int,
    reasoning: str,
    suggested_entry: float | None,
    suggested_stop: float | None,
    suggested_target: float | None,
    key_levels_notes: str | None,
    similar_setup_ids: list[int],
) -> int:
    """Persist one AI thesis result. Returns the new setup's id."""
    if not (1 <= conviction_score <= 10):
        raise ValueError("conviction_score must be between 1 and 10")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_setups (
                scanner_run_id, code, market, data_as_of, is_delayed_data,
                indicator_snapshot, feature_vector, trade_direction,
                conviction_score, reasoning, suggested_entry, suggested_stop,
                suggested_target, key_levels_notes, similar_setup_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scanner_run_id, code, market, data_as_of, int(is_delayed_data),
                json.dumps(indicator_snapshot), json.dumps(feature_vector),
                trade_direction, conviction_score, reasoning,
                suggested_entry, suggested_stop, suggested_target, key_levels_notes,
                json.dumps(similar_setup_ids),
            ),
        )
        return int(cur.lastrowid)


def get_latest_setup_for_code(code: str) -> dict[str, Any] | None:
    """The newest stored thesis for one ticker, or None.

    The `id DESC` tie-break matches `routers.setups._SORTS` rather than
    fixing an observed bug: `now_iso()` has second granularity, so a
    same-second pair would otherwise resolve arbitrarily. Be honest about
    the risk — a probe of all 1,882 stored rows found zero same-second
    collisions for any code, because a thesis takes 25-120s to generate. So
    this is cheap consistency with the list endpoint, not a live fault, and
    it matters because `scanner._scan_order`, `/setups/latest/{code}` and
    `alerts.build_alerts` all read the corpus through this one function.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trade_setups WHERE code = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (code,),
        ).fetchone()
        return dict(row) if row else None


def get_setup_history(codes: list[str], per_code: int = 10) -> dict[str, list[dict[str, Any]]]:
    """The newest `per_code` theses for each of `codes`, newest first.

    One query for the whole watchlist rather than N. The rotation writes a
    thesis per ticker roughly hourly, so "the last ten" is a few hours of
    the model's opinion about one name — which is the input the signals
    ranking treats as evidence (does the read persist, is conviction
    rising or falling), rather than trusting whichever single thesis
    happens to be newest.

    Same ROW_NUMBER partition the /setups list endpoint uses, with the
    cut at `per_code` instead of 1.
    """
    if not codes:
        return {}
    marks = ",".join("?" for _ in codes)
    query = (
        "SELECT * FROM ("
        "SELECT *, ROW_NUMBER() OVER (PARTITION BY code"
        " ORDER BY created_at DESC, id DESC) AS _rn"
        f" FROM trade_setups WHERE code IN ({marks})"
        ") WHERE _rn <= ? ORDER BY code, created_at DESC, id DESC"
    )
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
    with get_connection() as conn:
        for row in conn.execute(query, [*codes, per_code]).fetchall():
            item = dict(row)
            item.pop("_rn", None)
            out.setdefault(item["code"], []).append(item)
    return out


# --------------------------------------------------------------------------
# Similarity search (RAG retrieval)
# --------------------------------------------------------------------------

class SimilarSetup(TypedDict):
    setup_id: int
    code: str
    created_at: str
    trade_direction: str
    conviction_score: int
    similarity: float
    outcome: dict[str, Any] | None  # P&L / hold time / exit reason, if realized


def _vector_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cosine(a: list[float], b: list[float], norm_a: float) -> float:
    """Cosine of `a` and `b`, given `a`'s precomputed norm.

    Split out from `cosine_similarity` for one reason: `get_similar_setups`
    scores ONE query vector against every candidate row, and the public
    signature recomputes the query vector's norm on every one of them. The
    arithmetic below is unchanged — same expression, same order — so this is
    the single implementation and `cosine_similarity` is a wrapper over it.
    """
    if len(a) != len(b) or not a:
        return 0.0
    norm_b = _vector_norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (norm_a * norm_b)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return _cosine(a, b, _vector_norm(a))


# Below this cosine, a "precedent" is not evidence about this setup.
#
# The feature vector is mostly sign/tanh components (see similarity.py), so
# unrelated setups still score high: measured against the corpus, an IBM and a
# PLTR setup with nothing in common scored 0.891, and a NOW setup 0.954. With
# a single outcome on record, `top_k` slicing returned that one row for every
# ticker and the prompt presented it as a 0.95 match — while ai_thesis
# explicitly instructs the model to pull conviction down when lookalikes
# resolved badly. Conviction drifted 6->5->5->5->4 on unrelated names.
#
# So the floor is deliberately high, and set from the observed noise floor
# rather than from taste. Rejected candidates are logged with their scores so
# it can be tuned against real data instead of guessed at again.
MIN_RAG_SIMILARITY = 0.97


def get_similar_setups(
    feature_vector: list[float],
    top_k: int = 3,
    exclude_setup_id: int | None = None,
    only_with_outcomes: bool = True,
    min_similarity: float = MIN_RAG_SIMILARITY,
) -> list[SimilarSetup]:
    """
    Retrieve the top-k most similar historical setups by cosine similarity
    over `feature_vector`, for injection into the AI prompt as RAG context.

    By default only considers setups that have a realized outcome attached
    (there'd be nothing useful to inject otherwise). Full-table scan done
    in Python — see module docstring for the scale rationale.

    Candidates below `min_similarity` are dropped rather than returned as the
    best of a bad set. Returning nothing is the honest answer when nothing
    comparable exists, and `ai_thesis._similar_block` already handles the
    empty case well ("no track record to lean on, keep conviction modest").
    An unrelated precedent presented as a 0.95 match is worse than no
    precedent at all.
    """
    # Six columns, not `SELECT *`. Only these are read below, and a setup row
    # averages ~2KB (mostly `indicator_snapshot` and `reasoning`) against ~200
    # bytes of feature_vector — so the wide projection read an order of
    # magnitude more than the scan uses. `id` is in the list, so DISTINCT
    # dedupes on exactly the key it did before.
    with get_connection() as conn:
        if only_with_outcomes:
            rows = conn.execute(
                """
                SELECT DISTINCT s.id, s.code, s.created_at, s.trade_direction,
                       s.conviction_score, s.feature_vector
                FROM trade_setups s
                JOIN trade_outcomes o ON o.setup_id = s.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, code, created_at, trade_direction,
                          conviction_score, feature_vector
                   FROM trade_setups"""
            ).fetchall()

        # The query vector's norm is fixed for the whole scan; computing it
        # per candidate is the one avoidable cost in an O(N) loop.
        norm_q = _vector_norm(feature_vector)

        # Filtered as we go rather than sorted-then-filtered: only the kept
        # set needs ordering. `best_rejected` is carried because the log line
        # below reports the nearest miss, which was previously read off the
        # fully-sorted list.
        kept: list[tuple[float, sqlite3.Row]] = []
        considered = 0
        best_rejected: tuple[float, sqlite3.Row] | None = None
        for row in rows:
            if exclude_setup_id is not None and row["id"] == exclude_setup_id:
                continue
            try:
                candidate_vec = json.loads(row["feature_vector"])
            except (TypeError, json.JSONDecodeError):
                continue
            sim = _cosine(feature_vector, candidate_vec, norm_q)
            considered += 1
            if sim >= min_similarity:
                kept.append((sim, row))
            elif best_rejected is None or sim > best_rejected[0]:
                best_rejected = (sim, row)

        kept.sort(key=lambda pair: pair[0], reverse=True)

        if considered and not kept and best_rejected is not None:
            # Log what was rejected and by how much: this is the signal for
            # whether MIN_RAG_SIMILARITY is set sensibly against real data.
            best_sim, best_row = best_rejected
            logger.info(
                "RAG: no precedent clears %.2f for this setup; best was %s at "
                "%.3f (%d candidate(s) considered) — proceeding with no "
                "historical context",
                min_similarity, best_row["code"], best_sim, considered,
            )
        top = kept[:top_k]

        results: list[SimilarSetup] = []
        for sim, row in top:
            outcome_row = conn.execute(
                "SELECT * FROM trade_outcomes WHERE setup_id = ? ORDER BY created_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            results.append(
                SimilarSetup(
                    setup_id=row["id"],
                    code=row["code"],
                    created_at=row["created_at"],
                    trade_direction=row["trade_direction"],
                    conviction_score=row["conviction_score"],
                    similarity=round(sim, 4),
                    outcome=dict(outcome_row) if outcome_row else None,
                )
            )
        return results


# --------------------------------------------------------------------------
# Trade outcomes
# --------------------------------------------------------------------------

def log_outcome(
    *,
    setup_id: int,
    source: OutcomeSource,
    entry_price: float | None = None,
    exit_price: float | None = None,
    pnl_abs: float | None = None,
    pnl_pct: float | None = None,
    hold_time_hours: float | None = None,
    exit_reason: str | None = None,
    opened_at: str | None = None,
    closed_at: str | None = None,
    notes: str | None = None,
    moomoo_deal_id: str | None = None,
) -> int:
    """Manual or programmatic outcome logging. Returns the new outcome's id."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_outcomes (
                setup_id, source, moomoo_deal_id, entry_price, exit_price,
                pnl_abs, pnl_pct, hold_time_hours, exit_reason,
                opened_at, closed_at, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                setup_id, source, moomoo_deal_id, entry_price, exit_price,
                pnl_abs, pnl_pct, hold_time_hours, exit_reason,
                opened_at, closed_at, notes, now_iso(),
            ),
        )
        return int(cur.lastrowid)


# How long before a trade a setup may have been written and still plausibly
# be the reason for it. Without a bound, the "most recent setup before this
# deal" is happily a setup from six months earlier — which then inherits a
# P&L it had nothing to do with, and that outcome becomes RAG context
# steering future advice. A thesis that old is not why someone traded today.
MAX_SETUP_TO_DEAL_DAYS = 14


def find_candidate_setup_for_deal(
    code: str,
    opened_at: str,
    max_age_days: int = MAX_SETUP_TO_DEAL_DAYS,
) -> int | None:
    """
    Best-effort match: the most recent trade_setup for this code created
    within `max_age_days` before the deal's open time, that doesn't already
    have an outcome. This is a heuristic, not a guarantee — manual override
    exists for setups this misses or mismatches.
    """
    opened = parse_iso(opened_at)
    earliest = (
        (opened - timedelta(days=max_age_days)).isoformat()
        if opened is not None else ""
    )
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT s.id
            FROM trade_setups s
            LEFT JOIN trade_outcomes o ON o.setup_id = s.id
            WHERE s.code = ? AND s.created_at <= ? AND s.created_at >= ?
                  AND o.id IS NULL
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            (code, opened_at, earliest),
        ).fetchone()
        return int(row["id"]) if row else None


def sync_moomoo_outcomes(deals: list[dict[str, Any]]) -> dict[str, int]:
    """
    Upsert a batch of closed deals pulled from Moomoo's historical
    deals/positions API. Each `deal` dict is expected to carry at least:
    code, moomoo_deal_id, entry_price, exit_price, opened_at, closed_at.

    Dedup is enforced by the UNIQUE constraint on moomoo_deal_id — safe
    to call repeatedly with overlapping data. Deals that can't be matched
    to an existing setup are skipped and counted, for manual follow-up.

    Returns a summary: {"synced": n, "skipped_unmatched": n, "updated": n}
    """
    synced = 0
    updated = 0
    skipped = 0

    with get_connection() as conn:
        for deal in deals:
            deal_id = deal.get("moomoo_deal_id")
            if not deal_id:
                skipped += 1
                continue

            existing = conn.execute(
                "SELECT id FROM trade_outcomes WHERE moomoo_deal_id = ?", (deal_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE trade_outcomes SET
                        entry_price = ?, exit_price = ?, pnl_abs = ?, pnl_pct = ?,
                        hold_time_hours = ?, exit_reason = ?, opened_at = ?, closed_at = ?
                    WHERE moomoo_deal_id = ?
                    """,
                    (
                        deal.get("entry_price"), deal.get("exit_price"),
                        deal.get("pnl_abs"), deal.get("pnl_pct"),
                        deal.get("hold_time_hours"), deal.get("exit_reason"),
                        deal.get("opened_at"), deal.get("closed_at"),
                        deal_id,
                    ),
                )
                updated += 1
                continue

            setup_id = find_candidate_setup_for_deal(
                code=deal["code"], opened_at=deal.get("opened_at", now_iso())
            )
            if setup_id is None:
                skipped += 1
                continue

            conn.execute(
                """
                INSERT INTO trade_outcomes (
                    setup_id, source, moomoo_deal_id, entry_price, exit_price,
                    pnl_abs, pnl_pct, hold_time_hours, exit_reason,
                    opened_at, closed_at, notes, created_at
                ) VALUES (?, 'moomoo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    setup_id, deal_id, deal.get("entry_price"), deal.get("exit_price"),
                    deal.get("pnl_abs"), deal.get("pnl_pct"), deal.get("hold_time_hours"),
                    deal.get("exit_reason"), deal.get("opened_at"), deal.get("closed_at"),
                    deal.get("notes"), now_iso(),
                ),
            )
            synced += 1

    return {"synced": synced, "updated": updated, "skipped_unmatched": skipped}


# --------------------------------------------------------------------------
# Stats (for the Trade Memory view)
# --------------------------------------------------------------------------

def get_win_rate_stats(code: str | None = None) -> dict[str, Any]:
    """Basic win-rate / P&L summary, optionally scoped to one ticker."""
    with get_connection() as conn:
        query = """
            SELECT o.pnl_abs, o.pnl_pct
            FROM trade_outcomes o
            JOIN trade_setups s ON s.id = o.setup_id
            WHERE o.pnl_abs IS NOT NULL
        """
        params: tuple[Any, ...] = ()
        if code:
            query += " AND s.code = ?"
            params = (code,)

        rows = conn.execute(query, params).fetchall()
        total = len(rows)
        wins = sum(1 for r in rows if r["pnl_abs"] and r["pnl_abs"] > 0)
        avg_pnl_pct = (
            sum(r["pnl_pct"] for r in rows if r["pnl_pct"] is not None) / total
            if total else None
        )
        return {
            "total_closed_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total, 4) if total else None,
            "avg_pnl_pct": round(avg_pnl_pct, 4) if avg_pnl_pct is not None else None,
        }


# --------------------------------------------------------------------------
# Manual bootstrap (not called by the app; useful for a one-off shell check)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

def insert_news_articles(
    articles: list[dict[str, Any]],
    retention_days: int | None = None,
) -> dict[str, int]:
    """Upsert a refresh's worth of articles, their ticker links, and prune.

    **One connection, one transaction, one fsync.** `get_connection()` runs
    `PRAGMA synchronous = FULL`, justified by "write volume is trivial —
    every row is preceded by 60-120s of inference". A refresh writing
    hundreds of rows breaks that assumption outright, and a connection per
    article would mean hundreds of fsyncs.

    Each article dict carries its `codes`: a list of (code, match_basis).
    """
    now = now_iso()
    inserted = updated = linked = 0

    with get_connection() as conn:
        # Which keys already exist, asked once for the whole batch rather than
        # inferred from the upsert. The obvious trick — comparing the returned
        # first_seen_at against "now" — is wrong: now_iso() has SECOND
        # granularity, so two refreshes inside the same second both look new.
        keys = [a["dedup_key"] for a in articles]
        existing: set[str] = set()
        for i in range(0, len(keys), 400):   # stay well inside SQLite's param limit
            chunk = keys[i:i + 400]
            marks = ",".join("?" * len(chunk))
            existing.update(
                r["dedup_key"] for r in conn.execute(
                    f"SELECT dedup_key FROM news_articles WHERE dedup_key IN ({marks})",
                    chunk,
                )
            )

        for art in articles:
            is_new = art["dedup_key"] not in existing
            row = conn.execute(
                """
                INSERT INTO news_articles
                    (dedup_key, url, title, title_norm, summary, feed_key,
                     source_label, category, published_at, published_estimated,
                     first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedup_key) DO UPDATE SET last_seen_at = excluded.last_seen_at
                RETURNING id
                """,
                (art["dedup_key"], art.get("url"), art["title"], art["title_norm"],
                 art.get("summary"), art["feed_key"], art["source_label"],
                 art["category"], art["published_at"],
                 int(art.get("published_estimated", 0)), now, now),
            ).fetchone()
            article_id = int(row["id"])
            if is_new:
                inserted += 1
                existing.add(art["dedup_key"])   # a batch can repeat a key
            else:
                updated += 1

            for code, basis in art.get("codes", []):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO news_article_tickers "
                    "(article_id, code, match_basis) VALUES (?,?,?)",
                    (article_id, code, basis),
                )
                linked += cur.rowcount

        pruned = 0
        if retention_days:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=retention_days)).isoformat(timespec="seconds")
            pruned = conn.execute(
                "DELETE FROM news_articles WHERE published_at < ?", (cutoff,)
            ).rowcount

    return {"inserted": inserted, "updated": updated,
            "linked": linked, "pruned": pruned}


def list_news(
    category: str | None = None,
    code: str | None = None,
    watchlist_only: bool = False,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Articles, newest first, with their ticker links attached."""
    query = """
        SELECT a.* FROM news_articles a
    """
    clauses: list[str] = []
    params: list[Any] = []

    if code:
        query += " JOIN news_article_tickers t ON t.article_id = a.id "
        clauses.append("t.code = ?")
        params.append(code)
    elif watchlist_only:
        query += """ JOIN news_article_tickers t ON t.article_id = a.id
                     JOIN watchlist_cache w ON w.code = t.code AND w.enabled = 1 """
    if category and category not in ("all", "watchlist"):
        clauses.append("a.category = ?")
        params.append(category)
    if since:
        clauses.append("a.published_at >= ?")
        params.append(since)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # Same second-granularity tie-break reasoning as /setups: id DESC keeps
    # paging deterministic when several items share a timestamp.
    query += " GROUP BY a.id ORDER BY a.published_at DESC, a.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        if not rows:
            return []
        ids = tuple(r["id"] for r in rows)
        marks = ",".join("?" * len(ids))
        links: dict[int, list[dict[str, Any]]] = {}
        for lr in conn.execute(
            f"""SELECT t.article_id, t.code, t.match_basis, w.name
                FROM news_article_tickers t
                LEFT JOIN watchlist_cache w ON w.code = t.code
                WHERE t.article_id IN ({marks})""", ids
        ):
            links.setdefault(lr["article_id"], []).append(
                {"code": lr["code"], "name": lr["name"], "match_basis": lr["match_basis"]}
            )
    for r in rows:
        r["codes"] = links.get(r["id"], [])
        r["published_estimated"] = bool(r["published_estimated"])
    return rows


def count_news_by_category() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, count(*) AS n FROM news_articles GROUP BY category"
        ).fetchall()
    counts = {r["category"]: int(r["n"]) for r in rows}
    counts["all"] = sum(counts.values())
    return counts


def get_news_for_codes(
    codes: list[str], since: str, limit: int = 5,
    bases: tuple[str, ...] = ("feed_query", "company_name"),
) -> list[dict[str, Any]]:
    """Ticker-linked articles for the thesis prompt."""
    if not codes:
        return []
    cm = ",".join("?" * len(codes))
    bm = ",".join("?" * len(bases))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT a.*, t.match_basis FROM news_articles a
                JOIN news_article_tickers t ON t.article_id = a.id
                WHERE t.code IN ({cm}) AND t.match_basis IN ({bm})
                      AND a.published_at >= ?
                ORDER BY a.published_at DESC, a.id DESC LIMIT ?""",
            (*codes, *bases, since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def codes_with_news(
    codes: list[str], since: str,
    bases: tuple[str, ...] = ("feed_query", "company_name"),
) -> set[str]:
    """Which of `codes` have ANY ticker-linked article since `since`.

    The batched form of asking `get_news_for_codes([code], since, limit=1)`
    per code, which is how `sector_narrative` used to compute the fraction of
    a sector its headlines actually speak for.

    Deliberately NOT derivable from a `get_news_for_codes(codes, ...)` result:
    that call is capped by `limit` and its rows are then deduplicated by
    title, so a code whose only article lost the cut would drop out of the set
    and understate the coverage the prompt reports (decisions #72d).
    """
    if not codes:
        return set()
    cm = ",".join("?" * len(codes))
    bm = ",".join("?" * len(bases))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT t.code FROM news_article_tickers t
                JOIN news_articles a ON a.id = t.article_id
                WHERE t.code IN ({cm}) AND t.match_basis IN ({bm})
                      AND a.published_at >= ?""",
            (*codes, *bases, since),
        ).fetchall()
    return {r["code"] for r in rows}


def get_macro_news(since: str, limit: int = 3) -> list[dict[str, Any]]:
    """Market-wide context for the prompt: shocks and macro only.

    `themes` is excluded deliberately — Seeking Alpha and Investing.com
    commentary is useful reading and misleading evidence, and the prompt
    already instructs the model to weigh what it is given.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM news_articles
               WHERE category IN ('shocks','macro') AND published_at >= ?
               ORDER BY published_at DESC, id DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Earnings calendar and outlooks
# --------------------------------------------------------------------------

def upsert_earnings(rows: list[dict[str, Any]], retention_days: int = 30) -> dict[str, int]:
    """Write a batch of calendar rows, then prune old ones. One transaction.

    Batched for the same reason `insert_news_articles` is: `get_connection()`
    sets `PRAGMA synchronous = FULL`, which is justified by "write volume is
    trivial, every row follows 60-120s of inference" — a refresh writing
    dozens of rows one connection at a time breaks that justification.

    Retention keeps 30 days of PAST events, not just future ones: the
    `earnings_passed_unreviewed` alert needs to know a report has already
    happened and that the stored thesis predates it.
    """
    now = now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date().isoformat()
    inserted = updated = 0

    with get_connection() as conn:
        existing = {
            (r["code"], r["earnings_date"])
            for r in conn.execute("SELECT code, earnings_date FROM earnings_calendar")
        }
        for row in rows:
            key = (row["code"], row["earnings_date"])
            conn.execute(
                """
                INSERT INTO earnings_calendar (
                    code, earnings_date, market, name, pub_type, period_text,
                    eps_predict, eps_actual, revenue_predict, revenue_actual,
                    iv, iv_rank, iv_percentile, market_cap, price_at_fetch,
                    first_seen_at, last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code, earnings_date) DO UPDATE SET
                    market = excluded.market,
                    name = excluded.name,
                    pub_type = excluded.pub_type,
                    period_text = excluded.period_text,
                    eps_predict = excluded.eps_predict,
                    eps_actual = excluded.eps_actual,
                    revenue_predict = excluded.revenue_predict,
                    revenue_actual = excluded.revenue_actual,
                    iv = excluded.iv,
                    iv_rank = excluded.iv_rank,
                    iv_percentile = excluded.iv_percentile,
                    market_cap = excluded.market_cap,
                    price_at_fetch = excluded.price_at_fetch,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    row["code"], row["earnings_date"], row["market"],
                    row.get("name") or "", row.get("pub_type") or "UNKNOWN",
                    row.get("period_text"), row.get("eps_predict"), row.get("eps_actual"),
                    row.get("revenue_predict"), row.get("revenue_actual"),
                    row.get("iv"), row.get("iv_rank"), row.get("iv_percentile"),
                    row.get("market_cap"), row.get("price_at_fetch"), now, now,
                ),
            )
            if key in existing:
                updated += 1
            else:
                inserted += 1

        pruned = conn.execute(
            "DELETE FROM earnings_calendar WHERE earnings_date < ?", (cutoff,)
        ).rowcount

    return {"inserted": inserted, "updated": updated, "pruned": max(pruned, 0)}


def get_upcoming_earnings(
    codes: list[str] | None = None,
    days_ahead: int = 14,
    days_back: int = 0,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Calendar rows in a date window, soonest first, with any outlook attached.

    `days_back` exists for the alert that notices a report has already
    happened; it defaults to 0 so the ordinary "what is coming up" call is not
    quietly showing the past.
    """
    today = datetime.now(timezone.utc).date()
    lo = (today - timedelta(days=days_back)).isoformat()
    hi = (today + timedelta(days=days_ahead)).isoformat()

    sql = """
        SELECT e.*, w.name AS ticker_name,
               o.headline, o.what_to_watch, o.news_summary, o.uncertainty,
               o.generated_at AS outlook_generated_at, o.model AS outlook_model,
               o.sources AS outlook_sources
        FROM earnings_calendar e
        LEFT JOIN watchlist_cache w ON w.code = e.code
        LEFT JOIN earnings_outlooks o
               ON o.code = e.code AND o.earnings_date = e.earnings_date
        WHERE e.earnings_date >= ? AND e.earnings_date <= ?
    """
    params: list[Any] = [lo, hi]
    if market:
        sql += " AND e.market = ?"
        params.append(market)
    if codes is not None:
        if not codes:
            return []
        sql += f" AND e.code IN ({','.join('?' * len(codes))})"
        params.extend(codes)
    sql += " ORDER BY e.earnings_date ASC, e.code ASC"

    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:
        r["what_to_watch"] = json.loads(r["what_to_watch"]) if r.get("what_to_watch") else None
        r["outlook_sources"] = json.loads(r["outlook_sources"]) if r.get("outlook_sources") else None
    return rows


def get_next_earnings_for_code(code: str) -> dict[str, Any] | None:
    """The soonest event today or later for one ticker."""
    today = datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM earnings_calendar WHERE code = ? AND earnings_date >= ? "
            "ORDER BY earnings_date ASC LIMIT 1",
            (code, today),
        ).fetchone()
    return dict(row) if row else None


def get_next_earnings_for_codes(codes: list[str]) -> dict[str, dict[str, Any] | None]:
    """`get_next_earnings_for_code` for a whole list, in one query.

    Every code in `codes` is a key, mapping to None where there is no upcoming
    event — so the result reads exactly like the per-code dict comprehension
    it replaces.

    Cannot reuse `get_upcoming_earnings`: that one LEFT JOINs the outlook
    tables and bounds the window with `days_ahead`, so it returns both a
    different shape and a different set of rows.
    """
    if not codes:
        return {}
    today = datetime.now(timezone.utc).date().isoformat()
    marks = ",".join("?" * len(codes))
    out: dict[str, dict[str, Any] | None] = {c: None for c in codes}
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY code
                            ORDER BY earnings_date ASC) AS _rn
                    FROM earnings_calendar
                    WHERE code IN ({marks}) AND earnings_date >= ?
                ) WHERE _rn = 1""",
            (*codes, today),
        ).fetchall()
    for row in rows:
        item = dict(row)
        item.pop("_rn", None)
        out[item["code"]] = item
    return out


def upsert_earnings_outlook(
    *, code: str, earnings_date: str, model: str, headline: str,
    what_to_watch: list[str], news_summary: str, uncertainty: str,
    sources: dict[str, Any] | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO earnings_outlooks (
                code, earnings_date, generated_at, model, headline,
                what_to_watch, news_summary, uncertainty, sources
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code, earnings_date) DO UPDATE SET
                generated_at = excluded.generated_at,
                model = excluded.model,
                headline = excluded.headline,
                what_to_watch = excluded.what_to_watch,
                news_summary = excluded.news_summary,
                uncertainty = excluded.uncertainty,
                sources = excluded.sources
            """,
            (code, earnings_date, now_iso(), model, headline,
             json.dumps(what_to_watch), news_summary, uncertainty,
             json.dumps(sources or {})),
        )
        return int(cur.lastrowid or 0)


def get_outlook(code: str, earnings_date: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM earnings_outlooks WHERE code = ? AND earnings_date = ?",
            (code, earnings_date),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["what_to_watch"] = json.loads(d["what_to_watch"])
    d["sources"] = json.loads(d["sources"] or "{}")
    return d


def earnings_last_refreshed_at() -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(last_seen_at) AS t FROM earnings_calendar").fetchone()
    return row["t"] if row and row["t"] else None


# --------------------------------------------------------------------------
# Alert acknowledgements
# --------------------------------------------------------------------------

def acknowledge_alert(fingerprint: str, rule: str, code: str, severity: str,
                      ttl_hours: float) -> str:
    """Silence one alert until an absolute time. Returns that expiry."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(
        timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_acks (fingerprint, rule, code, severity,
                                    acknowledged_at, expires_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                acknowledged_at = excluded.acknowledged_at,
                expires_at = excluded.expires_at,
                severity = excluded.severity
            """,
            (fingerprint, rule, code, severity, now_iso(), expires),
        )
    return expires


def unacknowledge_alert(fingerprint: str) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM alert_acks WHERE fingerprint = ?", (fingerprint,)
        ).rowcount > 0


def active_alert_acks() -> dict[str, str]:
    """{fingerprint: expires_at} for unexpired acks; expired ones are pruned.

    Pruned on read rather than by a timer: the read happens every 60s while
    anyone is looking, and a table this small does not deserve a scheduler job.
    """
    now = now_iso()
    with get_connection() as conn:
        conn.execute("DELETE FROM alert_acks WHERE expires_at <= ?", (now,))
        return {
            r["fingerprint"]: r["expires_at"]
            for r in conn.execute("SELECT fingerprint, expires_at FROM alert_acks")
        }


def upsert_feed_health(entries: list[dict[str, Any]]) -> None:
    """Record the outcome of a refresh for every feed it touched."""
    now = now_iso()
    with get_connection() as conn:
        for e in entries:
            ok = e["status"] == "ok"
            conn.execute(
                """
                INSERT INTO news_feed_health
                    (feed_key, url, last_attempt_at, last_success_at,
                     last_status, last_error, consecutive_failures, articles_last_run)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(feed_key) DO UPDATE SET
                    url = excluded.url,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = COALESCE(excluded.last_success_at,
                                               news_feed_health.last_success_at),
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    consecutive_failures = CASE WHEN excluded.last_status = 'ok'
                        THEN 0 ELSE news_feed_health.consecutive_failures + 1 END,
                    articles_last_run = excluded.articles_last_run
                """,
                (e["feed_key"], e["url"], now, now if ok else None,
                 e["status"], e.get("error"), 0 if ok else 1,
                 int(e.get("articles", 0))),
            )


def get_feed_health() -> list[dict[str, Any]]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM news_feed_health ORDER BY feed_key"
        ).fetchall()]


# --------------------------------------------------------------------------
# Sessions and login throttling
#
# Hashing happens in services/auth_service.py, not here: this module persists
# what it is given and stays free of crypto, the same way it stores a feature
# vector without knowing how one is built.
# --------------------------------------------------------------------------

def create_session(token_hash: str, expires_at: str,
                   user_agent: str | None, ip: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, created_at, expires_at,
                                       user_agent, created_ip)
            VALUES (?,?,?,?,?)
            ON CONFLICT(token_hash) DO UPDATE SET
                expires_at = excluded.expires_at
            """,
            (token_hash, now_iso(), expires_at, user_agent, ip),
        )


def get_session(token_hash: str) -> dict[str, Any] | None:
    """The session row if it exists and has not expired; expired rows pruned.

    Pruned on read, like `active_alert_acks` — this runs on every
    authenticated request, so a scheduler job would be redundant. The DELETE
    is unconditional rather than scoped to this token so the table cannot
    accumulate rows for sessions nobody ever presents again.
    """
    now = now_iso()
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
        row = conn.execute(
            "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None


def delete_session(token_hash: str) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
        ).rowcount > 0


def delete_all_sessions() -> int:
    """Log out every device. Returns how many sessions were killed."""
    with get_connection() as conn:
        return conn.execute("DELETE FROM auth_sessions").rowcount


def record_login_failure(ip: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO auth_login_failures (ip, attempted_at) VALUES (?,?)",
            (ip, now_iso()),
        )


def count_login_failures(ip: str, window_minutes: float) -> int:
    """Failures from this IP inside the window. Older rows are pruned."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_login_failures WHERE attempted_at <= ?",
                     (cutoff,))
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM auth_login_failures WHERE ip = ?", (ip,)
        ).fetchone()
        return int(row["n"])


def clear_login_failures(ip: str) -> None:
    """Called on a successful login, so a legitimate user is not locked out by
    their own earlier typos."""
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_login_failures WHERE ip = ?", (ip,))


# --------------------------------------------------------------------------
# Push subscriptions and push dedup
# --------------------------------------------------------------------------

def upsert_push_subscription(endpoint: str, p256dh: str, auth: str,
                             user_agent: str | None) -> None:
    """Idempotent on `endpoint` — re-subscribing the same device is a no-op
    rather than a duplicate, and it resets failure_count because a device that
    just subscribed is by definition reachable."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth,
                                            created_at, user_agent)
            VALUES (?,?,?,?,?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent,
                failure_count = 0
            """,
            (endpoint, p256dh, auth, now_iso(), user_agent),
        )


def list_push_subscriptions() -> list[dict[str, Any]]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM push_subscriptions ORDER BY id")]


def delete_push_subscription(endpoint: str) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        ).rowcount > 0


def record_push_success(endpoint: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE push_subscriptions
               SET last_success_at = ?, failure_count = 0
               WHERE endpoint = ?""",
            (now_iso(), endpoint),
        )


def record_push_failure(endpoint: str) -> int:
    """Bump the failure counter and return its new value."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE push_subscriptions
               SET failure_count = failure_count + 1
               WHERE endpoint = ?""",
            (endpoint,),
        )
        row = conn.execute(
            "SELECT failure_count FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        return int(row["failure_count"]) if row else 0


def pushed_fingerprints(retention_days: int = 30) -> set[str]:
    """Fingerprints already pushed. Rows older than the window are pruned.

    Retention exists so the table cannot grow without bound, not to let an
    alert re-fire: a fact still true after `retention_days` genuinely is worth
    saying again, and the acknowledgement machinery is what silences the ones
    that are not.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute("DELETE FROM alert_pushes WHERE pushed_at <= ?", (cutoff,))
        return {r["fingerprint"]
                for r in conn.execute("SELECT fingerprint FROM alert_pushes")}


def record_alert_push(fingerprint: str, rule: str, code: str,
                      severity: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_pushes (fingerprint, rule, code, severity, pushed_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET pushed_at = excluded.pushed_at
            """,
            (fingerprint, rule, code, severity, now_iso()),
        )


# ---------------------------------------------------------------------------
# Sector rotation (decisions #71)
#
# Every writer here batches into ONE connection and one transaction. That is
# not style: get_connection() sets `PRAGMA synchronous = FULL` on the stated
# premise that write volume is trivial because every row follows 60-120s of
# inference. A rotation ingest writes ~1,600 rows in a burst and breaks that
# premise unless batched — the same correction decisions #39 had to make for
# the news refresh.
# ---------------------------------------------------------------------------


def upsert_sector_plates(plates: Sequence[dict[str, Any]]) -> int:
    """Snapshot the plate universe. Returns rows written."""
    if not plates:
        return 0
    ts = now_iso()
    rows = [
        (
            p["plate_code"],
            p["market"],
            p["plate_name"],
            p["plate_class"],
            p.get("plate_id"),
            p.get("sector_group"),
            int(p.get("constituent_count") or 0),
            ts,
            ts,
        )
        for p in plates
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_plates
                (plate_code, market, plate_name, plate_class, plate_id,
                 sector_group, constituent_count, first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plate_code) DO UPDATE SET
                plate_name        = excluded.plate_name,
                plate_class       = excluded.plate_class,
                plate_id          = excluded.plate_id,
                sector_group      = excluded.sector_group,
                constituent_count = CASE
                    WHEN excluded.constituent_count > 0 THEN excluded.constituent_count
                    ELSE sector_plates.constituent_count END,
                last_seen_at      = excluded.last_seen_at
            """,
            rows,
        )
    return len(rows)


def replace_plate_members(plate_code: str, members: Sequence[dict[str, Any]]) -> int:
    """Replace one plate's constituent list, and refresh its member count.

    A full replace rather than an upsert: a constituent that LEFT the plate
    must disappear, and an upsert would leave it there forever.
    """
    ts = now_iso()
    rows = [(plate_code, m["code"], m.get("stock_name"), ts) for m in members if m.get("code")]
    with get_connection() as conn:
        conn.execute("DELETE FROM sector_plate_members WHERE plate_code = ?", (plate_code,))
        if rows:
            conn.executemany(
                """INSERT INTO sector_plate_members (plate_code, code, stock_name, last_seen_at)
                   VALUES (?,?,?,?)""",
                rows,
            )
        conn.execute(
            """UPDATE sector_plates
               SET constituent_count = ?, members_synced_at = ?, last_seen_at = ?
               WHERE plate_code = ?""",
            (len(rows), ts, ts, plate_code),
        )
    return len(rows)


def upsert_sector_bars(bars: Sequence[dict[str, Any]]) -> int:
    """Persist plate OHLCV bars. Idempotent on (plate_code, bar_date)."""
    if not bars:
        return 0
    ts = now_iso()
    rows = [
        (
            b["plate_code"],
            b["bar_date"],
            b.get("close"),
            b.get("change_rate"),
            b.get("volume"),
            b.get("turnover"),
            int(bool(b.get("suspect_bar"))),
            ts,
        )
        for b in bars
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_bars
                (plate_code, bar_date, close, change_rate, volume, turnover,
                 suspect_bar, ingested_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(plate_code, bar_date) DO UPDATE SET
                close       = excluded.close,
                change_rate = excluded.change_rate,
                volume      = excluded.volume,
                turnover    = excluded.turnover,
                suspect_bar = excluded.suspect_bar,
                ingested_at = excluded.ingested_at
            """,
            rows,
        )
    return len(rows)


def upsert_sector_breadth(entries: Sequence[dict[str, Any]]) -> int:
    if not entries:
        return 0
    ts = now_iso()
    rows = [
        (
            e["plate_code"],
            e["as_of_date"],
            e.get("raise_count"),
            e.get("fall_count"),
            e.get("equal_count"),
            e.get("last_price"),
            e.get("turnover"),
            int(bool(e.get("partial_session", True))),
            ts,
        )
        for e in entries
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_breadth
                (plate_code, as_of_date, raise_count, fall_count, equal_count,
                 last_price, turnover, partial_session, captured_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plate_code, as_of_date) DO UPDATE SET
                raise_count     = excluded.raise_count,
                fall_count      = excluded.fall_count,
                equal_count     = excluded.equal_count,
                last_price      = excluded.last_price,
                turnover        = excluded.turnover,
                partial_session = excluded.partial_session,
                captured_at     = excluded.captured_at
            """,
            rows,
        )
    return len(rows)


def upsert_rotation_scores(scores: Sequence[dict[str, Any]]) -> int:
    if not scores:
        return 0
    ts = now_iso()
    rows = [
        (
            s["plate_code"],
            s["as_of_date"],
            int(s["window_days"]),
            s.get("score"),
            json.dumps(s.get("components") or {}),
            s.get("rel_return_pct"),
            s.get("turnover_thrust"),
            s.get("breadth"),
            s.get("persistence"),
            s.get("news_thrust"),
            int(s.get("sessions_used") or 0),
            s.get("constituents"),
            float(s.get("coverage") or 0.0),
            int(bool(s.get("thin_session"))),
            int(bool(s.get("sufficient"))),
            ts,
        )
        for s in scores
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_rotation_scores
                (plate_code, as_of_date, window_days, score, components,
                 rel_return_pct, turnover_thrust, breadth, persistence, news_thrust,
                 sessions_used, constituents, coverage, thin_session, sufficient, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plate_code, as_of_date, window_days) DO UPDATE SET
                score           = excluded.score,
                components      = excluded.components,
                rel_return_pct  = excluded.rel_return_pct,
                turnover_thrust = excluded.turnover_thrust,
                breadth         = excluded.breadth,
                persistence     = excluded.persistence,
                news_thrust     = excluded.news_thrust,
                sessions_used   = excluded.sessions_used,
                constituents    = excluded.constituents,
                coverage        = excluded.coverage,
                thin_session    = excluded.thin_session,
                sufficient      = excluded.sufficient,
                computed_at     = excluded.computed_at
            """,
            rows,
        )
    return len(rows)


def upsert_etf_flows(flows: Sequence[dict[str, Any]]) -> int:
    if not flows:
        return 0
    ts = now_iso()
    rows = [
        (
            f["etf_code"],
            f["flow_date"],
            f.get("in_flow"),
            f.get("main_in_flow"),
            f.get("super_in_flow"),
            f.get("big_in_flow"),
            f.get("mid_in_flow"),
            f.get("sml_in_flow"),
            f.get("trust_aum"),
            f.get("trust_outstanding_units"),
            f.get("last_price"),
            ts,
        )
        for f in flows
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_etf_flows
                (etf_code, flow_date, in_flow, main_in_flow, super_in_flow, big_in_flow,
                 mid_in_flow, sml_in_flow, trust_aum, trust_outstanding_units,
                 last_price, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            -- COALESCE on EVERY column, in both directions. The flow rows and
            -- the AUM/units snapshot come from two different calls and land on
            -- the SAME (etf_code, flow_date) key, each carrying only its own
            -- half. Without this the snapshot's NULL in_flow silently
            -- overwrote a real day's flow — measured, not hypothetical.
            ON CONFLICT(etf_code, flow_date) DO UPDATE SET
                in_flow                 = COALESCE(excluded.in_flow, sector_etf_flows.in_flow),
                main_in_flow            = COALESCE(excluded.main_in_flow, sector_etf_flows.main_in_flow),
                super_in_flow           = COALESCE(excluded.super_in_flow, sector_etf_flows.super_in_flow),
                big_in_flow             = COALESCE(excluded.big_in_flow, sector_etf_flows.big_in_flow),
                mid_in_flow             = COALESCE(excluded.mid_in_flow, sector_etf_flows.mid_in_flow),
                sml_in_flow             = COALESCE(excluded.sml_in_flow, sector_etf_flows.sml_in_flow),
                trust_aum               = COALESCE(excluded.trust_aum, sector_etf_flows.trust_aum),
                trust_outstanding_units = COALESCE(excluded.trust_outstanding_units,
                                                   sector_etf_flows.trust_outstanding_units),
                last_price              = COALESCE(excluded.last_price, sector_etf_flows.last_price),
                ingested_at             = excluded.ingested_at
            """,
            rows,
        )
    return len(rows)


def get_sector_universe(
    market: str = "US", plate_class: str | None = None
) -> list[dict[str, Any]]:
    """The plate universe, newest snapshot. Empty until a refresh has run."""
    sql = "SELECT * FROM sector_plates WHERE market = ?"
    args: list[Any] = [market]
    if plate_class:
        sql += " AND plate_class = ?"
        args.append(plate_class)
    sql += " ORDER BY plate_name"
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_sector_bars(plate_codes: Sequence[str], limit_per_plate: int = 200) -> dict[str, list[dict[str, Any]]]:
    """Bars for many plates in ONE query, oldest first per plate.

    Batched rather than per-plate: 262 plates is 262 round trips otherwise,
    the same reason `signals.build_opportunities` takes one batched
    `get_setup_history` call instead of N.
    """
    if not plate_codes:
        return {}
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in plate_codes}
    with get_connection() as conn:
        for chunk_start in range(0, len(plate_codes), 400):
            chunk = list(plate_codes)[chunk_start : chunk_start + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY plate_code ORDER BY bar_date DESC
                    ) AS rn
                    FROM sector_bars WHERE plate_code IN ({marks})
                )
                WHERE rn <= ?
                ORDER BY plate_code, bar_date ASC
                """,
                (*chunk, limit_per_plate),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d.pop("rn", None)
                out[d["plate_code"]].append(d)
    return out


def get_latest_breadth(market: str = "US") -> dict[str, dict[str, Any]]:
    """Most recent breadth row per plate, keyed by plate_code."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT b.* FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY plate_code ORDER BY as_of_date DESC
                ) AS rn
                FROM sector_breadth
            ) b
            JOIN sector_plates p ON p.plate_code = b.plate_code
            WHERE b.rn = 1 AND p.market = ?
            """,
            (market,),
        ).fetchall()
    return {r["plate_code"]: dict(r) for r in rows}


def get_rotation_board(
    market: str = "US",
    window_days: int = 5,
    plate_class: str | None = None,
    as_of_date: str | None = None,
) -> list[dict[str, Any]]:
    """Scored plates for one window on one day, best score first.

    `as_of_date` defaults to the newest date that HAS scores rather than to
    today — a run that skipped (the gateway was busy) must show yesterday's
    board, not an empty one.
    """
    with get_connection() as conn:
        if as_of_date is None:
            row = conn.execute(
                """SELECT MAX(s.as_of_date) FROM sector_rotation_scores s
                   JOIN sector_plates p ON p.plate_code = s.plate_code
                   WHERE p.market = ? AND s.window_days = ?""",
                (market, window_days),
            ).fetchone()
            as_of_date = row[0] if row else None
        if not as_of_date:
            return []
        sql = """
            SELECT s.*, p.plate_name, p.plate_class, p.sector_group, p.constituent_count
            FROM sector_rotation_scores s
            JOIN sector_plates p ON p.plate_code = s.plate_code
            WHERE p.market = ? AND s.window_days = ? AND s.as_of_date = ?
        """
        args: list[Any] = [market, window_days, as_of_date]
        if plate_class:
            sql += " AND p.plate_class = ?"
            args.append(plate_class)
        sql += " ORDER BY s.score DESC NULLS LAST, p.plate_name ASC"
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["components"] = json.loads(d.get("components") or "{}")
        out.append(d)
    return out


def get_score_history(plate_code: str, window_days: int = 5, limit: int = 60) -> list[dict[str, Any]]:
    """One plate's score series, oldest first — the sparkline's input."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT as_of_date, score, sufficient FROM sector_rotation_scores
               WHERE plate_code = ? AND window_days = ?
               ORDER BY as_of_date DESC LIMIT ?""",
            (plate_code, window_days, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_plate_members(plate_code: str) -> list[dict[str, Any]]:
    """Constituents, with the watchlist join that makes a sector actionable."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.code, m.stock_name, w.name AS watchlist_name, w.enabled
            FROM sector_plate_members m
            LEFT JOIN watchlist_cache w ON w.code = m.code
            WHERE m.plate_code = ?
            ORDER BY (w.code IS NULL), m.code
            """,
            (plate_code,),
        ).fetchall()
    return [
        {
            "code": r["code"],
            "name": r["watchlist_name"] or r["stock_name"],
            "on_watchlist": r["watchlist_name"] is not None,
            "enabled": bool(r["enabled"]) if r["enabled"] is not None else False,
        }
        for r in rows
    ]


def get_etf_flows(etf_codes: Sequence[str], days: int = 21) -> dict[str, list[dict[str, Any]]]:
    """Recent flow rows per ETF, oldest first."""
    if not etf_codes:
        return {}
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in etf_codes}
    marks = ",".join("?" * len(etf_codes))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY etf_code ORDER BY flow_date DESC
                ) AS rn
                FROM sector_etf_flows WHERE etf_code IN ({marks})
            )
            WHERE rn <= ?
            ORDER BY etf_code, flow_date ASC
            """,
            (*etf_codes, days),
        ).fetchall()
    for r in rows:
        d = dict(r)
        d.pop("rn", None)
        out[d["etf_code"]].append(d)
    return out


def upsert_sector_narrative(
    *,
    plate_code: str,
    as_of_date: str,
    window_days: int,
    headline: str,
    candidate_driver: str,
    supporting_headlines: Sequence[str],
    contradicts: str,
    confidence_label: str,
    model: str,
    sources: dict[str, Any] | None = None,
) -> None:
    """Store one sector narrative, replacing any for the same day and window.

    Keyword-only, like `insert_trade_setup`, because eleven positional
    arguments of mostly-strings is how the wrong two get swapped.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sector_narratives
                (plate_code, as_of_date, window_days, headline, candidate_driver,
                 supporting_headlines, contradicts, confidence_label, model,
                 sources, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plate_code, as_of_date, window_days) DO UPDATE SET
                headline             = excluded.headline,
                candidate_driver     = excluded.candidate_driver,
                supporting_headlines = excluded.supporting_headlines,
                contradicts          = excluded.contradicts,
                confidence_label     = excluded.confidence_label,
                model                = excluded.model,
                sources              = excluded.sources,
                generated_at         = excluded.generated_at
            """,
            (
                plate_code, as_of_date, int(window_days), headline, candidate_driver,
                json.dumps(list(supporting_headlines)), contradicts, confidence_label,
                model, json.dumps(sources or {}), now_iso(),
            ),
        )


def get_sector_narrative(
    plate_code: str, window_days: int, as_of_date: str | None = None
) -> dict[str, Any] | None:
    """The newest narrative for a plate and window, or one specific day."""
    with get_connection() as conn:
        if as_of_date:
            row = conn.execute(
                """SELECT * FROM sector_narratives
                   WHERE plate_code = ? AND window_days = ? AND as_of_date = ?""",
                (plate_code, window_days, as_of_date),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM sector_narratives
                   WHERE plate_code = ? AND window_days = ?
                   ORDER BY as_of_date DESC, id DESC LIMIT 1""",
                (plate_code, window_days),
            ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["supporting_headlines"] = json.loads(out.get("supporting_headlines") or "[]")
    out["sources"] = json.loads(out.get("sources") or "{}")
    return out


def get_narrated_plates(as_of_date: str, window_days: int) -> set[str]:
    """Which plates already have a narrative for this day and window.

    Lets the job skip work rather than re-spending GPU time on a sector whose
    reading has not changed — the `_needs_outlook` instinct.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT plate_code FROM sector_narratives
               WHERE as_of_date = ? AND window_days = ?""",
            (as_of_date, window_days),
        ).fetchall()
    return {r["plate_code"] for r in rows}


def watchlist_codes_in_plate(plate_code: str) -> list[str]:
    """A plate's constituents that are also on the watchlist.

    This is the whole news join. `news_article_tickers` links articles only to
    WATCHLIST tickers (decisions #42 — association is definitional or exact
    company-name only, never a bare symbol), so a sector holding none of the
    user's names genuinely has no ticker-linked news and must say so rather
    than being padded with unrelated headlines.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT m.code FROM sector_plate_members m
               JOIN watchlist_cache w ON w.code = m.code
               WHERE m.plate_code = ? ORDER BY m.code""",
            (plate_code,),
        ).fetchall()
    return [r["code"] for r in rows]
