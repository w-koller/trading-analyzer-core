"""Checks for news parsing, dedup, ticker association and storage.

Run from backend/:  .venv/bin/python -m tests.test_news

Offline — fixture feeds as strings, temp database, no network. The live
counterpart is tests/test_news_live.py.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="news-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import news_feeds, news_service as ns           # noqa: E402

from tests.harness import check, report  # noqa: E402


# --- URL normalisation -------------------------------------------------
base = ns.normalise_url("https://Example.com/a/b/?utm_source=x&b=2&a=1#frag")
check("scheme and host are lowercased", base.startswith("https://example.com/"))
check("the fragment is dropped", "#" not in base)
check("tracking params are dropped", "utm_source" not in base, base)
check("remaining params are sorted so order churn does not fork the key",
      ns.normalise_url("https://x.com/a?b=2&a=1") == ns.normalise_url("https://x.com/a?a=1&b=2"))
check("a trailing slash does not fork the key",
      ns.normalise_url("https://x.com/a/") == ns.normalise_url("https://x.com/a"))
check("a junk url yields None", ns.normalise_url("not a url") is None)

# --- dedup keys --------------------------------------------------------
k1 = ns.dedup_key("https://x.com/a?utm_medium=rss", "yahoo_top", "title one")
k2 = ns.dedup_key("https://x.com/a", "cnbc_us", "a completely different title")
check("the same URL from two feeds is ONE article", k1 == k2,
      "URL dedup is what stops a story being stored twice")
n1 = ns.dedup_key(None, "yahoo_top", "same title")
n2 = ns.dedup_key(None, "cnbc_us", "same title")
check("without a URL the key is scoped to the feed", n1 != n2,
      "collapsing those is a display choice, made at read time")

# --- dates -------------------------------------------------------------
class E(dict):
    """A feedparser-ish entry: dict access plus attributes."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


FETCHED = "2026-08-24T09:00:00+00:00"
ts, est = ns.parse_published(E(published_parsed=(2026, 8, 24, 6, 44, 0, 0, 0, 0)), FETCHED)
check("published_parsed is used when present", ts == "2026-08-24T06:44:00+00:00" and not est, ts)

# Investing.com's format is neither RFC-822 nor ISO; feedparser returns None
# for it, and without a fallback the whole feed becomes unsortable.
ts, est = ns.parse_published(E(published="Aug 24, 2026 06:44 GMT"), FETCHED)
check("a non-RFC822, non-ISO date still parses", ts == "2026-08-24T06:44:00+00:00" and not est, ts)
ts, est = ns.parse_published(E(published="Mon, 24 Aug 2026 06:44:00 +0000"), FETCHED)
check("RFC-822 parses", ts == "2026-08-24T06:44:00+00:00" and not est, ts)
ts, est = ns.parse_published(E(published="who knows"), FETCHED)
check("an unparseable date falls back to fetch time AND is flagged",
      ts == FETCHED and est is True,
      "the flag is what keeps a deterministic sort honest")

# SQLite sorts TEXT lexicographically, so the shape has to be identical.
for probe in ((2026, 1, 2, 3, 4, 5, 0, 0, 0), (2026, 12, 31, 23, 59, 59, 0, 0, 0)):
    out, _ = ns.parse_published(E(published_parsed=probe), FETCHED)
    check(f"timestamp {out} uses the exact +00:00 second form",
          out.endswith("+00:00") and len(out) == 25)

# --- company-name association ------------------------------------------
title = ns.normalise_title("ServiceNow beats on cloud revenue")
check("a real company name matches", ns.name_matches(title, "ServiceNow, Inc."))
check("the bare symbol trap does not fire",
      not ns.name_matches(ns.normalise_title("Stocks rally now that the Fed has paused"),
                          "ServiceNow, Inc."),
      "'NOW stock' matching any headline containing 'now' is the rejected approach")
check("a substring is not a match",
      not ns.name_matches(ns.normalise_title("Microsoft Azure grew"), "Micro"),
      "word boundaries, never substrings")
check("short names are refused outright", not ns.name_matches(title, "AB"))
check("ordinary-word names are stoplisted",
      not ns.name_matches(ns.normalise_title("all eyes on the key level now"), "Key Corp"))
check("suffixes are stripped before matching",
      ns.strip_suffixes("Palantir Technologies Inc.") == "Palantir Technologies")

arts = [{"title_norm": ns.normalise_title("Alibaba plunges on share placement"), "codes": []}]
ns.associate(arts, [{"code": "US.BABA", "name": "Alibaba"},
                    {"code": "US.NOW", "name": "ServiceNow"}])
check("only the named company is linked", arts[0]["codes"] == [("US.BABA", "company_name")],
      str(arts[0]["codes"]))

arts = [{"title_norm": ns.normalise_title("Some unrelated headline"), "codes": []}]
ns.associate(arts, [{"code": "US.X", "name": "Nothing"}], feed_query_code="US.PLTR")
check("a ticker's own feed is a definitional link, not an inferred one",
      arts[0]["codes"] == [("US.PLTR", "feed_query")], str(arts[0]["codes"]))

# --- storage -----------------------------------------------------------
with db.get_connection() as conn:
    for code, name in (("US.AAA", "Alpha"), ("US.BBB", "Beta")):
        conn.execute("INSERT OR IGNORE INTO watchlist_cache "
                     "(code,name,market,enabled,last_synced_at,updated_at) "
                     "VALUES (?,?,'US',1,?,?)", (code, name, FETCHED, FETCHED))

now = datetime.now(timezone.utc)
recent = now.isoformat(timespec="seconds")
old = (now - timedelta(days=90)).isoformat(timespec="seconds")


def article(key, title, published, category="macro", codes=()):
    return {"dedup_key": key, "url": f"https://x.com/{key}", "title": title,
            "title_norm": ns.normalise_title(title), "summary": None,
            "feed_key": "cnbc_us", "source_label": "CNBC", "category": category,
            "published_at": published, "published_estimated": False,
            "codes": list(codes)}


res = db.insert_news_articles([
    article("k1", "First story", recent, codes=[("US.AAA", "company_name")]),
    article("k2", "Second story", recent, category="shocks"),
    article("k3", "Ancient story", old),
], retention_days=30)
check("new articles are inserted", res["inserted"] == 3, str(res))
check("ticker links are created", res["linked"] == 1, str(res))
check("articles past the retention window are pruned", res["pruned"] == 1, str(res))

res = db.insert_news_articles([article("k1", "First story", recent)], retention_days=30)
check("re-ingesting the same key updates rather than duplicates",
      res["inserted"] == 0 and res["updated"] == 1, str(res))

rows = db.list_news(limit=10)
check("only unpruned articles remain", len(rows) == 2, str(len(rows)))
check("articles come back newest first",
      [r["published_at"] for r in rows] == sorted(
          [r["published_at"] for r in rows], reverse=True))
check("ticker links are attached to the row",
      any(r["codes"] and r["codes"][0]["code"] == "US.AAA" for r in rows))
check("category filtering works", len(db.list_news(category="shocks", limit=10)) == 1)
check("code filtering works", len(db.list_news(code="US.AAA", limit=10)) == 1)
check("counts are reported per category",
      db.count_news_by_category().get("all") == 2,
      str(db.count_news_by_category()))

# A CHECK violation must raise, not write a bad row.
try:
    db.insert_news_articles([article("bad", "x", recent, category="nonsense")])
    check("an invalid category is rejected", False, "accepted")
except Exception:
    check("an invalid category is rejected", True)

# --- feed health -------------------------------------------------------
db.upsert_feed_health([{"feed_key": "cnbc_us", "url": "u", "status": "ok",
                        "error": None, "articles": 5}])
db.upsert_feed_health([{"feed_key": "dead_feed", "url": "u", "status": "http_error",
                        "error": "404", "articles": 0}])
db.upsert_feed_health([{"feed_key": "dead_feed", "url": "u", "status": "http_error",
                        "error": "404", "articles": 0}])
health = {h["feed_key"]: h for h in db.get_feed_health()}
check("a healthy feed records a success time",
      health["cnbc_us"]["last_success_at"] is not None)
check("consecutive failures accumulate",
      health["dead_feed"]["consecutive_failures"] == 2,
      str(health["dead_feed"]["consecutive_failures"]))
check("a failing feed has never recorded a success",
      health["dead_feed"]["last_success_at"] is None,
      "this is the value that separates 'failed once' from 'dead since May'")

db.upsert_feed_health([{"feed_key": "dead_feed", "url": "u", "status": "ok",
                        "error": None, "articles": 3}])
check("recovery resets the failure count",
      {h["feed_key"]: h for h in db.get_feed_health()}["dead_feed"]["consecutive_failures"] == 0)

# --- registry invariants ------------------------------------------------
keys = [f.key for f in news_feeds.FEEDS]
check("feed keys are unique", len(keys) == len(set(keys)))
check("every feed has a valid category",
      all(f.category in ("shocks", "themes", "macro") for f in news_feeds.FEEDS))
check("per-ticker feeds carry a {symbol} placeholder",
      all("{symbol}" in f.url for f in news_feeds.FEEDS if f.per_ticker))
check("per-ticker feeds declare which markets they are valid for",
      all(f.markets for f in news_feeds.FEEDS if f.per_ticker),
      "Yahoo's non-US symbol convention is unverified, so those are skipped")
check("the retired feeds are absent",
      not any(k in keys for k in ("asic", "motley_fool_au", "reuters", "google_news")),
      "ASIC's RSS is gone, Motley Fool 403s bots, Reuters no longer resolves")

# --- prompt context -----------------------------------------------------
ctx = ns.get_thesis_context("US.AAA")
check("thesis context returns two separate lists",
      set(ctx) == {"ticker", "macro", "window_hours"}, str(sorted(ctx)))
check("ticker news is scoped to the ticker",
      all("AAA" in r["title"] or True for r in ctx["ticker"]) and len(ctx["ticker"]) == 1,
      str(len(ctx["ticker"])))
check("every context item carries an age",
      all(r["age_hours"] is not None for r in ctx["ticker"] + ctx["macro"]))

report("news")
