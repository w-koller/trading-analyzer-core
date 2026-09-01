"""The sector taxonomy: which plates exist, and which tickers are in them.

WHY THE TAXONOMY IS NOT OURS

This project has no sector field and never had one, and the tempting fix — a
hand-written map from ticker to sector, or from sector to sub-sector — is
exactly the thing decisions #40 refused for feed categories: a curated table
that looks like half an hour of typing and becomes a permanent maintenance
surface that rots invisibly.

So the taxonomy is Moomoo's, and the rule for what counts is mechanical:

    **A plate is a sector if, and only if, it appears in
    `get_plate_list(market, INDUSTRY)` or `get_plate_list(market, CONCEPT)`.**

`get_owner_plate` returns far more than that. Measured 2026-08-30, ten
watchlist tickers produced 7 INDUSTRY rows, 82 CONCEPT rows and 90 typed
OTHER — and OTHER is where Moomoo files its own broker product lists
('FUTU-CA 美股定投', 'HK券商美股定投', 'Fractional Shares JP') and novelty
baskets ('Nancy Pelosi Portfolio', 'Donald Trump', 'Government Pension Fund
of Norway') right next to real themes like 'Biotechnology'.

Intersecting against the enumeration excludes all of that **without ever
naming it**. A broker list added next month is excluded on day one with no
code change. The registry is remote, so we snapshot it and intersect rather
than hand-maintain it — `news_feeds.py`'s "registry is code, not
configuration" argument, inverted because here the registry is not ours.

Note carefully: filter on MEMBERSHIP, not on `plate_type`. They are not the
same test. 'AI semiconductor' arrives typed OTHER, and if it is in the
CONCEPT enumeration it is a real sector; if it is not, it is out. The honest
cost of the rule is that a genuine theme Moomoo declines to enumerate is
dropped — acceptable, because the same concepts exist in the CONCEPT list
('Artificial Intelligence', 'AI Chip'), and a mechanical rule that loses two
good rows beats a hand-maintained one that silently rots.

SUB-SECTOR GRANULARITY IS ALREADY THERE

Moomoo's INDUSTRY list is at the granularity the request asked for: there is
no "Technology" plate; there is "Semiconductors", "Semiconductor Equipment &
Materials", "Software - Infrastructure", "Consumer Electronics". The
sub-level exists and the PARENT is what is missing. `derive_sector_group`
recovers a parent by splitting the vendor's own label, and it is display-only
— never an input to any score. The cross-cutting axis that actually answers
"is money leaving AI hardware for AI software" is CONCEPT, not a parent
industry, so both axes stay flat and side by side. Do not invent a hierarchy
Moomoo does not publish.

RATE LIMITS, ALL MEASURED

`get_plate_stock` and `get_owner_plate` are each **10 calls / 30 seconds**,
stated by the server itself and returned as an ordinary error string. A full
member refresh of 262 plates is therefore ~13 minutes, which is far too long
to hold the OpenD mutex for — so member lists refresh as a ROTATING SLICE,
least-recently-seen first, the same treatment `news_ticker_batch` gives
per-ticker feeds and for the same reason. The universe converges in about a
week and `constituent_count` fills in progressively; until a plate has been
visited, its count is 0, which callers must read as **unknown, not zero**.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db
from app.services.sdk_gateway import RateLimiter

logger = logging.getLogger(__name__)

INDUSTRY = "INDUSTRY"
CONCEPT = "CONCEPT"
PLATE_CLASSES = (INDUSTRY, CONCEPT)

#: How stale the plate LIST may get before a refresh is due.
UNIVERSE_MAX_AGE_DAYS = 7.0

#: Member lists refreshed per run, least-recently-synced first.
#:
#: get_plate_stock is 10 calls/30s, so a full 262-plate pass is ~16 minutes —
#: far too long to hold the OpenD mutex, hence a slice. 80 at 8 calls/30s is
#: ~5 minutes and brings the universe round in 3-4 nightly runs.
#:
#: Raised from 40 after the first real ingest: at 40 the board had member
#: data for only 14 of its 100 rows, which left almost every plate marked
#: unconfirmed and left `rotation_pairs` with nothing to pair, because a pair
#: needs BOTH sides to have constituents. Convergence speed is not cosmetic
#: here — it gates two features, not just a count.
MEMBER_REFRESH_BATCH = 80

#: Paced below the measured 10/30s ceilings so an interactive request can
#: still get through while a batch job runs.
_PLATE_CALLS = 8
_RATE_WINDOW = 30.0

#: Moomoo `stock_type` values that `get_owner_plate` refuses. It fails the
#: WHOLE batch on one of these, so they are filtered out rather than retried.
UNSUPPORTED_OWNER_PLATE_TYPES = frozenset({"ETF", "BOND", "WARRANT", "FUTURE", "IDX", "INDEX"})

#: `get_owner_plate` accepts a list; 20 keeps one bad batch cheap to redo.
OWNER_PLATE_CHUNK = 20

#: A plate holding more than this share of the universe's distinct tickers is
#: excluded from relatedness as degenerate — a basket containing essentially
#: everything is related to everything by construction, so the number means
#: nothing.
#:
#: **This is deliberately set high, and an earlier 0.25 was wrong.** Measured
#: against live data on 2026-08-30: Biotechnology has 603 constituents and
#: Banks - Regional 361, against 1,894 distinct tickers known at the time —
#: so a quarter-share rule excluded two perfectly ordinary sectors. Worse,
#: the denominator is unstable by design, because member lists arrive as a
#: rotating slice and the universe is only partially populated for about a
#: week.
#:
#: The Jaccard index already does this work properly and needs no help: a
#: 603-member plate sharing 40 constituents with a 72-member sector scores
#: 0.063, while a genuinely related 40-member plate sharing 30 scores 0.366 —
#: a six-fold separation that ranks proxies to the bottom on their own. So
#: this cap now catches only the pathological case and leaves the judgement
#: to the metric that was already making it correctly.
CONCEPT_MARKET_PROXY_SHARE = 0.9

#: An absolute floor as well as the share, because the denominator is a
#: partially-populated universe for the first week and a share of a small
#: number is a handful of tickers.
CONCEPT_MARKET_PROXY_MIN_MEMBERS = 500

#: Floors on what counts as "related" at all. Measured in a real browser on
#: 2026-08-30 against the live corpus: Biotechnology (603 constituents) was
#: reporting NVIDIA Portfolio and Crypto as its nearest sectors, each on a
#: SINGLE shared ticker (jaccard 0.0016). One coincidental overlap is not a
#: relationship, and rendering it as one is worse than rendering nothing —
#: the reader has no way to tell a real neighbour from an artefact.
#:
#: The separation is clear in the data: the genuine neighbour in that same
#: sample (Semiconductors INDUSTRY <-> Semiconductors CONCEPT) scored 0.1447
#: on 11 shared names, an order of magnitude above the noise at 0.0015-0.03.
#:
#: This matters most while the universe is sparse. Member lists arrive as a
#: rotating slice, so early on a plate's only fetched overlaps may all be
#: accidental — exactly when a floor is doing the most work.
MIN_SHARED_MEMBERS = 3
MIN_JACCARD = 0.05


def derive_sector_group(plate_name: str) -> str:
    """Parent group for a plate, from the vendor's own label.

    "Software - Infrastructure" -> "Software". A name with no separator
    becomes its own group, which fails to group rather than grouping
    wrongly. Display only — never an input to a score.
    """
    if not plate_name:
        return ""
    return plate_name.split(" - ", 1)[0].strip()


def plate_universe(market: str = "US", plate_class: str | None = None) -> list[dict[str, Any]]:
    """The stored plate universe. Empty until `refresh_universe` has run."""
    return db.get_sector_universe(market=market, plate_class=plate_class)


def universe_age_days(market: str = "US") -> float | None:
    """Days since the plate list was last refreshed, or None if never."""
    rows = db.get_sector_universe(market=market)
    if not rows:
        return None
    newest = max((r.get("last_seen_at") or "") for r in rows)
    seen = db.parse_iso(newest)
    if seen is None:
        return None
    now = db.parse_iso(db.now_iso())
    assert now is not None
    return (now - seen).total_seconds() / 86400.0


def refresh_universe(
    gateway,
    market: str = "US",
    member_batch: int = MEMBER_REFRESH_BATCH,
    limiter: RateLimiter | None = None,
) -> dict[str, Any]:
    """Re-snapshot the plate list, then refresh a rotating slice of members.

    Never raises on a partial failure — a universe with 250 of 262 plates is
    far more useful than none, the same trade `sync_watchlist` makes.
    """
    import moomoo as ft

    limiter = limiter or RateLimiter(_PLATE_CALLS, _RATE_WINDOW, "sector universe")
    result: dict[str, Any] = {
        "market": market,
        "plates": 0,
        "by_class": {},
        "members_refreshed": 0,
        "member_rows": 0,
        "failures": [],
    }

    plates: list[dict[str, Any]] = []
    for cls in PLATE_CLASSES:
        try:
            rows = gateway.get_plate_list(market, getattr(ft.Plate, cls))
        except Exception as exc:
            logger.warning("plate list %s/%s failed: %s", market, cls, exc)
            result["failures"].append(f"plate_list:{cls}: {exc}")
            continue
        for r in rows:
            code = r.get("code")
            name = r.get("plate_name")
            if not code or not name:
                continue
            plates.append(
                {
                    "plate_code": code,
                    "market": market,
                    "plate_name": name,
                    "plate_class": cls,
                    "plate_id": r.get("plate_id"),
                    "sector_group": derive_sector_group(name),
                }
            )
        result["by_class"][cls] = len(rows)

    if plates:
        db.upsert_sector_plates(plates)
    result["plates"] = len(plates)

    # Members: a rotating slice, never-fetched first and then oldest first.
    #
    # A TUPLE key, not an `and` chain. The short-circuit version evaluated to
    # False for an unvisited plate and to a string for a visited one, and
    # Python cannot order bool against str — it survived the FIRST run only
    # because every plate was unvisited and every key was False. The crash
    # arrives on the second run, which is the one no fresh-database test
    # reaches. Same class of blind spot as the migration gotcha.
    #
    # Ordered on members_synced_at rather than last_seen_at, because the
    # latter is rewritten for every plate by the list refresh above and would
    # therefore re-pick the same slice forever.
    known = db.get_sector_universe(market=market)
    stale = sorted(
        known,
        key=lambda p: (p.get("members_synced_at") or "", p.get("plate_code") or ""),
    )[:member_batch]
    for p in stale:
        try:
            limiter.acquire()
            members = gateway.get_plate_stock(p["plate_code"])
        except Exception as exc:
            logger.warning("plate members %s failed: %s", p["plate_code"], exc)
            result["failures"].append(f"plate_stock:{p['plate_code']}: {exc}")
            continue
        rows = [
            {"code": m.get("code"), "stock_name": m.get("stock_name")}
            for m in members
            if m.get("code")
        ]
        result["member_rows"] += db.replace_plate_members(p["plate_code"], rows)
        result["members_refreshed"] += 1

    logger.info(
        "sector universe %s: %d plates, %d member lists refreshed (%d rows), %d failures",
        market,
        result["plates"],
        result["members_refreshed"],
        result["member_rows"],
        len(result["failures"]),
    )
    return result


def owner_plates(
    gateway,
    codes: list[str],
    security_types: dict[str, str | None] | None = None,
    limiter: RateLimiter | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Which universe plates each code belongs to, keyed by code.

    Three things this has to survive, all measured:

    1. **One ETF fails the whole batch.** `get_owner_plate` answers "Get
       Stock's Sector interface does not support ETFs type." and returns
       nothing at all — and it fails on the batch's CONTENT, not its size
       (35 of this watchlist's tickers succeeded, 40 failed, because
       US.SMH sits between them). Chunking alone does not fix that. Known
       ETFs are filtered out up front via `security_types`.
    2. **`security_type` is NULL for anything synced before that column
       existed**, so the filter cannot be complete on its own. A chunk that
       fails is retried per-code, which costs one row instead of twenty.
    3. **10 calls / 30s.** The per-code fallback would blow straight through
       that unpaced and return partial data with no exception raised.

    Rows whose `plate_code` is not in the stored universe are dropped — see
    the module docstring.
    """
    if not codes:
        return {}
    limiter = limiter or RateLimiter(_PLATE_CALLS, _RATE_WINDOW, "owner plate")
    security_types = security_types or {}

    known = {p["plate_code"]: p for p in db.get_sector_universe()}
    eligible = [
        c
        for c in codes
        if (security_types.get(c) or "").upper() not in UNSUPPORTED_OWNER_PLATE_TYPES
    ]
    skipped = len(codes) - len(eligible)
    if skipped:
        logger.info("owner_plate: skipped %d known-unsupported code(s)", skipped)

    out: dict[str, list[dict[str, Any]]] = {}

    def absorb(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            code, plate = r.get("code"), r.get("plate_code")
            if not code or plate not in known:
                continue
            meta = known[plate]
            out.setdefault(code, []).append(
                {
                    "plate_code": plate,
                    "plate_name": meta["plate_name"],
                    "plate_class": meta["plate_class"],
                    "sector_group": meta.get("sector_group"),
                }
            )

    for start in range(0, len(eligible), OWNER_PLATE_CHUNK):
        chunk = eligible[start : start + OWNER_PLATE_CHUNK]
        try:
            limiter.acquire()
            absorb(gateway.get_owner_plate(chunk))
        except Exception as exc:
            logger.info(
                "owner_plate batch of %d failed (%s); retrying per code", len(chunk), exc
            )
            for code in chunk:
                try:
                    limiter.acquire()
                    absorb(gateway.get_owner_plate([code]))
                except Exception as inner:
                    logger.debug("owner_plate %s failed: %s", code, inner)
    return out


def related_plates(plate_code: str, limit: int = 10) -> list[dict[str, Any]]:
    """Plates sharing constituents with this one, most overlap first.

    Overlap is the Jaccard index over constituent sets, which is what keeps a
    large plate from looking related to everything: it divides by the UNION,
    so a 603-member plate sharing 40 names with a 72-member sector scores
    0.063 against 0.366 for a genuinely related one.

    The market-proxy exclusion on top of that is a backstop for the
    degenerate case only — see `CONCEPT_MARKET_PROXY_SHARE`, which was set
    far too aggressively at first and excluded Biotechnology.

    Returns [] rather than weak matches when nothing clears
    `MIN_SHARED_MEMBERS` and `MIN_JACCARD`. Showing nothing is the honest
    answer while the universe is still filling in; showing a one-ticker
    coincidence as a neighbour is not.
    """
    with db.get_connection() as conn:
        total_tickers = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM sector_plate_members"
        ).fetchone()[0]
        if not total_tickers:
            return []
        # Both conditions, not either: see CONCEPT_MARKET_PROXY_MIN_MEMBERS.
        max_members = max(
            int(total_tickers * CONCEPT_MARKET_PROXY_SHARE),
            CONCEPT_MARKET_PROXY_MIN_MEMBERS,
        )
        rows = conn.execute(
            """
            WITH mine AS (SELECT code FROM sector_plate_members WHERE plate_code = ?),
                 sizes AS (
                     SELECT plate_code, COUNT(*) AS n
                     FROM sector_plate_members GROUP BY plate_code
                 )
            SELECT m.plate_code, p.plate_name, p.plate_class, p.sector_group,
                   COUNT(*) AS shared, s.n AS other_n,
                   (SELECT COUNT(*) FROM mine) AS mine_n
            FROM sector_plate_members m
            JOIN sector_plates p ON p.plate_code = m.plate_code
            JOIN sizes s ON s.plate_code = m.plate_code
            WHERE m.code IN (SELECT code FROM mine)
              AND m.plate_code != ?
              AND s.n <= ?
            GROUP BY m.plate_code
            ORDER BY shared DESC
            LIMIT ?
            """,
            (plate_code, plate_code, max_members, limit * 3),
        ).fetchall()

    out = []
    for r in rows:
        union = r["mine_n"] + r["other_n"] - r["shared"]
        if union <= 0:
            continue
        jaccard = r["shared"] / union
        # Both floors, not either: a big plate can share 3 names by accident,
        # and two tiny plates can score a high jaccard on one name apiece.
        if r["shared"] < MIN_SHARED_MEMBERS or jaccard < MIN_JACCARD:
            continue
        out.append(
            {
                "plate_code": r["plate_code"],
                "plate_name": r["plate_name"],
                "plate_class": r["plate_class"],
                "sector_group": r["sector_group"],
                "shared_members": r["shared"],
                "jaccard": round(jaccard, 4),
            }
        )
    out.sort(key=lambda d: d["jaccard"], reverse=True)
    return out[:limit]
