"""Live probe of every registered feed.

Run from backend/:  .venv/bin/python -m tests.test_news_live

Network, by design. This is the script that catches the next ASIC-style
retirement — a feed that quietly stops existing and then logs a warning every
fifteen minutes that nobody reads. Run it before trusting the news corpus.

Reports per feed: HTTP outcome, how many items parsed, whether the feed
supplies a usable **date** (a feed at 100% estimated is silently unsortable)
and whether it supplies a **link** (an article with no URL is a headline
nobody can open).
"""

import httpx

from app.config import settings
from app.services import news_feeds, news_service

from tests.harness import check, report


headers = {"User-Agent": settings.news_user_agent,
           "Accept": "application/rss+xml,application/xml,text/xml,*/*"}

print(f"User-Agent: {settings.news_user_agent}")
if not settings.news_contact_email.strip():
    print("NOTE: NEWS_CONTACT_EMAIL is unset — SEC EDGAR will be skipped.\n")

rows = []
with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
    for spec in news_feeds.FEEDS:
        url = (news_feeds.ticker_feed_url(spec, "AAPL")
               if spec.per_ticker else spec.url)
        result = news_service.fetch_feed(spec, url, client)
        arts = result["articles"]
        dated = sum(1 for a in arts if not a["published_estimated"])
        linked = sum(1 for a in arts if a["url"])
        rows.append((spec, result, arts, dated, linked))

print(f"{'feed':<20} {'status':<12} {'items':>5} {'dated':>7} {'linked':>7}")
print("-" * 56)
for spec, result, arts, dated, linked in rows:
    n = len(arts)
    print(f"{spec.key:<20} {result['status']:<12} {n:>5} "
          f"{(f'{dated}/{n}' if n else '-'):>7} {(f'{linked}/{n}' if n else '-'):>7}")

print()
expected_skip = {"sec_8k"} if not settings.news_contact_email.strip() else set()
for spec, result, arts, dated, linked in rows:
    if spec.key in expected_skip:
        continue
    check(f"{spec.key} responds", result["status"] == "ok",
          f"{result['status']}: {str(result.get('error'))[:70]}")
    if result["status"] != "ok":
        continue
    check(f"{spec.key} yields items", len(arts) > 0)
    if not arts:
        continue
    # A feed with no real dates sorts by fetch time, i.e. arbitrarily.
    check(f"{spec.key} supplies real publication dates",
          dated > 0, f"{dated}/{len(arts)} dated — the rest fall back to fetch time")
    # A feed with no links produces headlines nobody can open.
    check(f"{spec.key} supplies article links", linked > 0, f"{linked}/{len(arts)}")

# Cross-feed invariants worth knowing before trusting the corpus.
all_arts = [a for _, _, arts, _, _ in rows for a in arts]
if all_arts:
    # Within one feed a repeated key would mean the same item listed twice.
    # ACROSS feeds a shared key is the feature working: two SCMP feeds carry
    # the same article at the same URL, and dedup collapses it to one row.
    for spec, _, arts, _, _ in rows:
        keys = [a["dedup_key"] for a in arts]
        check(f"{spec.key} has no duplicate items within itself",
              len(keys) == len(set(keys)),
              f"{len(keys) - len(set(keys))} repeat(s)")

    cross = len(all_arts) - len({a["dedup_key"] for a in all_arts})
    print(f"\n  {cross} article(s) appear in more than one feed and will be "
          f"stored once — that is URL dedup doing its job.")
    check("every stored timestamp uses the exact +00:00 second form",
          all(a["published_at"].endswith("+00:00") and len(a["published_at"]) == 25
              for a in all_arts),
          "SQLite sorts these as TEXT; mixed forms do not sort together")

report("news_live",
       summary="news_live: every registered feed is alive and usable")
