"""Free-form questions about one ticker, answered from what is already stored.

This is the one model call in the project that is NOT strict JSON, and that is
deliberate rather than an oversight. Rule #2 governs the *thesis* endpoint: a
thesis is acted on, feeds `trade_outcomes`, and is retrieved as RAG context
for later advice, so it has to be schema-checked or a formatting slip becomes
a number someone trades on. A conversation is read once by a person and then
gone. Validating prose against a schema it has no reason to satisfy would buy
nothing and reject answers that were fine.

What still holds, unchanged:

  * Rule #1. Every number in the prompt was calculated in Python before the
    model saw it. Nothing here recomputes an indicator, and the system prompt
    forbids the model from deriving one. `build_context` reads
    `indicator_snapshot` — it never calls `indicators.compute`.
  * Rule #3's spirit, by omission. RAG retrieval is a *pre-thesis* step. The
    chat produces no thesis, no conviction and no levels, so injecting
    precedents would only invite the model to re-score a conviction that
    `validate_thesis` already settled. `get_similar_setups` is not called.
  * Rule #7. The freshness line and the thesis-age line both go in the prompt,
    because a six-day-old analysis read as current is the same failure as a
    delayed quote read as live.

Advisory-only is enforced in three places, not one: this system prompt, the
preset questions in the UI (which are phrased away from action, so the
highest-traffic path never asks for a recommendation), and a static line under
the input that model output cannot remove. There is deliberately no output
filtering — you cannot retract tokens already streamed, and blanking a
sentence mid-flow renders as a bug rather than as a safeguard.

Nothing said here is persisted. `trading.db` is the corpus future advice is
built from; unvalidated prose sitting beside validated theses invites being
read as evidence. The cost is that a page refresh clears the transcript, which
is the right trade for a panel you consult and move on from.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Iterable

from app import db
from app.config import settings
from app.services import news_service, ollama_models, prompt_blocks

logger = logging.getLogger(__name__)

CHAT_TIMEOUT = 120.0
FIRST_TOKEN_TIMEOUT = 45.0     # a cold 27B load is tens of seconds
MAX_MESSAGE_CHARS = 2000
MAX_HISTORY_TURNS = 6          # user+assistant pairs kept, server-side
MAX_TOKENS = 1100      # ~3 full paragraphs; see the truncation note below
TEMPERATURE = 0.3

CHAT_SYSTEM_PROMPT = """You are a research assistant answering questions about \
ONE ticker, using only the stored analysis provided in the user turn. This tool \
is advisory-only and has no order path of any kind — it cannot buy or sell \
anything, and neither can you.

Every number you have was calculated deterministically in Python before you saw \
it. Read them as given. Never recalculate, re-derive, extrapolate or estimate a \
number, and never invent one that is not in front of you. If something is not in \
your context, say you do not have it — especially the current price, which you \
do not have.

You do not tell the user to buy, sell, hold, add, trim or size a position. If \
they ask what they should do, do not answer with a recommendation: set out what \
the stored analysis says for and against, name what would invalidate it, say \
which figures are stale or delayed, and state plainly that the decision and the \
order are theirs. Declining to be their decision is the job, not an evasion — be \
genuinely useful about the evidence instead.

Answer in plain prose, one to three short paragraphs. No JSON, no markdown \
headings, no bullet lists unless the user asks for one."""


class ChatUnavailable(RuntimeError):
    """No stored analysis for this ticker, so there is nothing to talk about."""


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------

def _position_for(code: str) -> dict[str, Any] | None:
    """The user's holding, read server-side.

    Never accepted from the client. A browser-supplied quantity and cost is a
    number the user could edit, and the model would then reason over it as if
    it were fact.
    """
    try:
        from app.services.moomoo_trade_gateway import get_trade_gateway
        for p in get_trade_gateway().list_positions():
            if str(p.get("code")) == code:
                return p
    except Exception as exc:                              # noqa: BLE001
        logger.info("chat: no position data for %s (%s)", code, exc)
    return None


def build_context(code: str) -> dict[str, Any]:
    """Everything the model gets, assembled from storage only.

    Makes no OpenD quote call by design. A chat must keep working while a
    pre-market scan owns the gateway for an hour, and the price it would fetch
    is not the price the stored thesis reasoned from anyway — offering a live
    number next to hour-old levels invites comparing the two as if they were
    measured together.
    """
    setup = db.get_latest_setup_for_code(code)
    if setup is None:
        raise ChatUnavailable(
            f"No stored analysis for {code} yet. Run a scan on it first — "
            f"without a thesis there are no numbers to answer from, only the "
            f"model's own priors."
        )

    snap = json.loads(setup["indicator_snapshot"] or "{}")
    created = db.parse_iso(setup["created_at"])
    age_hours = None
    if created is not None:
        from datetime import datetime, timezone
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600

    try:
        news = news_service.get_thesis_context(code)
    except Exception as exc:                              # noqa: BLE001
        logger.info("chat: no news context for %s (%s)", code, exc)
        news = None

    return {
        "code": code,
        "market": setup["market"],
        "setup": setup,
        "setup_id": setup["id"],
        "setup_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "indicators": snap.get("indicators") or {},
        "walls": snap.get("walls"),
        "session": snap.get("session"),
        "spot": snap.get("spot"),
        "bar_age_days": snap.get("bar_age_days"),
        "bars_stale": bool(snap.get("bars_stale")),
        "thesis_model": snap.get("model"),
        "news": news,
        "position": _position_for(code),
    }


def _thesis_block(setup: dict[str, Any], age_hours: float | None, model: str | None) -> str:
    when = f"{age_hours:.1f}h ago" if age_hours is not None else "at an unknown time"
    by = f" by {model}" if model else ""
    return (
        f"STORED THESIS (produced{by} {when}):\n"
        f"  Direction: {setup['trade_direction']}   "
        f"Conviction: {setup['conviction_score']}/10\n"
        f"  Reasoning: {setup['reasoning']}\n"
        f"  Suggested entry: {prompt_blocks._fmt(setup.get('suggested_entry'))}   "
        f"Suggested stop: {prompt_blocks._fmt(setup['suggested_stop'])}   "
        f"Suggested target: {prompt_blocks._fmt(setup['suggested_target'])}\n"
        f"  Key levels: {setup['key_levels_notes'] or 'none noted'}"
    )


def _position_block(position: dict[str, Any] | None) -> str:
    if not position:
        return "THE USER'S POSITION: they do not hold this ticker."
    return (
        "THE USER'S POSITION IN THIS TICKER:\n"
        f"  {prompt_blocks._fmt(position.get('qty'), 4)} units at an average cost of "
        f"{prompt_blocks._fmt(position.get('avg_cost'))} "
        f"{position.get('currency') or ''}, last "
        f"{prompt_blocks._fmt(position.get('last_price'))}, unrealised "
        f"{prompt_blocks._fmt(position.get('unrealized_pnl_pct'), 2, '%')}.\n"
        "  This is what they hold. It is not a view on what they should do "
        "about it."
    )


def build_prompt(ctx: dict[str, Any]) -> str:
    """The user-turn context block. The question is appended as its own turn."""
    setup = ctx["setup"]
    market = ctx["market"]
    is_delayed = bool(setup["is_delayed_data"])

    age = ctx["setup_age_hours"]
    age_warning = (
        f"THESIS AGE: this analysis was produced {age:.1f} hours ago and every "
        "level below is from then. You do NOT have a live price. If asked what "
        "it is trading at now, say so rather than quoting the figure below."
        if age is not None else
        "THESIS AGE: unknown. Treat every level below as potentially stale."
    )

    return "\n\n".join([
        f"TICKER: {ctx['code']}   MARKET: {market}"
        + (f"   SESSION: {ctx['session']}" if ctx.get("session") else ""),
        prompt_blocks._freshness_line(
            market, is_delayed, setup["data_as_of"],
            ctx.get("bar_age_days"), ctx.get("bars_stale"),
        ),
        age_warning,
        _thesis_block(setup, age, ctx.get("thesis_model")),
        "TECHNICAL INDICATORS (already calculated in Python — read, do not recompute):\n"
        + prompt_blocks._indicator_block(ctx["indicators"]),
        "OPTIONS POSITIONING:\n" + prompt_blocks._walls_block(ctx.get("walls")),
        prompt_blocks._news_block(ctx.get("news"), ctx["code"]),
        _position_block(ctx.get("position")),
    ])


def build_messages(ctx: dict[str, Any], message: str,
                   history: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """System + context + trimmed history + the new question.

    History is truncated here rather than trusted from the client: an
    unbounded history is an unbounded prompt is unbounded GPU time, and the
    slot limiter caps concurrency, not the size of any one request.
    """
    turns = [
        {"role": h["role"], "content": str(h["content"])[:MAX_MESSAGE_CHARS]}
        for h in history
        if h.get("role") in ("user", "assistant") and h.get("content")
    ][-(MAX_HISTORY_TURNS * 2):]

    return [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(ctx)},
        *turns,
        {"role": "user", "content": message[:MAX_MESSAGE_CHARS]},
    ]


# --------------------------------------------------------------------------
# <think> splitting
# --------------------------------------------------------------------------

class ThinkSplitter:
    """Route inline <think>…</think> to the reasoning channel, incrementally.

    `ai_thesis.extract_json` strips these with a regex, which needs the whole
    string — useless on a stream. And this cannot be skipped just because the
    current model does not emit them: the model is runtime-selectable
    (decisions #38) and deepseek-r1 is still the env default. Ollama normally
    surfaces deepseek's chain-of-thought on `delta.reasoning` and keeps
    `content` clean, but that is a property of the server's template for one
    model, not a guarantee — a model that inlines the tags would otherwise
    have its scratchpad rendered as the answer.

    Buffers on a partial tag rather than guessing: '<' may be the start of
    '<think>' or just a less-than sign, and it is only knowable once more
    characters arrive.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._thinking = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Returns [(channel, text)] where channel is 'token' or 'reasoning'."""
        self._buf += chunk
        out: list[tuple[str, str]] = []

        while self._buf:
            tag = self.CLOSE if self._thinking else self.OPEN
            idx = self._buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append(("reasoning" if self._thinking else "token",
                                self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._thinking = not self._thinking
                continue

            # No complete tag. Emit everything that cannot be the start of one.
            keep = 0
            for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(tag[:n]):
                    keep = n
                    break
            if keep < len(self._buf):
                out.append(("reasoning" if self._thinking else "token",
                            self._buf[:len(self._buf) - keep]))
            self._buf = self._buf[len(self._buf) - keep:]
            break

        return [(c, t) for c, t in out if t]

    def flush(self) -> list[tuple[str, str]]:
        """Whatever is left when the stream ends — a truncated tag is text."""
        if not self._buf:
            return []
        rest, self._buf = self._buf, ""
        return [("reasoning" if self._thinking else "token", rest)]


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------

def _async_client(timeout: float):
    """Mirrors `ai_thesis._client`, but async — and that is load-bearing.

    `openai`'s sync client yields a BLOCKING iterator. Iterating it inside an
    `async def` blocks the event loop for the entire generation, which starves
    /livez, which makes the watchdog restart a backend that is working
    perfectly (decisions #26 and #30). `ai_thesis` stays sync because it runs
    under run_in_threadpool from a blocking scan cycle; a streaming endpoint
    cannot.
    """
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",
        timeout=timeout,
    )


async def stream_answer(
    ctx: dict[str, Any],
    message: str,
    history: Iterable[dict[str, str]],
    model: str | None = None,
    timeout: float = CHAT_TIMEOUT,
    client: Any = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yields ('token'|'reasoning', text), then exactly one ('finish', reason).

    The caller owns the LLM slot and the disconnect check — this only knows
    how to talk to the model. Splitting it that way keeps the SSE framing and
    the lifecycle in the router, where the request object lives.

    The trailing ('finish', reason) pair is not decoration. `reason` is
    'length' when MAX_TOKENS cut the answer off mid-sentence, and the user has
    to be told that: a truncated answer that presents itself as complete is
    worse than a short one, because the missing half is invisible. Reporting a
    hardcoded 'stop' would make the endpoint claim something it does not know.
    """
    model = model or ollama_models.active_model()
    llm = client if client is not None else _async_client(timeout)
    splitter = ThinkSplitter()

    stream = await llm.chat.completions.create(
        model=model,
        messages=build_messages(ctx, message, history),
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        stream=True,
    )
    finish_reason = "stop"
    try:
        async for chunk in stream:
            if not chunk.choices:      # some builds send a usage-only chunk
                continue
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            delta = chunk.choices[0].delta
            # `reasoning` is not declared on ChoiceDelta; the SDK's models are
            # extra="allow", so Ollama's extra field survives as an attribute.
            trace = getattr(delta, "reasoning", None)
            if trace:
                yield ("reasoning", str(trace))
            if delta.content:
                for pair in splitter.feed(delta.content):
                    yield pair
        for pair in splitter.flush():
            yield pair
        yield ("finish", finish_reason)
    finally:
        # Closing upstream is the point of the whole disconnect path: without
        # it Ollama keeps generating for a user who has left, holding the slot
        # this design exists to protect.
        await stream.close()
