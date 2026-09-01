"""News endpoints.

Context and a reading list. Nothing here is parsed for numbers or allowed to
drive an indicator — rule #1 keeps every calculation in Python.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app import db, scheduler
from app.config import settings
from app.services import news_feeds, news_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/news", tags=["news"])

VALID_CATEGORIES = ("all", "shocks", "themes", "macro", "watchlist")


def _icon_for(feed_key: str) -> str:
    spec = news_feeds.by_key(feed_key.split(":", 1)[0])
    return spec.icon if spec else "news"


def _shape(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    when = db.parse_iso(row["published_at"])
    return {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "summary": row.get("summary"),
        "source_label": row["source_label"],
        "feed_key": row["feed_key"],
        "category": row["category"],
        "icon": _icon_for(row["feed_key"]),
        "published_at": row["published_at"],
        "published_estimated": bool(row["published_estimated"]),
        "age_seconds": int((now - when).total_seconds()) if when else None,
        "codes": row.get("codes", []),
        "also_in": row.get("also_in", []),
    }


def _collapse(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict], int]:
    """Fold the same story arriving from several outlets into one row.

    Write-side dedup is on URL, which cannot catch this: Yahoo and CNBC
    publish the same headline at different addresses. Collapsing here keeps
    the earliest-published row and lists the others in `also_in` — a display
    choice, and one that is allowed to be wrong, which is exactly why it is
    not done destructively at write time.
    """
    seen: dict[str, dict[str, Any]] = {}
    collapsed = 0
    for row in rows:
        key = row["title_norm"]
        if key in seen:
            other = row["source_label"]
            if other not in seen[key]["also_in"]:
                seen[key]["also_in"].append(other)
            collapsed += 1
            continue
        row["also_in"] = []
        seen[key] = row
    return list(seen.values())[:limit], collapsed


@router.get("")
async def list_news(
    category: str = "all",
    code: str | None = None,
    since_hours: int | None = None,
    limit: int = 50,
    offset: int = 0,
    collapse: bool = True,
):
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown category {category!r}; valid: {list(VALID_CATEGORIES)}",
        )
    now = datetime.now(timezone.utc)
    since = ((now - timedelta(hours=since_hours)).isoformat(timespec="seconds")
             if since_hours else None)

    def _read() -> dict[str, Any]:
        # Over-fetch when collapsing so a page still fills after folding.
        fetch = limit * 3 if collapse else limit
        rows = db.list_news(category=category, code=code,
                            watchlist_only=(category == "watchlist"),
                            since=since, limit=fetch, offset=offset)
        folded = 0
        if collapse:
            rows, folded = _collapse(rows, limit)
        else:
            rows = rows[:limit]
        return {
            "articles": [_shape(r, now) for r in rows],
            "count": len(rows),
            "collapsed": folded,
            "counts_by_category": db.count_news_by_category(),
            "generated_at": now.isoformat(timespec="seconds"),
        }

    return await run_in_threadpool(_read)


@router.get("/top")
async def top_stories(limit: int = 9, max_per_source: int = 2):
    """The dashboard grid: newest across every category.

    `max_per_source` matters. SEC's feed is `action=getcurrent` — every
    filer's 8-Ks, not just the watchlist's — so without a cap it would own
    the entire grid on any busy filing day.
    """
    now = datetime.now(timezone.utc)

    def _read() -> dict[str, Any]:
        rows = db.list_news(limit=limit * 8)
        rows, _ = _collapse(rows, limit * 8)
        picked: list[dict[str, Any]] = []
        per_source: dict[str, int] = {}
        for row in rows:
            src = row["source_label"]
            if per_source.get(src, 0) >= max_per_source:
                continue
            per_source[src] = per_source.get(src, 0) + 1
            picked.append(row)
            if len(picked) >= limit:
                break
        return {"articles": [_shape(r, now) for r in picked], "count": len(picked)}

    return await run_in_threadpool(_read)


@router.get("/feeds")
async def feed_status():
    """Registry crossed with health — the Sources panel."""
    def _read() -> dict[str, Any]:
        health = {h["feed_key"]: h for h in db.get_feed_health()}
        feeds = []
        for spec in news_feeds.FEEDS:
            # Per-ticker feeds get one health row per ticker; summarise.
            related = [h for k, h in health.items() if k.split(":", 1)[0] == spec.key]
            worst = next((h for h in related if h["last_status"] != "ok"), None)
            best = next((h for h in related if h["last_status"] == "ok"), None)
            row = worst or best or {}
            feeds.append({
                "key": spec.key,
                "label": spec.label,
                "category": spec.category,
                "category_label": news_feeds.CATEGORY_LABELS[spec.category],
                "url": spec.url,
                "icon": spec.icon,
                "per_ticker": spec.per_ticker,
                "enabled": spec.key not in set(settings.news_feeds_disabled_list),
                "last_status": row.get("last_status", "unknown"),
                "last_success_at": row.get("last_success_at"),
                "last_attempt_at": row.get("last_attempt_at"),
                "last_error": row.get("last_error"),
                "consecutive_failures": row.get("consecutive_failures", 0),
                "articles_last_run": sum(h.get("articles_last_run", 0) for h in related),
            })
        failing = [f["key"] for f in feeds if f["last_status"] not in ("ok", "unknown")]
        return {"feeds": feeds, "count": len(feeds), "failing": failing}

    return await run_in_threadpool(_read)


@router.post("/refresh")
async def refresh_now():
    """Fetch every feed now.

    Refuses rather than queues while one is running, same reasoning as
    `POST /scan/run` (decisions #33). Takes the news lock, never `_scan_lock`
    — this touches neither OpenD nor the GPU.
    """
    if not scheduler.acquire_news_lock():
        raise HTTPException(
            status_code=409,
            detail="A news refresh is already running.",
        )
    try:
        return await run_in_threadpool(news_service.refresh)
    finally:
        scheduler.release_news_lock()
