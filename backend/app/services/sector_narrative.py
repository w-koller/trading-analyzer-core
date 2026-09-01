"""The qualitative layer over sector rotation: what the news says about it.

WHERE THE LINE IS, AND WHY IT IS DRAWN HERE

The original request asked for an LLM-produced daily "Rotation Score". Rule #1
forbids asking a model to compute a number, and `alerts.py` (decisions #53)
had already written down the shape the answer takes:

    "If a narrative is ever wanted, the shape is a 'brief me' button handing
     the already-computed list to the model."

That is exactly this module. **The score is computed in Python before the
model is called, the model never sees it before it exists, cannot adjust it,
and cannot reorder the board.** What it produces is a differently-named,
differently-shaped artefact stored in its own table: a candidate explanation
for a move the arithmetic already found.

A rotation score that came back different on two runs over identical data
would be worse than no score. A narrative that reads differently on two runs
is simply prose, and is allowed to.

THE SCHEMA CARRIES NO NUMBER, DELIBERATELY

Five keys, none numeric. `confidence_label` is three fixed words rather than a
1-10 rating for the reason decisions #52 gives for keeping conviction out of
the earnings outlook: a number would be averaged, plotted, and eventually set
beside a `conviction_score` looking comparable to it. Three words cannot be
averaged.

THE RULE THAT MATTERS MOST

`supporting_headlines` must contain titles that were **verbatim members of the
set supplied in the prompt**. That makes it structurally impossible for the
model to cite an article that does not exist — the failure mode that would do
the most damage here, because a fabricated headline is exactly the kind of
detail a reader would trust without checking. A non-member is a rejection, and
the offending string goes back to the model in the correction turn.

WHAT THE NEWS SIDE CAN AND CANNOT SEE

Ticker-linked articles reach a sector through its constituents, and
`news_article_tickers` links articles only to WATCHLIST tickers (decisions
#42 — association is definitional or exact company-name only, never a bare
symbol). So a sector holding none of the user's names has no ticker news, gets
macro only, and should correctly answer "no news explains it" rather than
being handed unrelated headlines to rationalise over.

**Not built, and not to be faked: semantic drift in analyst language.** The
request asked to detect analysts moving from "GPU capacity" to "software
monetization". There is no analyst commentary stream here —
`get_analyst_consensus` is a bare dict snapshot with no history, there are no
transcripts, and there is no upgrade feed. Measuring drift needs the same
language sampled at two times, and this project has one time. A keyword
counter over headlines would look like the feature and measure something else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db
from app.services import (ai_thesis, llm_json, llm_slots, ollama_models,
                          prompt_blocks)

logger = logging.getLogger(__name__)

NARRATIVE_TIMEOUT = 300.0
NARRATIVE_MAX_RETRIES = 3

MAX_HEADLINE = 160
MAX_PROSE = 600
MIN_DRIVER_SENTENCES = 2
MAX_DRIVER_SENTENCES = 3
MAX_SUPPORTING = 5

#: How far back the news window reaches. Matches the 5-session default window
#: the narrative describes, plus slack for a weekend.
NEWS_WINDOW_HOURS = 9 * 24
MAX_TICKER_NEWS = 12
MAX_MACRO_NEWS = 4

#: The window a narrative describes. One, not four: the same sector narrated
#: at four horizons would produce four confident and mutually inconsistent
#: stories about the same corpus, and a reader has no way to pick between
#: them. Five sessions is the dashboard default and the one the board opens on.
NARRATIVE_WINDOW = 5

#: How many sectors get a narrative per run — the strongest movers each way.
#: Bounded because each one costs 30-120s of GPU, and a narrative for the
#: 137th-ranked sector is not something anyone will read.
TOP_N_EACH_WAY = 3

CONFIDENCE_LABELS = (
    "news explains it",
    "news is consistent",
    "no news explains it",
)

REQUIRED_KEYS = frozenset(
    {"headline", "candidate_driver", "supporting_headlines", "contradicts",
     "confidence_label"}
)

SYSTEM_PROMPT = """You are a market analyst writing one short note about a \
single sector's recent price and volume behaviour.

A ROTATION SCORE HAS ALREADY BEEN COMPUTED for this sector, deterministically, \
from price and volume. You are not being asked to score anything, to agree or \
disagree with it, or to say whether the sector is a buy. You are being asked \
ONE question: does the recent news explain the move, or not?

"Not" is a completely acceptable answer and is often the correct one. Sectors \
move for reasons that never reach a headline. Do not invent a driver to fill \
the field.

You may cite ONLY the headlines given to you, quoted EXACTLY as they appear. \
Do not paraphrase them, do not merge two into one, and never mention an \
article that is not in the list. If none of them bear on the move, cite none.

Do not name a price target, a stop, a conviction score, or any number that \
looks like a rating.

Respond with a single JSON object and nothing else, with exactly these keys:
  "headline"             - one sentence, at most 160 characters
  "candidate_driver"     - two or three sentences on what may explain the move
  "supporting_headlines" - an array of 0 to 5 headlines, copied EXACTLY from
                           the list you were given
  "contradicts"          - one sentence naming what in the news cuts the other
                           way, or says nothing does
  "confidence_label"     - exactly one of: "news explains it",
                           "news is consistent", "no news explains it\""""


class NarrativeError(RuntimeError):
    """The model could not be reached, or would not produce a valid object."""


class NarrativeValidationError(NarrativeError):
    """The response was reached but rejected. Worth another attempt."""


def validate_narrative(
    payload: dict[str, Any], allowed_titles: set[str]
) -> dict[str, Any]:
    """Reject rather than coerce, the stance `validate_thesis` set.

    `allowed_titles` is the set actually put in front of the model. Every
    cited headline must be a member — this is what makes fabricating a source
    impossible rather than merely discouraged, and it is the single most
    valuable rule in the schema.

    A DIFFERENT schema from both the thesis and the outlook, on purpose, and
    it must never gain a numeric field: the rotation score beside it is
    computed in Python, and a model-authored number next to it would look
    comparable when it is nothing of the kind (decisions #52).
    """
    if not isinstance(payload, dict):
        raise NarrativeValidationError(
            f"expected a JSON object, got {type(payload).__name__}"
        )

    missing = REQUIRED_KEYS - payload.keys()
    extra = payload.keys() - REQUIRED_KEYS
    if missing:
        raise NarrativeValidationError(f"missing keys: {sorted(missing)}")
    if extra:
        raise NarrativeValidationError(
            f"unexpected keys: {sorted(extra)} — this schema carries no score, "
            "no rating and no price level"
        )

    headline = payload["headline"]
    if not isinstance(headline, str) or not headline.strip():
        raise NarrativeValidationError("headline must be a non-empty string")
    if len(headline) > MAX_HEADLINE:
        raise NarrativeValidationError(
            f"headline is {len(headline)} chars, max {MAX_HEADLINE}"
        )

    driver = payload["candidate_driver"]
    if not isinstance(driver, str) or not driver.strip():
        raise NarrativeValidationError("candidate_driver must be a non-empty string")
    if len(driver) > MAX_PROSE:
        raise NarrativeValidationError(
            f"candidate_driver is {len(driver)} chars, max {MAX_PROSE}"
        )
    sentences = ai_thesis.count_sentences(driver)
    if not MIN_DRIVER_SENTENCES <= sentences <= MAX_DRIVER_SENTENCES:
        raise NarrativeValidationError(
            f"candidate_driver has {sentences} sentences, needs "
            f"{MIN_DRIVER_SENTENCES} or {MAX_DRIVER_SENTENCES}"
        )

    cited = payload["supporting_headlines"]
    if not isinstance(cited, list):
        raise NarrativeValidationError("supporting_headlines must be an array")
    if len(cited) > MAX_SUPPORTING:
        raise NarrativeValidationError(
            f"supporting_headlines has {len(cited)} items, max {MAX_SUPPORTING}"
        )
    clean_cited: list[str] = []
    for item in cited:
        if not isinstance(item, str) or not item.strip():
            raise NarrativeValidationError(
                "supporting_headlines must contain non-empty strings"
            )
        title = item.strip()
        if title not in allowed_titles:
            # The offending string goes back to the model verbatim, so the
            # correction turn can actually act on it.
            raise NarrativeValidationError(
                f"supporting_headlines contains {title!r}, which was not in the "
                "headlines you were given — cite only those, copied exactly"
            )
        clean_cited.append(title)

    contradicts = payload["contradicts"]
    if not isinstance(contradicts, str) or not contradicts.strip():
        raise NarrativeValidationError("contradicts must be a non-empty string")
    if len(contradicts) > MAX_PROSE:
        raise NarrativeValidationError(
            f"contradicts is {len(contradicts)} chars, max {MAX_PROSE}"
        )

    label = payload["confidence_label"]
    # Case-sensitive, the decisions #14 precedent: "News Explains It" is a
    # different string and coercing it teaches the model the rule is soft.
    if label not in CONFIDENCE_LABELS:
        raise NarrativeValidationError(
            f"confidence_label must be exactly one of {list(CONFIDENCE_LABELS)}, "
            f"got {label!r}"
        )

    # A model that cites nothing cannot also claim the news explains the move.
    if not clean_cited and label == "news explains it":
        raise NarrativeValidationError(
            "confidence_label says 'news explains it' but no supporting "
            "headlines were cited — cite the headlines, or say "
            "'no news explains it'"
        )

    return {
        "headline": headline.strip(),
        "candidate_driver": driver.strip(),
        "supporting_headlines": clean_cited,
        "contradicts": contradicts.strip(),
        "confidence_label": label,
    }


# --- the evidence put in front of the model --------------------------------


def _since_iso(hours: int) -> str:
    """`now_iso()`'s exact shape, because news_articles.published_at sorts
    lexicographically as TEXT (decisions #43)."""
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")


def _age_hours(published_at: str | None) -> float | None:
    when = db.parse_iso(published_at)
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def gather_news(plate_code: str, window_hours: int = NEWS_WINDOW_HOURS) -> dict[str, Any]:
    """Articles that could bear on this sector, plus market-wide context.

    Ticker news reaches a sector through its constituents, and only watchlist
    constituents carry article links at all (decisions #42). A sector holding
    none of the user's names therefore gets an EMPTY ticker list rather than a
    padded one — the prompt says so, and the model is expected to answer "no
    news explains it".
    """
    since = _since_iso(window_hours)
    codes = db.watchlist_codes_in_plate(plate_code)
    constituent_count = len(db.get_plate_members(plate_code))

    ticker: list[dict[str, Any]] = []
    covered: set[str] = set()
    if codes:
        # `get_news_for_codes` is DISTINCT over (article, match_basis), so an
        # article linked to a ticker by BOTH bases comes back twice. Observed
        # live: the same Chevron headline appeared twice in one prompt, and a
        # repeated headline reads as a second, corroborating source.
        seen_titles: set[str] = set()
        for row in db.get_news_for_codes(codes, since, limit=MAX_TICKER_NEWS * 3):
            title = row["title"]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            ticker.append(
                {
                    "title": title,
                    "source_label": row["source_label"],
                    "published_at": row["published_at"],
                    "age_hours": _age_hours(row["published_at"]),
                    "match_basis": row.get("match_basis"),
                }
            )
            if len(ticker) >= MAX_TICKER_NEWS:
                break
        # Which of the sector's own names the surviving articles speak for.
        # One query, not one per constituent. Deliberately asked of the whole
        # corpus rather than read off `ticker` above: that list is capped and
        # title-deduplicated, so a name whose only article lost the cut would
        # silently drop out and understate the coverage the prompt reports.
        covered = db.codes_with_news(codes, since)

    # An article can be BOTH ticker-linked and macro-category ('shocks'
    # qualifies for each), and showing it under two headings would put the
    # same headline in front of the model twice — reading as two independent
    # pieces of evidence corroborating one another, which is precisely how a
    # confidence label gets inflated. Ticker news wins: it is the more
    # specific claim.
    seen = {item["title"] for item in ticker}
    macro: list[dict[str, Any]] = []
    for row in db.get_macro_news(since, limit=MAX_MACRO_NEWS + len(seen)):
        if row["title"] in seen:
            continue
        if len(macro) >= MAX_MACRO_NEWS:
            break
        macro.append(
            {
                "title": row["title"],
                "source_label": row["source_label"],
                "published_at": row["published_at"],
                "age_hours": _age_hours(row["published_at"]),
            }
        )

    return {
        "ticker": ticker,
        "macro": macro,
        "watchlist_codes": codes,
        # How much of the SECTOR this news actually speaks for. Observed live:
        # a 14-constituent safe-haven plate produced 12 headlines, every one
        # about Chevron, because CVX was its only watchlist name. The model
        # saw through it that time; it should not have to.
        "covered_codes": sorted(covered),
        "constituent_count": constituent_count,
        "window_hours": window_hours,
    }


def allowed_titles(news: dict[str, Any]) -> set[str]:
    """Exactly what the model may cite. The validator's whitelist."""
    return {
        item["title"].strip()
        for item in list(news.get("ticker") or []) + list(news.get("macro") or [])
        if item.get("title")
    }


def _news_block(news: dict[str, Any], plate_name: str) -> str:
    """Two separately LABELLED lists, never merged, every item dated.

    Same reasoning as decisions #46: a flat undated block let the model read
    "Stocks slide as Fed holds" as though it were news about this sector, and
    "RECENT" was unverified in a prompt whose freshness line exists precisely
    because the model has no reliable sense of today's date.
    """
    lines: list[str] = []
    hours = news.get("window_hours", NEWS_WINDOW_HOURS)

    lines.append(f"HEADLINES ABOUT COMPANIES IN {plate_name.upper()}:")
    ticker = news.get("ticker") or []
    if not ticker:
        codes = news.get("watchlist_codes") or []
        if codes:
            lines.append(f"  (none in the last {hours // 24} days)")
        else:
            # The honest reason, not a shrug: this sector holds none of the
            # tickers the news index covers, so silence here is structural
            # rather than evidence that nothing happened.
            lines.append(
                "  (none — no company in this sector is on the watchlist, so "
                "no company-level news is indexed for it)"
            )
    else:
        covered = news.get("covered_codes") or []
        total = news.get("constituent_count") or 0
        if covered and total:
            # Load-bearing, not a nicety: 12 headlines about 1 of 14
            # constituents is a completely different weight of evidence from
            # 12 across 10 of 14, and the model cannot tell from the titles.
            names = ", ".join(c.split(".")[-1] for c in covered)
            caveat = (
                "; they are evidence about those names, not the whole sector"
                if len(covered) * 3 < total
                else ""
            )
            lines.append(
                f"  (covering {len(covered)} of this sector's {total} "
                f"constituents — {names}{caveat})"
            )
        for item in ticker:
            lines.append(
                f"  - \"{item['title']}\" ({item['source_label']}, "
                f"{prompt_blocks.age_label(item)})"
            )

    lines.append("")
    lines.append("MARKET-WIDE HEADLINES (context, not about this sector):")
    macro = news.get("macro") or []
    if not macro:
        lines.append(f"  (none in the last {hours // 24} days)")
    else:
        for item in macro:
            lines.append(
                f"  - \"{item['title']}\" ({item['source_label']}, "
                f"{prompt_blocks.age_label(item)})"
            )
    return "\n".join(lines)


def _score_block(score: dict[str, Any]) -> str:
    """The already-computed reading, stated as fact for the model to explain.

    Every number here is presented as settled. The model is not asked whether
    it agrees, and the prompt never invites it to produce one of its own.
    """
    parts = [
        f"  Rotation score            : {score.get('score'):+.2f} "
        "(range -1 to +1; computed from price and volume, not by you)",
        f"  Move vs the median sector : {score.get('rel_return_pct'):+.2f}% "
        f"over {score.get('window_days')} trading sessions",
    ]
    thrust = score.get("turnover_thrust")
    if thrust is not None:
        louder = "heavier" if thrust > 0 else "lighter"
        parts.append(
            f"  Dollar volume             : {louder} than this sector's own "
            f"recent normal (thrust {thrust:+.2f})"
        )
    breadth = score.get("breadth")
    if breadth is not None:
        parts.append(
            f"  Breadth                   : {breadth:+.2f} "
            "(advancers minus decliners, over the total)"
        )
    persistence = score.get("persistence")
    if persistence is not None:
        spread = (
            "spread across the window's sessions"
            if persistence > 0.3
            else "concentrated in very few sessions"
            if persistence < -0.3
            else "mixed across the window's sessions"
        )
        parts.append(f"  Persistence               : {persistence:+.2f} — {spread}")
    n = score.get("constituents")
    parts.append(
        f"  Constituents              : {n if n else 'not yet counted'}"
    )
    if not score.get("sufficient"):
        parts.append(
            "  NOTE: this reading is marked UNCONFIRMED — say so if you rely on it."
        )
    return "\n".join(parts)


def build_prompt(
    plate: dict[str, Any], score: dict[str, Any], news: dict[str, Any]
) -> str:
    name = plate.get("plate_name") or plate["plate_code"]
    kind = "industry" if plate.get("plate_class") == "INDUSTRY" else "theme"
    direction = "OUTPERFORMED" if (score.get("score") or 0) > 0 else "UNDERPERFORMED"
    return f"""SECTOR: {name} ({kind}, {plate['plate_code']})
AS OF: {score.get('as_of_date')}

This sector {direction} the median sector over the window below. The reading \
is already computed:

{_score_block(score)}

{_news_block(news, name)}

Does the news above explain this move?

Weigh it honestly. A sector can move on flows, positioning or a rotation out \
of somewhere else, with nothing in the headlines — if that is what you see, \
say "no news explains it" and say so in the driver too. If the headlines are \
merely consistent with the move without explaining it, say "news is \
consistent". Reserve "news explains it" for when you can point at specific \
headlines, and cite them.

Produce the JSON object now."""


# --- generation ------------------------------------------------------------


def generate_narrative(
    plate: dict[str, Any],
    score: dict[str, Any],
    *,
    model: str | None = None,
    timeout: float = NARRATIVE_TIMEOUT,
    client: Any = None,
    max_retries: int = NARRATIVE_MAX_RETRIES,
    persist: bool = True,
) -> dict[str, Any]:
    """One validated narrative for one sector. The caller owns the LLM slot."""
    plate_code = plate["plate_code"]
    # Resolved ONCE, above the retry loop: a correction turn hands the model
    # its own bad output, which is meaningless if a different model made it
    # (decisions #38).
    model = model or ollama_models.active_model()

    news = gather_news(plate_code)
    titles = allowed_titles(news)
    prompt = build_prompt(plate, score, news)

    narrative = llm_json.generate_validated_json(
        client if client is not None else llm_json.client(timeout),
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        # extract_json IS generic — a ```json fence is a formatting artefact.
        # validate_thesis and validate_outlook are NOT, and neither is reused:
        # this artefact must not be able to carry a score.
        validate=lambda raw: validate_narrative(ai_thesis.extract_json(raw), titles),
        subject=plate_code,
        label="sector narrative",
        correction_hint="exactly the five required keys",
        transport_error=NarrativeError,
        exhausted_error=NarrativeValidationError,
        retry_on=NarrativeValidationError,
        max_retries=max_retries,
    )

    if persist:
        db.upsert_sector_narrative(
            plate_code=plate_code,
            as_of_date=score["as_of_date"],
            window_days=int(score["window_days"]),
            model=model,
            sources={
                "ticker_news": len(news["ticker"]),
                "macro_news": len(news["macro"]),
                "watchlist_codes": len(news["watchlist_codes"]),
                "window_hours": news["window_hours"],
                "score": score.get("score"),
                "sufficient": bool(score.get("sufficient")),
            },
            **narrative,
        )
    logger.info(
        "sector narrative for %s (%s): %s",
        plate_code, model, narrative["confidence_label"],
    )
    return {
        "plate_code": plate_code,
        "plate_name": plate.get("plate_name"),
        "as_of_date": score["as_of_date"],
        "window_days": score["window_days"],
        "model": model,
        **narrative,
    }


def select_sectors(
    market: str = "US",
    window_days: int = NARRATIVE_WINDOW,
    top_n: int = TOP_N_EACH_WAY,
) -> list[dict[str, Any]]:
    """The strongest movers each way that do not already have a narrative.

    Bounded rather than watchlist-wide: each narrative costs 30-120s of GPU,
    and nobody reads a story about the 137th-ranked sector. Ordered so the
    biggest movers are done first, in case the run is cut short.
    """
    board = db.get_rotation_board(market=market, window_days=window_days)
    scored = [r for r in board if r.get("score") is not None]
    if not scored:
        return []
    as_of = scored[0]["as_of_date"]
    done = db.get_narrated_plates(as_of, window_days)
    picks = scored[:top_n] + scored[-top_n:]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(picks, key=lambda r: abs(r["score"]), reverse=True):
        code = row["plate_code"]
        if code in seen or code in done:
            continue
        seen.add(code)
        out.append(row)
    return out


def refresh_narratives(
    market: str = "US",
    window_days: int = NARRATIVE_WINDOW,
    top_n: int = TOP_N_EACH_WAY,
    model: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Write narratives for the day's strongest movers.

    Takes ONE llm_slot per narrative with a long acquire, so a batch job never
    holds both slots and an interactive chat can always get the other
    (decisions #50). Never raises: one sector the model will not describe must
    not stop the rest.
    """
    result: dict[str, Any] = {
        "market": market,
        "window_days": window_days,
        "considered": 0,
        "written": 0,
        "skipped_no_slot": 0,
        "failures": [],
    }
    targets = select_sectors(market, window_days, top_n)
    result["considered"] = len(targets)
    if not targets:
        return result

    universe = {p["plate_code"]: p for p in db.get_sector_universe(market=market)}
    for row in targets:
        plate = universe.get(row["plate_code"])
        if not plate:
            continue
        token = llm_slots.acquire(
            f"sector narrative {row['plate_code']}", llm_slots.BACKGROUND_TIMEOUT
        )
        if token is None:
            result["skipped_no_slot"] += 1
            continue
        try:
            generate_narrative(plate, row, model=model, client=client)
            result["written"] += 1
        except Exception as exc:                                   # noqa: BLE001
            result["failures"].append(f"{row['plate_code']}: {exc}")
            logger.warning("sector narrative failed for %s: %s", row["plate_code"], exc)
        finally:
            llm_slots.release(token)

    logger.info(
        "sector narratives: %d written, %d considered, %d failures",
        result["written"], result["considered"], len(result["failures"]),
    )
    return result


def narrative_for(plate_code: str, window_days: int = NARRATIVE_WINDOW) -> dict[str, Any] | None:
    """The stored narrative, or None. Read-only; never generates on demand."""
    return db.get_sector_narrative(plate_code, window_days)
