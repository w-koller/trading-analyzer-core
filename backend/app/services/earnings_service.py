"""Upcoming earnings for watchlist tickers, plus a short AI outlook per event.

Stored rather than fetched live, for four reasons that all point the same way:
it is whole-market data (a 7-day US window is ~300 rows, filtered down to ~45);
a live fetch during a scan queues behind the scan's OpenD calls and raises
GatewayTimeout, so the page would look broken exactly when the app is busiest;
the position alerts read it, and an alert that vanishes on an OpenD hiccup is
not an alert; and it is a *calendar* — it changes at most daily, so refetching
per page load is pure waste. Same shape as the news hub (decisions #39).

AU is not covered and cannot be. Moomoo's earnings calendar rejects it
outright — "Invalid market type, supported: HK/US/CNSH/CNSZ/SG/JP" — despite
its own SDK docstring claiming otherwise. That is reported as a structured
skip, never as a failure, following `market_data.get_movers`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import db
from app.services import (
    ai_thesis, llm_json, llm_slots, news_service, ollama_models, prompt_blocks,
)
from app.services.gateway_errors import GatewayError

logger = logging.getLogger(__name__)

HORIZON_DAYS = 14
MAX_WINDOW_DAYS = 7            # the server rejects anything wider
SUPPORTED_MARKETS = ("US", "HK")

UNSUPPORTED_REASON = {
    "AU": (
        "Moomoo's earnings calendar does not cover AU — the server replies "
        "\"Invalid market type, supported: HK/US/CNSH/CNSZ/SG/JP\". ASX "
        "earnings dates are not available from this source."
    ),
}

_PUB_TYPES = {"BEFORE", "AFTER", "REGULAR"}


def _f(value: Any) -> float | None:
    """Numbers arrive as 'N/A' strings for fields the feed does not carry."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip() or value.strip().upper() in ("N/A", "NA", "-"):
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out       # NaN


def windows(start: date, horizon_days: int = HORIZON_DAYS) -> list[tuple[str, str]]:
    """Split a horizon into inclusive <=7-day chunks.

    Not a convenience: an 8-day request is rejected outright with "Date range
    must not exceed 7 days", so a fortnight genuinely is two calls per market.
    """
    out: list[tuple[str, str]] = []
    day = 0
    while day <= horizon_days:
        begin = start + timedelta(days=day)
        end = start + timedelta(days=min(day + MAX_WINDOW_DAYS - 1, horizon_days))
        out.append((begin.isoformat(), end.isoformat()))
        day += MAX_WINDOW_DAYS
    return out


def _normalise(row: dict[str, Any], code: str, market: str) -> dict[str, Any] | None:
    raw_date = str(row.get("earnings_date") or "").strip()
    if not raw_date:
        return None
    pub = str(row.get("pub_type") or "").strip().upper()
    return {
        "code": code,
        "earnings_date": raw_date[:10],
        "market": market,
        "name": str(row.get("name") or "")[:200],
        "pub_type": pub if pub in _PUB_TYPES else "UNKNOWN",
        "period_text": str(row.get("period_text") or "") or None,
        "eps_predict": _f(row.get("eps_predict")),
        "eps_actual": _f(row.get("eps_actual")),
        "revenue_predict": _f(row.get("revenue_predict")),
        "revenue_actual": _f(row.get("revenue_actual")),
        "iv": _f(row.get("iv")),
        "iv_rank": _f(row.get("iv_rank")),
        "iv_percentile": _f(row.get("iv_percentile")),
        "market_cap": _f(row.get("market_cap")),
        "price_at_fetch": _f(row.get("price")),
    }


def refresh(gateway, horizon_days: int = HORIZON_DAYS) -> dict[str, Any]:
    """Fetch, filter to the enabled watchlist, store. Never raises."""
    started = datetime.now(timezone.utc)
    tickers = db.get_enabled_tickers()

    # Match on the FULL code. Verified live: get_earnings_calendar returns
    # "US.PDD", i.e. exactly watchlist_cache.code. The bare symbol is kept as
    # a fallback because the sibling get_search_news documents the reverse
    # form ("LITE.US"), and a mismatch here does not error — it returns zero
    # rows and renders as "no earnings this fortnight", the same silent-empty
    # failure class as the never-append-Z bug.
    by_code = {t["code"]: t for t in tickers}
    by_symbol = {t["code"].split(".", 1)[-1]: t for t in tickers}

    wanted_markets = {str(t["market"]).upper() for t in tickers}
    skipped: dict[str, str] = {}
    for m in sorted(wanted_markets - set(SUPPORTED_MARKETS)):
        skipped[m] = UNSUPPORTED_REASON.get(
            m, f"Moomoo's earnings calendar does not cover {m}."
        )

    collected: dict[tuple[str, str], dict[str, Any]] = {}
    calls = 0
    for market in sorted(wanted_markets & set(SUPPORTED_MARKETS)):
        for begin, end in windows(started.date(), horizon_days):
            calls += 1
            try:
                rows = gateway.get_earnings_calendar(market, begin, end)
            except GatewayError as exc:
                # One bad window never costs the rest.
                logger.info("earnings: %s %s..%s failed (%s)", market, begin, end, exc)
                skipped.setdefault(f"{market} {begin}..{end}", str(exc))
                continue

            for row in rows:
                sec = str(row.get("security") or "").strip()
                t = by_code.get(sec) or by_symbol.get(sec.split(".", 1)[-1])
                if t is None:
                    continue
                norm = _normalise(row, t["code"], str(t["market"]).upper())
                if norm:
                    collected[(norm["code"], norm["earnings_date"])] = norm

    written = db.upsert_earnings(list(collected.values()))
    result = {
        "started_at": started.isoformat(timespec="seconds"),
        "elapsed_seconds": round(
            (datetime.now(timezone.utc) - started).total_seconds(), 1),
        "markets_queried": sorted(wanted_markets & set(SUPPORTED_MARKETS)),
        "calls": calls,
        "events": len(collected),
        "skipped_markets": skipped,
        **written,
    }
    logger.info("earnings refresh: %s", result)
    return result


# --------------------------------------------------------------------------
# The AI outlook
# --------------------------------------------------------------------------

OUTLOOK_TIMEOUT = 300.0
OUTLOOK_MAX_RETRIES = 3
MAX_PER_RUN = 8
MAX_HEADLINE = 160
MAX_PROSE = 700
SEARCH_NEWS_COUNT = 8

REQUIRED_KEYS = frozenset({"headline", "what_to_watch", "news_summary", "uncertainty"})

OUTLOOK_SYSTEM_PROMPT = """You are a research assistant writing a short \
pre-earnings briefing for ONE company. It is advisory-only: this tool has no \
order path, and you never tell anyone to buy, sell, hold, add or trim.

You are given an earnings date, consensus estimates and recent headlines. Every \
number was retrieved before you saw it — read them as given, never recalculate \
or invent one, and say so plainly when something is not in front of you.

Do NOT score conviction, pick a direction, or suggest a stop or a target. Those \
belong to the trade thesis, which is produced elsewhere and validated against a \
schema. Your job is to say what is coming, what is worth watching, and what \
would make the picture wrong.

Respond with a single JSON object and nothing else, with exactly these keys:
  "headline"      - one sentence, at most 160 characters
  "what_to_watch" - an array of 3 to 5 short strings
  "news_summary"  - two or three sentences on the recent headlines
  "uncertainty"   - one sentence naming what would make this briefing wrong"""


class OutlookError(RuntimeError):
    pass


def validate_outlook(payload: dict[str, Any]) -> dict[str, Any]:
    """Reject rather than coerce — the same stance as `validate_thesis`.

    A DIFFERENT schema from a thesis, on purpose. Rule #2's shape is the
    thesis endpoint's contract, and this is not a thesis: it has no
    conviction_score, trade_direction, suggested_stop or suggested_target,
    because an outlook must not look like one. If it carried a second
    "conviction", an unvalidated opinion would sit next to a validated one
    looking comparable, and the RAG corpus reads the validated one.

    Prose with no schema at all was the alternative and is worse: a response
    that ignored the instructions could not be rejected, and the UI would have
    to render whatever arrived.
    """
    if not isinstance(payload, dict):
        raise OutlookError(f"expected a JSON object, got {type(payload).__name__}")

    missing = REQUIRED_KEYS - payload.keys()
    extra = payload.keys() - REQUIRED_KEYS
    if missing:
        raise OutlookError(f"missing keys: {sorted(missing)}")
    if extra:
        raise OutlookError(f"unexpected keys: {sorted(extra)}")

    headline = payload["headline"]
    if not isinstance(headline, str) or not headline.strip():
        raise OutlookError("headline must be a non-empty string")
    if len(headline) > MAX_HEADLINE:
        raise OutlookError(f"headline is {len(headline)} chars, max {MAX_HEADLINE}")

    watch = payload["what_to_watch"]
    if not isinstance(watch, list) or not all(
        isinstance(w, str) and w.strip() for w in watch
    ):
        raise OutlookError("what_to_watch must be an array of non-empty strings")
    if not 3 <= len(watch) <= 5:
        raise OutlookError(f"what_to_watch has {len(watch)} items, needs 3 to 5")

    for field in ("news_summary", "uncertainty"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise OutlookError(f"{field} must be a non-empty string")
        if len(value) > MAX_PROSE:
            raise OutlookError(f"{field} is {len(value)} chars, max {MAX_PROSE}")

    return {
        "headline": headline.strip(),
        "what_to_watch": [w.strip() for w in watch],
        "news_summary": payload["news_summary"].strip(),
        "uncertainty": payload["uncertainty"].strip(),
    }


def _search_news(gateway, name: str) -> list[dict[str, Any]]:
    """Moomoo's news search. Both empty shapes are 'no results'."""
    if not name:
        return []
    try:
        return gateway.search_news(name, max_count=SEARCH_NEWS_COUNT) or []
    except GatewayError as exc:
        logger.info("earnings: news search for %r returned nothing (%s)", name, exc)
        return []


def build_outlook_prompt(
    event: dict[str, Any],
    setup: dict[str, Any] | None,
    stored_news: dict[str, Any] | None,
    searched: list[dict[str, Any]],
    consensus: dict[str, Any] | None,
) -> str:
    days = _days_until(event["earnings_date"])
    when = {
        "BEFORE": "before the market opens",
        "AFTER": "after the close",
        "REGULAR": "during the session",
    }.get(event.get("pub_type") or "UNKNOWN", "at an unstated time")

    parts = [
        f"COMPANY: {event.get('name') or event['code']} ({event['code']})",
        f"REPORTS: {event['earnings_date']} ({when}), in {days} days"
        + (f" — {event['period_text']}" if event.get("period_text") else ""),
    ]

    est = []
    for label, key in (("EPS estimate", "eps_predict"), ("Revenue estimate", "revenue_predict")):
        if event.get(key) is not None:
            est.append(f"  {label}: {event[key]:,.4g}")
    for label, key in (("Implied volatility", "iv"), ("IV rank", "iv_rank"),
                       ("IV percentile", "iv_percentile")):
        if event.get(key) is not None:
            est.append(f"  {label}: {event[key]:,.2f}")
    parts.append("CONSENSUS AND OPTIONS EXPECTATION:\n"
                 + ("\n".join(est) if est else "  none published for this name"))

    if consensus:
        parts.append(
            "ANALYST CONSENSUS:\n"
            f"  Rating {consensus.get('rating')} across {consensus.get('total')} analysts"
            f" (buy {consensus.get('buy')}%, hold {consensus.get('hold')}%,"
            f" sell {consensus.get('sell')}%)\n"
            f"  Target: low {consensus.get('lowest')}, average {consensus.get('average')},"
            f" high {consensus.get('highest')} — as of {consensus.get('update_time_str')}"
        )

    if setup:
        parts.append(
            "THE TOOL'S OWN LATEST THESIS (context only — do not restate or re-score it):\n"
            f"  {setup['trade_direction']}, conviction {setup['conviction_score']}/10,"
            f" produced {setup['created_at']}\n"
            f"  {setup['reasoning']}"
        )

    if searched:
        # Dates are passed through verbatim and labelled unverified: this feed
        # gives "8/24" with no year and no timezone, so it must never be
        # parsed, sorted on, or presented as a fact about when.
        lines = [
            f"  - ({s.get('source') or 'unknown source'}, feed date"
            f" {s.get('publish_time') or 'unstated'} — unverified) {s.get('title')}"
            for s in searched[:SEARCH_NEWS_COUNT]
            if s.get("title")
        ]
        parts.append("NEWS SEARCH RESULTS (dates as the feed gave them, "
                     "not normalised — do not compute an age from them):\n"
                     + "\n".join(lines))
    else:
        parts.append("NEWS SEARCH RESULTS: none found for this company.")

    ticker_news = (stored_news or {}).get("ticker") or []
    if ticker_news:
        parts.append(
            "STORED HEADLINES FOR THIS TICKER (ages are reliable):\n"
            + "\n".join(
                f"  - ({prompt_blocks.age_label(n)}) {n.get('source_label')}: {n.get('title')}"
                for n in ticker_news
            )
        )

    parts.append("Produce the JSON object now.")
    return "\n\n".join(parts)


def _days_until(earnings_date: str) -> int:
    try:
        d = date.fromisoformat(earnings_date[:10])
    except ValueError:
        return 0
    return (d - datetime.now(timezone.utc).date()).days


def generate_outlook(
    gateway,
    event: dict[str, Any],
    model: str | None = None,
    timeout: float = OUTLOOK_TIMEOUT,
    client: Any = None,
    max_retries: int = OUTLOOK_MAX_RETRIES,
) -> dict[str, Any]:
    """One validated outlook. The caller owns the LLM slot."""
    code = event["code"]
    model = model or ollama_models.active_model()

    setup = db.get_latest_setup_for_code(code)
    try:
        stored_news = news_service.get_thesis_context(code)
    except Exception as exc:                                  # noqa: BLE001
        logger.info("earnings: no stored news for %s (%s)", code, exc)
        stored_news = None
    searched = _search_news(gateway, event.get("name") or "")
    try:
        consensus = gateway.get_analyst_consensus(code) or None
    except GatewayError as exc:
        logger.info("earnings: no analyst consensus for %s (%s)", code, exc)
        consensus = None

    prompt = build_outlook_prompt(event, setup, stored_news, searched, consensus)
    outlook = llm_json.generate_validated_json(
        client if client is not None else llm_json.client(timeout),
        model=model,
        system_prompt=OUTLOOK_SYSTEM_PROMPT,
        user_prompt=prompt,
        # extract_json IS generic — a ```json fence is a formatting artefact,
        # not thesis semantics. validate_thesis is NOT generic and is
        # deliberately not reused here: an outlook has its own schema, and one
        # carrying a conviction_score would read as a second thesis.
        validate=lambda raw: validate_outlook(ai_thesis.extract_json(raw)),
        subject=code,
        label="outlook",
        correction_hint="exactly the four required keys",
        transport_error=OutlookError,
        exhausted_error=OutlookError,
        max_retries=max_retries,
    )

    db.upsert_earnings_outlook(
        code=code, earnings_date=event["earnings_date"], model=model,
        sources={
            "search_news": len(searched),
            "stored_news": len((stored_news or {}).get("ticker") or []),
            "setup_id": setup["id"] if setup else None,
            "consensus": bool(consensus),
        },
        **outlook,
    )
    logger.info("outlook stored for %s (%s) on %s",
                code, event["earnings_date"], model)
    return {"code": code, "earnings_date": event["earnings_date"],
            "model": model, **outlook}


def _needs_outlook(event: dict[str, Any]) -> bool:
    """True when there is no outlook, or news has landed since it was written.

    Cheap enough to run over the whole horizon, and it means the weekly job
    usually does almost nothing rather than re-spending GPU time on briefings
    that have not changed.
    """
    if not event.get("outlook_generated_at"):
        return True
    with db.get_connection() as conn:
        row = conn.execute(
            """
            SELECT MAX(a.published_at) AS newest
            FROM news_articles a
            JOIN news_article_tickers t ON t.article_id = a.id
            WHERE t.code = ?
            """,
            (event["code"],),
        ).fetchone()
    newest = row["newest"] if row else None
    return bool(newest and newest > event["outlook_generated_at"])


def refresh_outlooks(gateway, horizon_days: int = HORIZON_DAYS,
                     limit: int = MAX_PER_RUN) -> dict[str, Any]:
    """Generate outlooks for upcoming reports that need one. Never raises.

    Soonest-first and capped, the slice-not-full-pass shape of decisions #15:
    if the cap bites, this week's reports are the ones that got done.

    Takes ONE llm_slot per outlook with a long acquire timeout, so with
    capacity 2 an interactive chat can always get the other. It does not take
    `_scan_lock` — it makes one OpenD call per ticker, already serialised by
    the gateway's own bounded lock, and holding the scan mutex for sixteen
    minutes of inference would delay a pre-market scan for nothing.
    """
    started = datetime.now(timezone.utc)
    candidates = [e for e in db.get_upcoming_earnings(days_ahead=horizon_days)
                  if _needs_outlook(e)][:limit]

    done, failed = [], {}
    for event in candidates:
        token = llm_slots.acquire(f"outlook {event['code']}",
                                  llm_slots.BACKGROUND_TIMEOUT)
        if token is None:
            failed[event["code"]] = "no LLM slot available"
            continue
        try:
            generate_outlook(gateway, event)
            done.append(event["code"])
        except Exception as exc:                              # noqa: BLE001
            logger.warning("outlook failed for %s: %s", event["code"], exc)
            failed[event["code"]] = str(exc)[:200]
        finally:
            llm_slots.release(token)

    return {
        "started_at": started.isoformat(timespec="seconds"),
        "elapsed_seconds": round(
            (datetime.now(timezone.utc) - started).total_seconds(), 1),
        "candidates": len(candidates),
        "generated": done,
        "failed": failed,
    }
