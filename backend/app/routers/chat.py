"""Streaming Q&A about one ticker.

The only Server-Sent Events endpoint in the project. It exists because a
27B model takes 15-60 seconds to answer and a spinner that long reads as a
hang — the user cannot tell a slow model from a broken one, and the answer
arriving all at once at the end wastes the whole generation as feedback.

Three things here are easy to get subtly wrong and are commented where they
happen: the slot is acquired inside the generator, frames carry JSON rather
than raw text, and the upstream stream is closed when the client leaves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.services import ai_chat, llm_slots, ollama_models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

HEARTBEAT_SECONDS = 15.0


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=ai_chat.MAX_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=ai_chat.MAX_MESSAGE_CHARS)
    # Bounded here as well as trimmed in build_messages. This bound rejects an
    # abusive body before it is parsed into turns; that one decides how much
    # of a legitimate conversation is worth re-sending to the model.
    history: list[ChatTurn] = Field(default_factory=list, max_length=64)


def _frame(event: str, payload: dict[str, Any]) -> str:
    """One SSE frame.

    The payload is JSON, never raw text. SSE frames terminate on a blank line
    and `data:` is line-oriented, so a model token containing a newline would
    otherwise split into two frames and be silently reassembled wrong by the
    client. JSON-encoding sidesteps the whole class of problem for the cost of
    a few bytes.
    """
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _error_frame(detail: str, *, retryable: bool) -> str:
    """An error the client renders in place of an answer.

    Args:
        detail:    what to show the user. Reaches the UI verbatim.
        retryable: whether trying again could plausibly work. A busy model or
            one still loading is retryable; a model that died mid-generation
            is not, and offering a retry button for it wastes the user's time.

    Errors travel as SSE frames rather than an HTTP status because by the time
    most of them happen the response has already begun streaming and the
    status is long since sent. The client needs this path regardless — the
    model can die at token 40.
    """
    return _frame("error", {"detail": detail, "retryable": retryable})


def _meta_payload(ctx: dict[str, Any], model: str) -> dict[str, Any]:
    """The opening frame: what the answer is grounded in, before any of it.

    Sent first so the panel can caveat the answer while it is still arriving
    rather than after. Freshness is the point of most of these fields — rule
    #7 means a delayed quote must never read as live, and a six-day-old thesis
    must never read as current.

    Args:
        ctx:   the stored analysis from `ai_chat.build_context`.
        model: the model resolved for this answer.

    Returns:
        The JSON payload for the "meta" event.
    """
    return {
        "model": model,
        "setup_id": ctx["setup_id"],
        "setup_age_hours": ctx["setup_age_hours"],
        "is_delayed_data": bool(ctx["setup"]["is_delayed_data"]),
        "data_as_of": ctx["setup"]["data_as_of"],
        "held": ctx["position"] is not None,
        "news_items": len((ctx.get("news") or {}).get("ticker") or []),
        "has_walls": bool((ctx.get("walls") or {}).get("has_walls")),
    }


def _done_payload(started: float, first_token_at: float | None,
                  finish_reason: str) -> dict[str, Any]:
    """The closing frame: how the generation ended, and how long it took.

    Args:
        started:        monotonic clock at the start of the generation.
        first_token_at: monotonic clock when the first token arrived, or None
            if none ever did.
        finish_reason:  the model's own reason. 'length' means MAX_TOKENS cut
            it off mid-sentence, and the UI says so — a truncated answer that
            looks complete is worse than one labelled truncated.

    Returns:
        The JSON payload for the "done" event.
    """
    return {
        "finish_reason": finish_reason,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "first_token_seconds": (
            round(first_token_at - started, 1) if first_token_at else None
        ),
    }


@router.post("/{code}/stream")
async def stream_chat(code: str, body: ChatRequest, request: Request):
    """Answer a question about `code`, streamed token by token.

    404 when there is no stored analysis: without a thesis the model has
    nothing but its own priors, which is the exact failure rule #1 exists to
    prevent. Better to disable the panel than to answer from vibes.
    """
    try:
        ctx = await run_in_threadpool(ai_chat.build_context, code)
    except ai_chat.ChatUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    model = await run_in_threadpool(ollama_models.active_model)

    async def generate() -> AsyncIterator[str]:
        # Acquired HERE, not in the endpoint above. A slot taken before the
        # StreamingResponse is returned leaks if the response is never
        # iterated — the client can disappear between the two — whereas
        # try/finally inside the generator is airtight. The cost is that a
        # "busy" answer arrives as an SSE error frame rather than a 503, and
        # the client already needs that path anyway because the model can die
        # at token 40.
        token = await asyncio.to_thread(
            llm_slots.acquire, f"chat {code}", llm_slots.INTERACTIVE_TIMEOUT
        )
        if token is None:
            yield _error_frame(
                "The model is busy with another request. Try again in a moment.",
                retryable=True,
            )
            return

        started = time.monotonic()
        first_token_at: float | None = None
        finish_reason = "stop"
        producer: asyncio.Task | None = None
        stream = ai_chat.stream_answer(
            ctx, body.message, [t.model_dump() for t in body.history], model=model,
        )

        # The model stream is drained by a background task into a queue rather
        # than iterated directly, so the heartbeat clock is independent of it.
        # The obvious alternative — asyncio.wait_for around __anext__ — cancels
        # a pending read on every heartbeat, and cancelling mid-read on an
        # httpx-backed stream can leave it unusable. The queue is bounded, so a
        # model faster than the client cannot buffer without limit.
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def drain() -> None:
            try:
                async for pair in stream:
                    await queue.put(("chunk", pair))
                await queue.put(("end", None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # noqa: BLE001
                await queue.put(("error", exc))

        try:
            yield _frame("meta", _meta_payload(ctx, model))

            producer = asyncio.create_task(drain())

            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Nothing yet. Distinguish "still loading" from "wedged":
                    # a cold 27B takes tens of seconds to load, but past the
                    # deadline, silence is a fault and should say so rather
                    # than heartbeat forever.
                    if (first_token_at is None
                            and time.monotonic() - started > ai_chat.FIRST_TOKEN_TIMEOUT):
                        yield _error_frame(
                            f"No response from {model} after "
                            f"{ai_chat.FIRST_TOKEN_TIMEOUT:.0f}s. It may still "
                            "be loading — try again.",
                            retryable=True,
                        )
                        return
                    if await request.is_disconnected():
                        logger.info(
                            "chat %s: client disconnected while waiting for the "
                            "first token (%.1fs in)", code, time.monotonic() - started,
                        )
                        return
                    # A comment frame. Every SSE parser ignores it; it exists
                    # to keep the connection provably alive while the model
                    # thinks, so an intermediary does not drop it as idle.
                    yield ": ping\n\n"
                    continue

                if kind == "end":
                    break
                if kind == "error":
                    raise payload

                channel, text = payload
                if channel == "finish":
                    finish_reason = text
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                # Checked between chunks: this is the only signal that the user
                # closed the tab or pressed Stop. Without it Ollama keeps
                # generating for nobody, holding the slot this design protects.
                if await request.is_disconnected():
                    logger.info("chat %s: client disconnected after %.1fs",
                                code, time.monotonic() - started)
                    return
                yield _frame(channel, {"text": text})

            yield _frame("done",
                         _done_payload(started, first_token_at, finish_reason))
        except asyncio.CancelledError:
            logger.info("chat %s: cancelled after %.1fs", code,
                        time.monotonic() - started)
            raise
        except Exception as exc:                          # noqa: BLE001
            logger.warning("chat %s failed on %s: %s", code, model, exc)
            yield _error_frame(f"The model call failed: {exc}", retryable=False)
        finally:
            # Order matters: cancel the drain first so nothing is mid-read,
            # then close the model stream (which tells Ollama to stop
            # generating), then give the slot back.
            if producer is not None and not producer.done():
                producer.cancel()
                try:
                    await producer
                except (asyncio.CancelledError, Exception):   # noqa: BLE001
                    pass
            await stream.aclose()
            llm_slots.release(token)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # No reverse proxy today. If one is ever added, its default
            # buffering would hold every frame until the response completed —
            # turning a streaming endpoint back into a blocking one, silently,
            # with nothing in any log to say why.
            "X-Accel-Buffering": "no",
        },
    )
