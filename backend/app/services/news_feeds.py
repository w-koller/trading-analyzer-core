"""The feed registry — which sources exist, and what each one is for.

A hand-edited table rather than configuration. Sixteen feeds each carrying a
category, a display label, an icon key and (for one) a URL template do not fit
`str` env fields, and `config.py`'s documented constraint makes a structured
env var actively hostile: pydantic-settings JSON-decodes complex types
*before* field validators run. A table in source is also something a human can
read and correct, which is the point.

**Categories are static per feed and never inferred.** The three of them are
the reader's three questions — "what just broke", "what is the mood", "what is
moving everything" — not a publisher's taxonomy. Inferring them would mean
either an LLM call (GPU time on a 15-minute job, to produce metadata) or a
keyword heuristic that misfiles things silently. Note `investing_macro` sits
under `themes` on purpose: that is the user's own grouping, and the name is
the publisher's.

Every URL here was probed live on 2026-08-24 and returned 200 with parseable
XML. Two the user asked for are deliberately absent:

  * **ASIC** — its RSS is retired. Every documented URL 404s, and the
    newsroom pages carry no feed links at all.
  * **Motley Fool AU** — returns 403 to non-browser agents, including on
    `robots.txt`. That is an explicit answer, not an obstacle; do not spoof a
    User-Agent to get around it.

`abc_au_business` substitutes for both. It is a judgement call: it stands in
for a regulator (`shocks`) and for retail commentary (`themes`) and is
neither, so it is filed under `macro` as the Australian macro lens. Moving it
is a one-line change here and nothing else.

Also gone: the old Reuters feed. `feeds.reuters.com` no longer resolves in
DNS — worse than the "unparseable content" CLAUDE.md already recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import NewsCategory


@dataclass(frozen=True)
class FeedSpec:
    key: str
    label: str
    url: str
    category: NewsCategory
    icon: str
    per_ticker: bool = False
    # Per-ticker feeds only. Yahoo's symbol convention for non-US listings is
    # not Moomoo's ("HK.00700" is not what Yahoo expects) and is unverified
    # against real data, so those markets are skipped rather than guessed at.
    markets: tuple[str, ...] = ()
    # SEC EDGAR 403s without a User-Agent carrying contact details.
    needs_contact_ua: bool = False


FEEDS: tuple[FeedSpec, ...] = (
    # --- 1. Market shocks: sudden, material, usually regulatory -----------
    FeedSpec("sec_8k", "SEC EDGAR",
             "https://www.sec.gov/cgi-bin/browse-edgar"
             "?action=getcurrent&type=8-K&count=100&output=atom",
             "shocks", "filing", needs_contact_ua=True),
    FeedSpec("federal_reserve", "Federal Reserve",
             "https://www.federalreserve.gov/feeds/press_all.xml",
             "shocks", "bank"),
    FeedSpec("hkex_regulatory", "HKEX Regulatory",
             "https://www.hkex.com.hk/Services/RSS-Feeds/regulatory-announcements?sc_lang=en",
             "shocks", "bank"),
    FeedSpec("hkex_news", "HKEX News",
             "https://www.hkex.com.hk/Services/RSS-Feeds/News-Releases?sc_lang=en",
             "shocks", "bank"),
    FeedSpec("rba", "Reserve Bank of Australia",
             "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
             "shocks", "bank"),

    # --- 2. Vibe and themes: narrative, momentum, sentiment ---------------
    FeedSpec("yahoo_top", "Yahoo Finance",
             "https://finance.yahoo.com/news/rssindex", "themes", "markets"),
    FeedSpec("investing_stocks", "Investing.com",
             "https://www.investing.com/rss/news_25.rss", "themes", "markets"),
    FeedSpec("investing_macro", "Investing.com Macro",
             "https://www.investing.com/rss/market_overview.rss", "themes", "markets"),
    FeedSpec("scmp_china_biz", "SCMP China Business",
             "https://www.scmp.com/rss/92/feed", "themes", "globe"),
    FeedSpec("scmp_hk_econ", "SCMP Hong Kong Economy",
             "https://www.scmp.com/rss/93/feed", "themes", "globe"),
    FeedSpec("seeking_alpha", "Seeking Alpha",
             "https://seekingalpha.com/market_currents.xml", "themes", "markets"),

    # --- 3. Macro and geopolitics: cross-market spillover ------------------
    FeedSpec("cnbc_world", "CNBC World Markets",
             "https://search.cnbc.com/rs/search/combinedcms/view.xml"
             "?partnerId=wrss01&id=10000664", "macro", "globe"),
    FeedSpec("cnbc_us", "CNBC US Markets",
             "https://search.cnbc.com/rs/search/combinedcms/view.xml"
             "?partnerId=wrss01&id=15837362", "macro", "globe"),
    FeedSpec("marketwatch", "MarketWatch",
             "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
             "macro", "news"),
    FeedSpec("ft_global_economy", "Financial Times",
             "https://www.ft.com/global-economy?format=rss", "macro", "news"),
    FeedSpec("abc_au_business", "ABC Australia Business",
             "https://www.abc.net.au/news/feed/51892/rss.xml", "macro", "news"),

    # --- Per-ticker ---------------------------------------------------------
    # The only ticker-scoped source. Replaces the old Google News query
    # `f"{symbol} stock"`, which had no market scoping and no company-name
    # disambiguation — "NOW stock" matched any headline containing "now", and
    # the same trap sits on ALL, IT, ON, KEY, CAR and SO.
    FeedSpec("yahoo_ticker", "Yahoo Finance",
             "https://feeds.finance.yahoo.com/rss/2.0/headline"
             "?s={symbol}&region=US&lang=en-US",
             "themes", "markets", per_ticker=True, markets=("US",)),
)

CATEGORY_LABELS: dict[str, str] = {
    "shocks": "Market shocks",
    "themes": "Vibe & themes",
    "macro": "Macro & geopolitics",
}


def by_key(key: str) -> FeedSpec | None:
    return next((f for f in FEEDS if f.key == key), None)


def _disabled() -> set[str]:
    from app.config import settings
    return set(settings.news_feeds_disabled_list)


def shared_feeds() -> list[FeedSpec]:
    """Feeds fetched once per refresh, regardless of the watchlist."""
    off = _disabled()
    return [f for f in FEEDS if not f.per_ticker and f.key not in off]


def per_ticker_feeds() -> list[FeedSpec]:
    off = _disabled()
    return [f for f in FEEDS if f.per_ticker and f.key not in off]


def ticker_feed_url(spec: FeedSpec, symbol: str) -> str:
    return spec.url.format(symbol=symbol)
