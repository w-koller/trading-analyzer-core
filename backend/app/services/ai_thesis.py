"""AI thesis generation — prompt assembly, RAG retrieval, strict validation.

Three CLAUDE.md rules converge in this file:

- **Rule #1 — the LLM interprets, it never calculates.** Every number in the
  prompt is precomputed by `indicators.py` / `options_walls.py`. The system
  prompt says so explicitly, and the three level fields — `suggested_entry`,
  `suggested_stop` and `suggested_target` — are the only numbers the model is
  allowed to originate. They are judgements about where a setup turns, not
  quantities derived from the inputs.

- **Rule #2 — strict JSON only, exact shape, reject/retry on malformed
  output, never silently coerce.** `validate_thesis()` is deliberately
  unforgiving: wrong type, wrong casing, extra keys and a reasoning field
  that isn't exactly three sentences are all rejections. Each rejection is
  fed back to the model as a correction turn, so a retry is informed rather
  than a reroll.

- **Rule #3 — RAG retrieval happens before the call.** `generate_thesis()`
  performs the `db.get_similar_setups()` lookup itself rather than accepting
  the results as an argument. A caller cannot forget the retrieval step or
  run it afterwards, because there is no code path through this function
  that reaches the model without it.

Live-model behaviour this was written against (Ollama / deepseek-r1:32b,
verified 2026-08-23):

- A single call takes ~90-120 seconds. `DEFAULT_TIMEOUT` is generous
  accordingly, and the scanner must not assume it can evaluate a large
  watchlist inside one 60s scan interval.
- `response_format={"type": "json_object"}` is accepted but NOT honoured as
  bare JSON — the model still wraps the object in a ```json fence. The
  response is therefore always run through `extract_json()`.
- Ollama surfaces deepseek's chain-of-thought on `message.reasoning` and
  keeps `message.content` clean, but `<think>` blocks are stripped
  defensively in case another model inlines them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

from app import db
from app.services import llm_json, ollama_models

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0   # deepseek-r1:32b runs ~90-120s per thesis
DEFAULT_MAX_RETRIES = 3
MAX_NOTES_LENGTH = 500
REQUIRED_SENTENCES = 3

VALID_DIRECTIONS = ("Bullish", "Bearish", "Neutral")
REQUIRED_KEYS = frozenset({
    "conviction_score", "trade_direction", "reasoning",
    "suggested_entry", "suggested_stop", "suggested_target",
    "key_levels_notes",
})

SYSTEM_PROMPT = """You are a trading analyst producing advisory-only setup \
theses. You never place trades; a human reads your output and decides.

Every technical value you are given has already been calculated \
deterministically in Python. Do not recalculate, re-derive, or second-guess \
any number — read them as given and interpret what they mean together. If a \
value is null it could not be computed; say so rather than estimating it.

Respond with a single JSON object and nothing else. No prose, no markdown \
fence, no explanation before or after. The object must have exactly these \
seven keys:

  "conviction_score":  integer 1-10
  "trade_direction":   exactly one of "Bullish", "Bearish", "Neutral"
  "reasoning":         a string of EXACTLY three sentences
  "suggested_entry":   number or null
  "suggested_stop":    number or null
  "suggested_target":  number or null
  "key_levels_notes":  short string or null

"suggested_entry" is the price at which the setup becomes worth acting on — \
where a buyer would want to be filled. That is not always the current price: \
a setup may be worth waiting for a pullback to, or worth taking only once a \
level has broken. Use null when it is worth acting on at the current price.

The three levels must be ordered. For a Bullish thesis the stop sits below \
the entry and the entry below the target; for a Bearish thesis the stop sits \
above the entry and the entry above the target. Use null for any level you \
cannot justify from the data given, rather than inventing one to satisfy the \
ordering."""


class ThesisError(RuntimeError):
    """The model could not be reached or did not produce a usable thesis."""


class ThesisValidationError(ThesisError):
    """The model's output did not match the required schema."""


@dataclass
class AIThesis:
    """A validated thesis. Constructing one is proof it passed the schema."""

    conviction_score: int
    trade_direction: str
    reasoning: str
    suggested_entry: float | None
    suggested_stop: float | None
    suggested_target: float | None
    key_levels_notes: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Sentence counting
# --------------------------------------------------------------------------

# Periods that do not end a sentence. Decimals matter most here: a thesis
# almost always contains a price like "179.94", and a naive split on "."
# would read it as two sentence breaks and reject valid output forever.
_ABBREVIATIONS = (
    "u.s.", "e.g.", "i.e.", "vs.", "etc.", "approx.", "est.",
    "inc.", "corp.", "ltd.", "co.", "no.", "a.m.", "p.m.",
)
_SENTINEL = "\x00"


def count_sentences(text: str) -> int:
    """Count sentences, tolerating decimal numbers and common abbreviations."""
    if not text or not text.strip():
        return 0
    protected = re.sub(r"(?<=\d)\.(?=\d)", _SENTINEL, text)
    for abbr in _ABBREVIATIONS:
        # \b matters more than it looks. Without it these match as
        # SUBSTRINGS: "est." swallowed the period in "open interest.", so a
        # perfectly good three-sentence thesis counted as two and was
        # rejected three times over before the ticker was abandoned. "Open
        # interest" appears in almost every options thesis, so this failed
        # US.TSLA and US.GNRC in a single scan. Same trap for "co." in
        # "Cisco.", "inc." in "zinc.", "no." in "casino.".
        protected = re.sub(
            r"\b" + re.escape(abbr),
            lambda m: m.group(0).replace(".", _SENTINEL),
            protected,
            flags=re.IGNORECASE,
        )
    parts = [p for p in re.split(r"[.!?]+(?=\s|$)", protected) if p.strip()]
    return len(parts)


# --------------------------------------------------------------------------
# Response parsing and validation
# --------------------------------------------------------------------------

def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model response.

    Tolerates the wrappers the model actually emits — a ```json fence, an
    inlined <think> block, stray leading newlines — but does not tolerate
    the object itself being wrong. Parsing is separate from validation on
    purpose: a fence is a formatting artefact, a missing key is not.
    """
    if not text or not text.strip():
        raise ThesisValidationError("model returned an empty response")

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    cleaned = cleaned.strip()

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ThesisValidationError(
            f"no JSON object found in response: {text[:200]!r}"
        )
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ThesisValidationError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ThesisValidationError(
            f"expected a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _number(value: Any, field: str) -> float | None:
    """Accept a JSON number or null; reject strings and bools outright.

    A quoted "150.5" is the model ignoring the schema, not a formatting
    quirk — coercing it would hide exactly the failure rule #2 asks us to
    surface. bool is checked first because bool subclasses int in Python.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThesisValidationError(
            f"{field} must be a number or null, got {type(value).__name__} ({value!r})"
        )
    result = float(value)
    if result != result:
        raise ThesisValidationError(f"{field} must be a real number, got NaN")
    return result


def validate_thesis(payload: dict[str, Any]) -> AIThesis:
    """Validate a parsed response against the rule #2 schema.

    Raises ThesisValidationError with a message specific enough to hand
    straight back to the model as a correction.
    """
    if not isinstance(payload, dict):
        raise ThesisValidationError(f"expected an object, got {type(payload).__name__}")

    keys = set(payload)
    missing = REQUIRED_KEYS - keys
    if missing:
        raise ThesisValidationError(f"missing required keys: {sorted(missing)}")
    extra = keys - REQUIRED_KEYS
    if extra:
        raise ThesisValidationError(
            f"unexpected keys {sorted(extra)}; the object must have exactly "
            f"{sorted(REQUIRED_KEYS)}"
        )

    score = payload["conviction_score"]
    if isinstance(score, bool) or not isinstance(score, int):
        raise ThesisValidationError(
            f"conviction_score must be an integer 1-10, got "
            f"{type(score).__name__} ({score!r})"
        )
    if not 1 <= score <= 10:
        raise ThesisValidationError(f"conviction_score must be 1-10, got {score}")

    direction = payload["trade_direction"]
    if direction not in VALID_DIRECTIONS:
        raise ThesisValidationError(
            f"trade_direction must be exactly one of {list(VALID_DIRECTIONS)} "
            f"(case-sensitive), got {direction!r}"
        )

    reasoning = payload["reasoning"]
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ThesisValidationError("reasoning must be a non-empty string")
    sentences = count_sentences(reasoning)
    if sentences != REQUIRED_SENTENCES:
        raise ThesisValidationError(
            f"reasoning must be exactly {REQUIRED_SENTENCES} sentences, "
            f"got {sentences}: {reasoning!r}"
        )

    entry = _number(payload["suggested_entry"], "suggested_entry")
    stop = _number(payload["suggested_stop"], "suggested_stop")
    target = _number(payload["suggested_target"], "suggested_target")

    notes = payload["key_levels_notes"]
    if notes is not None:
        if not isinstance(notes, str):
            raise ThesisValidationError(
                f"key_levels_notes must be a string or null, got {type(notes).__name__}"
            )
        if len(notes) > MAX_NOTES_LENGTH:
            raise ThesisValidationError(
                f"key_levels_notes must be at most {MAX_NOTES_LENGTH} characters, "
                f"got {len(notes)}"
            )
        notes = notes.strip() or None

    # Directional coherence. A Bullish thesis whose stop sits above its
    # target is not a formatting slip — it is advice that would lose money
    # if followed, which is the one thing this tool exists to produce. The
    # entry is held to the same standard: an entry outside its own stop and
    # target describes a trade that is already lost, or already won, at the
    # moment it is opened.
    #
    # Each pair is checked independently rather than as one three-way
    # comparison, so a thesis naming only two of the three levels is still
    # held to the ordering of the pair it did name. Nulls are skipped, not
    # defaulted — "I cannot justify a level" stays a legal answer.
    #
    # Neutral is exempt entirely. It makes no directional claim, so there is
    # no ordering its levels could contradict.
    if direction in ("Bullish", "Bearish"):
        bullish = direction == "Bullish"
        # Ordered (lower, higher) as a BULLISH thesis reads them; Bearish
        # inverts the whole set, which is why one flag drives all three
        # comparisons instead of each pair carrying its own direction.
        # stop/target comes first so that a thesis violating several pairs
        # reports the same fault it always has.
        pairs = (
            ("stop", stop, "target", target),
            ("stop", stop, "entry", entry),
            ("entry", entry, "target", target),
        )
        for lo_name, lo, hi_name, hi in pairs:
            if lo is None or hi is None:
                continue
            if bullish and lo >= hi:
                raise ThesisValidationError(
                    f"Bullish thesis needs suggested_{lo_name} below "
                    f"suggested_{hi_name}, got {lo_name}={lo} {hi_name}={hi}"
                )
            if not bullish and lo <= hi:
                raise ThesisValidationError(
                    f"Bearish thesis needs suggested_{lo_name} above "
                    f"suggested_{hi_name}, got {lo_name}={lo} {hi_name}={hi}"
                )

    return AIThesis(
        conviction_score=score,
        trade_direction=direction,
        reasoning=reasoning.strip(),
        suggested_entry=entry,
        suggested_stop=stop,
        suggested_target=target,
        key_levels_notes=notes,
    )


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

# Moved to `prompt_blocks` so the chat endpoint describes a setup in the same
# words this prompt does. Re-exported under their original private names
# rather than referenced through the module, because they are imported by
# name from here (tests/test_ai_thesis.py) and because `build_prompt` below
# reads better as a flat assembly of local helpers than as a wall of
# `prompt_blocks._` prefixes.
from app.services.prompt_blocks import (  # noqa: E402  (grouped with its use)
    _freshness_line,
    _indicator_block,
    _news_block,
    _similar_block,
    _walls_block,
)

__all__ = [
    "AIThesis", "ThesisError", "ThesisValidationError",
    "build_prompt", "count_sentences", "extract_json",
    "generate_thesis", "validate_thesis",
]


def build_prompt(
    code: str,
    market: str,
    indicators: dict[str, Any],
    walls: dict[str, Any] | None,
    similar_setups: list[dict[str, Any]],
    data_as_of: str,
    is_delayed_data: bool,
    news: dict[str, Any] | None = None,
    session: str | None = None,
    bar_age_days: float | None = None,
    bars_stale: bool = False,
) -> str:
    """Assemble the user-turn prompt. Every number here is precomputed."""
    return f"""TICKER: {code}   MARKET: {market}\
{f"   SESSION: {session}" if session else ""}

{_freshness_line(market, is_delayed_data, data_as_of, bar_age_days, bars_stale)}

TECHNICAL INDICATORS (calculated in Python — read, do not recompute):
{_indicator_block(indicators)}

OPTIONS POSITIONING:
{_walls_block(walls)}

RECENT NEWS:
{_news_block(news, code)}

HISTORICALLY SIMILAR SETUPS AND HOW THEY ACTUALLY RESOLVED:
{_similar_block(similar_setups)}

Weigh the historical outcomes above: if setups shaped like this one have \
resolved badly, that should pull your conviction down even when the \
technicals look clean, and vice versa.

Produce the JSON object now."""


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def generate_thesis(
    *,
    code: str,
    market: str,
    feature_vector: list[float],
    indicators: dict[str, Any],
    walls: dict[str, Any] | None,
    data_as_of: str,
    is_delayed_data: bool,
    news: dict[str, Any] | None = None,
    session: str | None = None,
    bar_age_days: float | None = None,
    bars_stale: bool = False,
    top_k: int = 3,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    client: Any = None,
    model: str | None = None,
) -> tuple[AIThesis, list[dict[str, Any]]]:
    """Produce one validated thesis. Returns (thesis, similar_setups_used).

    The RAG retrieval (rule #3) happens here, before the model is contacted,
    and the retrieved setups are returned alongside the thesis so the caller
    can persist `similar_setup_ids` as an audit trail of what was injected.

    `client` is injectable so the retry/validation logic can be exercised
    without a live model; left as None it builds an Ollama-backed client.

    `model` pins one model for this call instead of resolving the active one.
    Production never passes it — the scanner wants whatever the user selected.
    It exists for the benchmark, which must compare models without writing to
    `app_state`: that key is read by every other caller, so setting it to
    measure one model would silently change the model a concurrently running
    scan is midway through using (decisions #38 resolves once per thesis, not
    once per attempt, which is exactly the guarantee that would break).
    """
    # --- RAG step: retrieve BEFORE the call, never after (rule #3) ---
    similar = db.get_similar_setups(feature_vector, top_k=top_k)
    logger.info("RAG: retrieved %d similar setups for %s", len(similar), code)

    prompt = build_prompt(
        code=code, market=market, indicators=indicators, walls=walls,
        similar_setups=similar, data_as_of=data_as_of,
        is_delayed_data=is_delayed_data, news=news, session=session,
        bar_age_days=bar_age_days, bars_stale=bars_stale,
    )

    # Resolved ONCE, above the retry loop. Per-attempt resolution would let a
    # mid-flight model change swap models between an attempt and its
    # correction turn — and that correction hands the model its own bad
    # output to fix, which is meaningless if a different model produced it.
    model = model or ollama_models.active_model()

    thesis = llm_json.generate_validated_json(
        client if client is not None else llm_json.client(timeout),
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        # extract_json first: deepseek-r1 ignores response_format and fences
        # its object in ```json, so the raw text is not parseable as-is.
        validate=lambda raw: validate_thesis(extract_json(raw)),
        subject=code,
        label="thesis",
        correction_hint="exactly the seven required keys",
        transport_error=ThesisError,
        exhausted_error=ThesisValidationError,
        retry_on=ThesisValidationError,
        max_retries=max_retries,
    )
    logger.info("thesis for %s: %s conviction %d",
                code, thesis.trade_direction, thesis.conviction_score)
    return thesis, similar
