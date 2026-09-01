"""Fetch, normalise, associate and store public news.

Replaces a stub that could not become a product surface. What it got wrong,
recorded here so it does not come back:

1. `entry["link"]` was never read, so **no headline had a URL** — a news list
   nobody can click through.
2. The 15-minute cache stored titles only, so a cache hit rebuilt each item
   with `source` replaced by the literal string `"cache"` and `published`
   gone. A cache that discards fields is worse than no cache.
3. It cached the *limit-truncated* list under one key, so a UI asking for 50
   and the scanner asking for 5 poisoned each other, in whichever order they
   happened to run.
4. `_fetch_feed` mutated the **process-global** `socket.setdefaulttimeout`
   from inside a threadpool worker. `httpx` carries per-request timeouts;
   never reach for the socket default in threaded code.
5. Nothing was ever sorted by publication time, despite a docstring claiming
   "newest feeds first".

Shape now: fetch everything over a small pool holding **no database
connection**, then write once. Sixteen sequential fetches at up to 10s each
inside a write transaction would block the scanner for minutes.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app import db
from app.config import settings
from app.services import news_feeds

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0
MAX_PER_FEED = 40
MAX_WORKERS = 4

# Tracking parameters that fork one story into several dedup keys.
_TRACKING = re.compile(r"^(utm_|guccounter|guce_|ito$|tsrc$|yptr$|ncid$|__twitter)")

# Corporate suffixes stripped before matching a company name in a headline.
_SUFFIXES = re.compile(
    r"[,\s]+(inc\.?|corp\.?|corporation|ltd\.?|limited|plc|holdings?|company|"
    r"co\.?|group|n\.?v\.?|s\.?a\.?|ag|se|class\s+[abc]|adr|reit)\b\.?",
    re.IGNORECASE,
)

# A stripped name this short or this ordinary matches half of every headline.
_MIN_NAME_LEN = 4
_NAME_STOPLIST = {
    "apple", "block", "key", "now", "all", "on", "it", "so", "car", "gap",
    "sun", "well", "open", "next", "first", "one", "core", "match", "peak",
}


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def normalise_url(url: str | None) -> str | None:
    """Strip the parts that vary without changing the article."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING.match(k)]
    query.sort()   # parameter order churn must not fork the key
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                       urlencode(query), ""))


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the read-side key."""
    lowered = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", lowered).strip()


def dedup_key(url: str | None, feed_key: str, title_norm: str) -> str:
    """Stable identity for an article.

    URL when there is one, because it is exact. Otherwise the feed plus the
    normalised title — scoped to the feed rather than global, so two outlets
    running the same headline stay two rows. Collapsing those is a *display*
    decision and happens at query time, where being wrong is recoverable.
    """
    basis = normalise_url(url) or f"{feed_key}|{title_norm}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# Date shapes feedparser does not recognise. Investing.com publishes
# "Aug 24, 2026 06:44 GMT" in <pubDate> — neither RFC-822 nor ISO — so both
# feedparser and parse_iso return None and the whole feed silently falls back
# to fetch time, i.e. becomes unsortable. Worth the two lines.
_LOOSE_DATE_FORMATS = (
    "%b %d, %Y %H:%M %Z",
    "%b %d, %Y %H:%M",
    "%d %b %Y %H:%M:%S %Z",
    "%Y-%m-%d %H:%M:%S",
)


def _parse_loose_date(raw: str) -> datetime | None:
    """Last-resort date parsing. Returns None rather than raising."""
    text = (raw or "").strip()
    if not text:
        return None
    # RFC-822 with an odd offset — the stdlib handles more shapes than strptime.
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(text)
        if parsed:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in _LOOSE_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def parse_published(entry: Any, fetched_at: str) -> tuple[str, bool]:
    """(iso_timestamp, estimated). Always `now_iso()`'s exact shape.

    That exactness is load-bearing: SQLite sorts TEXT lexicographically, so
    `...Z`, `...+00:00` and `....123+00:00` do not sort together and "newest
    first" would silently interleave.
    """
    for attr in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, attr, None)
        if struct:
            try:
                return (datetime(*struct[:6], tzinfo=timezone.utc)
                        .isoformat(timespec="seconds"), False)
            except (TypeError, ValueError):
                pass
    for attr in ("published", "updated"):
        raw = (entry.get(attr) if hasattr(entry, "get") else None) or ""
        parsed = db.parse_iso(raw) or _parse_loose_date(raw)
        if parsed:
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds"), False
    # Substituting fetch time keeps the sort deterministic; the flag keeps it
    # honest, and its rate per feed is a health signal in its own right.
    return fetched_at, True


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_feed(spec: news_feeds.FeedSpec, url: str, client: httpx.Client) -> dict[str, Any]:
    """One feed. Never raises — the outcome is the return value."""
    import feedparser

    if spec.needs_contact_ua and not settings.news_contact_email.strip():
        return {"feed_key": spec.key, "url": url, "status": "http_error",
                "error": "needs NEWS_CONTACT_EMAIL — this source rejects "
                         "requests without a contact address",
                "articles": []}

    fetched_at = db.now_iso()
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        return {"feed_key": spec.key, "url": url, "status": "timeout",
                "error": str(exc), "articles": []}
    except Exception as exc:
        return {"feed_key": spec.key, "url": url, "status": "http_error",
                "error": str(exc), "articles": []}

    parsed = feedparser.parse(response.text)
    entries = getattr(parsed, "entries", []) or []

    # Checked unconditionally, not only when entries is empty: a
    # partially-parsed feed yields rows *and* is a health event.
    bozo_note = None
    if getattr(parsed, "bozo", 0):
        bozo_note = str(getattr(parsed, "bozo_exception", "malformed feed"))[:200]

    if not entries:
        return {"feed_key": spec.key, "url": url,
                "status": "unparseable" if bozo_note else "empty",
                "error": bozo_note, "articles": []}

    articles = []
    for entry in entries[:MAX_PER_FEED]:
        title = re.sub(r"\s+", " ", (entry.get("title") or "")).strip()
        if not title:
            continue
        link = entry.get("link") or None
        title_norm = normalise_title(title)
        published_at, estimated = parse_published(entry, fetched_at)
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "")
        articles.append({
            "dedup_key": dedup_key(link, spec.key, title_norm),
            "url": link,
            "title": title,
            "title_norm": title_norm,
            "summary": re.sub(r"\s+", " ", summary).strip()[:600] or None,
            "feed_key": spec.key,
            "source_label": spec.label,
            "category": spec.category,
            "published_at": published_at,
            "published_estimated": estimated,
            "codes": [],
        })

    return {"feed_key": spec.key, "url": url, "status": "ok",
            "error": bozo_note, "articles": articles}


# --------------------------------------------------------------------------
# Ticker association
# --------------------------------------------------------------------------

def strip_suffixes(name: str) -> str:
    return _SUFFIXES.sub("", name or "").strip(" ,.-")


def name_matches(title_norm: str, company: str) -> bool:
    """Word-boundary match of a company name in a normalised headline.

    Conservative on purpose. The rejected alternative was matching the bare
    symbol — which is how "NOW stock" came to match any headline containing
    the word "now". A wrong association feeds the model someone else's news,
    so the failure mode chosen is "no news" rather than "wrong news".
    """
    stripped = normalise_title(strip_suffixes(company))
    if len(stripped) < _MIN_NAME_LEN or stripped in _NAME_STOPLIST:
        return False
    return re.search(rf"\b{re.escape(stripped)}\b", title_norm) is not None


def associate(articles: list[dict[str, Any]], tickers: list[dict[str, Any]],
              feed_query_code: str | None = None) -> None:
    """Attach ticker links in place.

    `feed_query_code` means these came from that ticker's own feed, so the
    association is definitional rather than inferred.
    """
    for art in articles:
        codes: dict[str, str] = {}
        if feed_query_code:
            codes[feed_query_code] = "feed_query"
        for t in tickers:
            if t["code"] in codes:
                continue
            if name_matches(art["title_norm"], t.get("name") or ""):
                codes[t["code"]] = "company_name"
        art["codes"] = list(codes.items())


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

def _rotation_slice(tickers: list[dict[str, Any]], batch: int,
                    health: dict[str, dict]) -> list[dict[str, Any]]:
    """Least-recently-fetched tickers first.

    45 tickers x 4 refreshes an hour would be 180 requests an hour to one
    host. Rotating a slice gives every ticker roughly hourly coverage — the
    same slice-not-full-pass shape the scanner uses (decisions #15).
    """
    def last_seen(t: dict[str, Any]) -> str:
        h = health.get(f"yahoo_ticker:{t['code']}")
        return (h or {}).get("last_attempt_at") or ""
    return sorted(tickers, key=last_seen)[:batch]


def refresh() -> dict[str, Any]:
    """Fetch every enabled feed, associate, store, prune. Never raises."""
    started = datetime.now(timezone.utc)
    tickers = db.get_enabled_tickers()
    health = {h["feed_key"]: h for h in db.get_feed_health()}

    jobs: list[tuple[news_feeds.FeedSpec, str, str | None]] = [
        (spec, spec.url, None) for spec in news_feeds.shared_feeds()
    ]
    for spec in news_feeds.per_ticker_feeds():
        eligible = [t for t in tickers if t["market"] in spec.markets]
        skipped = [t["code"] for t in tickers if t["market"] not in spec.markets]
        if skipped:
            logger.info("news: %s skips %d non-%s ticker(s) — the symbol "
                        "convention for those markets is unverified",
                        spec.key, len(skipped), "/".join(spec.markets))
        for t in _rotation_slice(eligible, settings.news_ticker_batch, health):
            symbol = t["code"].split(".", 1)[-1]
            jobs.append((spec, news_feeds.ticker_feed_url(spec, symbol), t["code"]))

    results: list[dict[str, Any]] = []
    # No database connection is held across any of this.
    headers = {"User-Agent": settings.news_user_agent,
               "Accept": "application/rss+xml,application/xml,text/xml,*/*"}
    with httpx.Client(timeout=FETCH_TIMEOUT, headers=headers,
                      follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_feed, spec, url, client): (spec, code)
                       for spec, url, code in jobs}
            for future, (spec, code) in futures.items():
                result = future.result()
                associate(result["articles"], tickers, feed_query_code=code)
                # Per-ticker feeds get their own health row per ticker, which
                # is also what drives the rotation ordering above.
                result["feed_key"] = f"{spec.key}:{code}" if code else spec.key
                results.append(result)

    articles = [a for r in results for a in r["articles"]]
    stored = db.insert_news_articles(articles, settings.news_retention_days)
    db.upsert_feed_health([
        {"feed_key": r["feed_key"], "url": r["url"], "status": r["status"],
         "error": r["error"], "articles": len(r["articles"])}
        for r in results
    ])

    failures = {r["feed_key"]: r["error"] for r in results if r["status"] != "ok"}
    summary = {
        "started_at": started.isoformat(timespec="seconds"),
        "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "feeds_ok": sum(1 for r in results if r["status"] == "ok"),
        "feeds_failed": len(failures),
        "articles_seen": len(articles),
        **stored,
        "failures": failures,
    }
    logger.info("news refresh: %s", summary)
    return summary


# --------------------------------------------------------------------------
# Prompt context
# --------------------------------------------------------------------------

def get_thesis_context(code: str, ticker_limit: int = 5, macro_limit: int = 3,
                       max_age_hours: int = 72) -> dict[str, Any]:
    """Stored news for one ticker, plus market context. No network.

    Two separate lists, never merged. The old code padded a short ticker list
    with unrelated market headlines under one flat heading, so the model could
    not tell "IBM beats on cloud" from "Stocks slide as Fed holds".
    """
    since = (datetime.now(timezone.utc)
             - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    now = datetime.now(timezone.utc)

    def age(row: dict[str, Any]) -> float | None:
        when = db.parse_iso(row.get("published_at"))
        return round((now - when).total_seconds() / 3600, 1) if when else None

    ticker_rows = db.get_news_for_codes([code], since, ticker_limit)
    macro_rows = db.get_macro_news(since, macro_limit)
    return {
        "ticker": [{"title": r["title"], "source_label": r["source_label"],
                    "published_at": r["published_at"], "age_hours": age(r),
                    "match_basis": r.get("match_basis")} for r in ticker_rows],
        "macro": [{"title": r["title"], "source_label": r["source_label"],
                   "published_at": r["published_at"], "age_hours": age(r)}
                  for r in macro_rows],
        "window_hours": max_age_hours,
    }
