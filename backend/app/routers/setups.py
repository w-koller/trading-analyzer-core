"""Trade setup (AI thesis) endpoints — the read side of the scanner."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app import db
from app.services.similarity import describe_vector

router = APIRouter(prefix="/setups", tags=["setups"])


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    """Decode the JSON columns so the frontend doesn't parse strings twice."""
    out = dict(row)
    for field in ("indicator_snapshot", "feature_vector", "similar_setup_ids"):
        raw = out.get(field)
        if isinstance(raw, str):
            try:
                out[field] = json.loads(raw)
            except json.JSONDecodeError:
                out[field] = None
    # ROW_NUMBER()'s output is scaffolding for the latest-per-code window,
    # not part of the row. Left in, it leaks into the public API shape.
    out.pop("_rn", None)
    out["is_delayed_data"] = bool(out.get("is_delayed_data"))
    if isinstance(out.get("feature_vector"), list):
        out["feature_breakdown"] = describe_vector(out["feature_vector"])
    return out


# Whitelist, not interpolation: `sort` arrives from the query string and
# these fragments go straight into SQL.
#
# The secondary keys are what make either order meaningful rather than
# arbitrary. Conviction ties are the common case — most theses land on 5 or 6
# — so without `created_at DESC` a "highest conviction" page would return an
# effectively random slice of the 6s. `id DESC` is the final tie-break
# because created_at has SECOND granularity (db.now_iso uses
# timespec="seconds"), and a scan writes several rows inside one second.
_SORTS = {
    "conviction": "conviction_score DESC, created_at DESC, id DESC",
    "recent": "created_at DESC, id DESC",
}
DEFAULT_SORT = "conviction"


def _recent(limit: int, code: str | None, min_conviction: int | None,
            market: str | None = None, sort: str = DEFAULT_SORT,
            latest_per_code: bool = False):
    """Rows for the list endpoint, ordered by `sort` (a whitelist key).

    `latest_per_code` collapses the result to the newest thesis per ticker.
    The rotation re-analyses every enabled ticker roughly hourly (decisions
    #15), so without it a browse page is seven near-identical cards of
    whatever was scanned most recently and the other 47 tickers never
    surface at all.

    **Which filter goes inside the window and which goes outside is the
    load-bearing part.** `code` and `market` are IDENTITY: they select which
    tickers are in play, and a ticker's market never varies, so they belong
    inside the subquery, before the partition. `min_conviction` is a
    JUDGEMENT about one particular thesis. Inside, it would mean "the newest
    thesis that happens to score >= 6" — which resurfaces this morning's
    stale 7 the moment the current read drops to a 4, presenting superseded
    advice as current. Outside, it means "the current thesis, if it scores
    >= 6", which is what the filter is for.

    The window's own ORDER BY is fixed at `created_at DESC, id DESC` and is
    deliberately NOT `sort`: which row is *latest* is a fact about the data,
    while `sort` only decides how the collapsed set is then ranked.
    """
    order = _SORTS[sort]
    clauses: list[str] = []
    params: list[Any] = []
    if code:
        clauses.append("code = ?")
        params.append(code)
    if market:
        clauses.append("market = ?")
        params.append(market.upper())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    if not latest_per_code:
        if min_conviction is not None:
            where += (" AND " if where else " WHERE ") + "conviction_score >= ?"
            params.append(min_conviction)
        query = f"SELECT * FROM trade_setups{where} ORDER BY {order} LIMIT ?"
    else:
        outer = ["_rn = 1"]
        if min_conviction is not None:
            outer.append("conviction_score >= ?")
            params.append(min_conviction)
        query = (
            "SELECT * FROM ("
            "SELECT *,"
            " ROW_NUMBER() OVER (PARTITION BY code"
            " ORDER BY created_at DESC, id DESC) AS _rn,"
            " COUNT(*) OVER (PARTITION BY code) AS thesis_count"
            f" FROM trade_setups{where}"
            f") WHERE {' AND '.join(outer)}"
            f" ORDER BY {order} LIMIT ?"
        )
    params.append(limit)

    with db.get_connection() as conn:
        return [_hydrate(dict(r)) for r in conn.execute(query, params).fetchall()]


@router.get("")
async def list_setups(
    limit: int = 50,
    code: str | None = None,
    min_conviction: int | None = None,
    market: str | None = None,
    sort: str = DEFAULT_SORT,
    latest_per_code: bool = False,
):
    """List stored theses.

    Defaults to highest-conviction-first: the question this list answers is
    "what does the model think is worth looking at", and that is a ranking,
    not a feed. Callers that genuinely mean "most recent" — the dashboard's
    "Recent theses" section, and anything that filters by a time window or
    tracks live scan progress — must pass `sort=recent` explicitly, because
    a conviction-ranked page can legitimately contain nothing from today.

    `latest_per_code` collapses the list to one row per ticker — the newest
    thesis for each. It is OPT-IN rather than the default because three
    callers depend on the row-per-scan shape (decisions #37), and one breaks
    outright without it: the scan-runner dialog counts rows carrying the
    running scan's id to drive its progress bar, and one-row-per-ticker
    would pin that counter near-flat for the hour a pre-market scan takes.
    Each returned row then carries `thesis_count`, the true number of stored
    theses for that ticker, so the UI can offer the history without a second
    query.
    """
    if sort not in _SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown sort {sort!r}; valid values: {sorted(_SORTS)}",
        )
    setups = await run_in_threadpool(
        _recent, limit, code, min_conviction, market, sort, latest_per_code
    )
    # Echoed so a client can confirm what it actually got rather than assume.
    return {
        "setups": setups,
        "count": len(setups),
        "sort": sort,
        "latest_per_code": latest_per_code,
    }


@router.get("/latest/{code}")
async def latest_for_code(code: str):
    row = await run_in_threadpool(db.get_latest_setup_for_code, code)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no setup recorded for {code}")
    return _hydrate(row)


def _one(setup_id: int):
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM trade_setups WHERE id = ?", (setup_id,)
        ).fetchone()
        return dict(row) if row else None


@router.get("/{setup_id}")
async def get_setup(setup_id: int):
    row = await run_in_threadpool(_one, setup_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no setup {setup_id}")
    return _hydrate(row)


@router.get("/{setup_id}/similar")
async def similar_to(setup_id: int, top_k: int = 3):
    """What the RAG step would retrieve for this setup's vector."""
    row = await run_in_threadpool(_one, setup_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no setup {setup_id}")
    vector = json.loads(row["feature_vector"])
    similar = await run_in_threadpool(
        db.get_similar_setups, vector, top_k, setup_id
    )
    return {"setup_id": setup_id, "similar": similar}
