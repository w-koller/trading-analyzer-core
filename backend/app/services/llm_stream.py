"""The reject-and-correct loop, streamed — so a "brief me" button can show the
model thinking while it works (cloud #53).

`llm_json.generate_validated_json` blocks for the whole generation and hands
back only the validated object. That is right for a scan and wrong for a
button a person is watching: a 30-120 second wait behind a static
"Interpreting…" line, on a model that exposes its reasoning as it goes, which
the ticker chat already shows live. This module is the same loop with the
stream kept open.

WHAT IS STREAMED, AND WHAT IS NOT. The reasoning channel is streamed as it
arrives. The answer is NOT: it is JSON that means nothing until it has been
parsed and validated, so while it is being written only its length is
reported ("writing"), and the object itself arrives once, as "result", after
the caller's validator has accepted it. A rejected attempt is reported as
rejected, with the reason, before the correction turn goes out — nothing
unvalidated is ever presented as the answer.

This reverses a stance `llm_json._log_trace` records — that a scratchpad is
logged and never put in the UI, lest it be read as the justification. The
owner asked for it, the chat already shows one, and the mitigations are the
chat's own: the reasoning is labelled as the model's thinking, collapsible,
and rendered apart from the validated answer.

WHAT IS SHARED WITH THE BLOCKING LOOP, DELIBERATELY. The caller's validator
(the schema stays the caller's — decisions #52, #63), the correction turn's
exact words (`llm_json.correction_turn`), the exhausted message, temperature
and response format. A streamed answer and a blocking one are held to the
same rules and corrected in the same words.

Events yielded by `stream_validated_json`, in order, per attempt:
    ("attempt",   {"n": 1, "of": 3})
    ("reasoning", "text…")               zero or more
    ("writing",   {"chars": 240})        zero or more, throttled
    ("checking",  {})
    ("rejected",  {"n": 1, "of": 3, "reason": "…", "final": bool})
                                          — or, on success, and last:
    ("result",    {…the validated object…})
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.services import llm_json, llm_slots
from app.services.think_split import ThinkSplitter

logger = logging.getLogger(__name__)

# The chat's heartbeat, for the chat's reason: a comment frame every 15s keeps
# an idle-looking connection provably alive while a cold model loads.
HEARTBEAT_SECONDS = 15.0
# How long to wait for ANYTHING — a reasoning token counts — before calling
# the model wedged. Matches the blocking interpret's own ceiling
# (valuation_narrative.VALUATION_TIMEOUT): deepseek-r1:32b's cold load alone
# has measured 279s (decisions #54), so the chat's 45s would fail a working
# model on its first call of the day.
FIRST_ACTIVITY_TIMEOUT = 300.0
# A "writing" event per this many characters of JSON — enough to animate a
# progress line, not one frame per token.
WRITING_EVERY_CHARS = 48


def sse_frame(event: str, payload: Any) -> str:
    """One SSE frame, JSON payload — the chat router's framing, for its reason:
    `data:` is line-oriented and model text contains newlines."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_validated_json(
    llm: Any,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    validate: Callable[[str], Any],
    subject: str,
    label: str,
    correction_hint: str,
    transport_error: type[Exception],
    exhausted_error: type[Exception],
    retry_on: type[Exception] | tuple[type[Exception], ...] = Exception,
    max_retries: int = 3,
) -> AsyncIterator[tuple[str, Any]]:
    """`llm_json.generate_validated_json`, as a stream of events.

    Takes the same keyword arguments, so a caller's `prepare_*` output feeds
    either one unchanged. `llm` must be an ASYNC client (`llm_json.async_client`).

    Raises transport_error when the model cannot be reached or dies mid-stream,
    and exhausted_error after max_retries rejected attempts — the same two
    failures, with the same meaning, as the blocking loop.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        yield ("attempt", {"n": attempt, "of": max_retries})
        splitter = ThinkSplitter()
        answer: list[str] = []
        written = reported = 0

        def route(channel: str, text: str) -> list[tuple[str, Any]]:
            nonlocal written, reported
            if channel == "reasoning":
                return [("reasoning", text)]
            answer.append(text)
            written += len(text)
            if written - reported >= WRITING_EVERY_CHARS:
                reported = written
                return [("writing", {"chars": written})]
            return []

        try:
            stream = await llm.chat.completions.create(
                model=model,
                messages=messages,
                temperature=llm_json.TEMPERATURE,
                response_format=llm_json.RESPONSE_FORMAT,
                stream=True,
            )
        except transport_error:
            raise
        except Exception as exc:                              # noqa: BLE001
            raise transport_error(
                f"Ollama call failed for {subject} on {model} at "
                f"{settings.ollama_base_url}: {exc}"
            ) from exc

        try:
            async for chunk in stream:
                if not chunk.choices:        # some builds send a usage-only chunk
                    continue
                delta = chunk.choices[0].delta
                # Undeclared on ChoiceDelta; the SDK's models allow extras, so
                # Ollama's reasoning field survives as an attribute (ai_chat).
                trace = getattr(delta, "reasoning", None)
                if trace:
                    yield ("reasoning", str(trace))
                if delta.content:
                    for channel, text in splitter.feed(delta.content):
                        for event in route(channel, text):
                            yield event
            for channel, text in splitter.flush():
                for event in route(channel, text):
                    yield event
        except (transport_error, asyncio.CancelledError):
            raise
        except Exception as exc:                              # noqa: BLE001
            raise transport_error(
                f"The model stopped mid-answer for {subject} on {model}: {exc}"
            ) from exc
        finally:
            await stream.close()

        raw = "".join(answer)
        yield ("checking", {})
        try:
            result = validate(raw)
        except retry_on as exc:
            last_error = exc
            logger.warning("%s validation failed for %s on %s (attempt %d/%d, "
                           "streamed): %s", label, subject, model, attempt,
                           max_retries, exc)
            yield ("rejected", {"n": attempt, "of": max_retries, "reason": str(exc),
                                "final": attempt == max_retries})
            if attempt == max_retries:
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append(llm_json.correction_turn(exc, correction_hint))
            continue

        yield ("result", result)
        return

    raise exhausted_error(
        llm_json.exhausted_message(subject, label, model, max_retries, last_error))


def sse_response(
    events: AsyncIterator[tuple[str, Any]],
    *,
    request: Request,
    slot_label: str,
    meta: dict[str, Any],
    result_extra: dict[str, Any] | None = None,
    errors: tuple[type[Exception], ...] = (),
) -> StreamingResponse:
    """Serve `stream_validated_json`'s events as SSE, owning the lifecycle.

    The chat router's lifecycle, kept to the letter where it was learned the
    hard way:
      * the `llm_slots` slot is taken INSIDE the generator under try/finally
        (decisions #50) — taken before the response is returned, it leaks
        whenever the response is never iterated;
      * the model stream is drained by a task into a bounded queue, so the
        heartbeat is independent of it and never cancels a read mid-flight;
      * a client disconnect — closing the tab, pressing Stop — ends it, so
        Ollama is not left generating for nobody on one of two GPU slots.

    `errors` are the caller's own failure types; they reach the client as an
    `error` frame carrying the message. Anything else is a fault, logged.
    """

    async def generate() -> AsyncIterator[str]:
        token = await asyncio.to_thread(
            llm_slots.acquire, slot_label, llm_slots.INTERACTIVE_TIMEOUT)
        if token is None:
            yield sse_frame("error", {
                "detail": "The model is busy with another request. Try again in a moment.",
                "retryable": True})
            return

        started = time.monotonic()
        active = False
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        producer: asyncio.Task | None = None

        async def drain() -> None:
            try:
                async for event in events:
                    await queue.put(("event", event))
                await queue.put(("end", None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001
                await queue.put(("error", exc))

        try:
            yield sse_frame("meta", meta)
            producer = asyncio.create_task(drain())
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if not active and time.monotonic() - started > FIRST_ACTIVITY_TIMEOUT:
                        yield sse_frame("error", {
                            "detail": f"No response from {meta.get('model', 'the model')} "
                                      f"after {FIRST_ACTIVITY_TIMEOUT:.0f}s. Try again.",
                            "retryable": True})
                        return
                    if await request.is_disconnected():
                        logger.info("%s: client left while waiting (%.1fs in)",
                                    slot_label, time.monotonic() - started)
                        return
                    yield ": ping\n\n"
                    continue

                if kind == "end":
                    break
                if kind == "error":
                    raise payload

                name, data = payload
                if name in ("reasoning", "writing"):
                    active = True
                if await request.is_disconnected():
                    logger.info("%s: client left after %.1fs", slot_label,
                                time.monotonic() - started)
                    return
                if name == "reasoning":
                    yield sse_frame("reasoning", {"text": data})
                elif name == "result":
                    yield sse_frame("result", {**(result_extra or {}), **data})
                else:
                    yield sse_frame(name, data)

            yield sse_frame("done", {"elapsed_seconds": round(time.monotonic() - started, 1)})
        except asyncio.CancelledError:
            raise
        except errors as exc:
            yield sse_frame("error", {"detail": str(exc), "retryable": True})
        except Exception as exc:                              # noqa: BLE001
            logger.warning("%s failed: %s", slot_label, exc)
            yield sse_frame("error", {"detail": f"The model call failed: {exc}",
                                      "retryable": False})
        finally:
            # The chat's order: stop the drain, close the model stream (which
            # tells Ollama to stop), then give the slot back.
            if producer is not None and not producer.done():
                producer.cancel()
                try:
                    await producer
                except (asyncio.CancelledError, Exception):   # noqa: BLE001
                    pass
            await events.aclose()
            llm_slots.release(token)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
