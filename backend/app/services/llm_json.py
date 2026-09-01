"""The reject-and-correct loop that turns a chatty model into validated JSON.

Two callers need the same thing and had grown their own copy of it:
`ai_thesis.generate_thesis` and `earnings_service.generate_outlook`. Both ask
a local model for one JSON object, validate it against a schema, and — when it
does not fit — hand the model its own bad output plus the specific fault and
ask again.

**Retrying with the fault is the whole point.** A blind reroll is about as
likely to repeat the mistake, and each attempt costs 30-120 seconds of local
inference; naming what was wrong turns a coin flip into a correction. Rule #2
is why the loop rejects at all: a thesis is acted on and becomes RAG context
for future advice, so silently coercing "172.0" into 172.0 would launder a
formatting slip into a number someone trades on.

What this module deliberately does NOT own is the schema. Each caller passes
its own validator, because the two schemas must stay separate — an earnings
outlook carrying a thesis's `conviction_score` would put an unvalidated
opinion beside a validated one looking comparable (decisions #52). This module
only knows "the validator said no, ask again".
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Low but not zero. Zero makes a model that has fallen into a bad phrasing
# repeat it verbatim on every retry, which defeats the correction turn.
TEMPERATURE = 0.2

# Ollama accepts this; deepseek-r1 ignores it and fences its object in
# ```json anyway, which is why every response still goes through the caller's
# extract_json. Sent regardless because the models that do honour it produce
# cleaner output for free.
RESPONSE_FORMAT = {"type": "json_object"}


def client(timeout: float):
    """An OpenAI-protocol client pointed at the local Ollama instance.

    Args:
        timeout: seconds to wait for a complete response. Sized by the caller
            for the model in use — a 32B thesis legitimately takes 90-120s.

    Returns:
        A synchronous `openai.OpenAI`. Imported lazily so the offline tests can
        exercise everything here with a fake client and no openai installed.

    For the async equivalent see `ai_chat._async_client`; the difference is
    load-bearing, since iterating a sync stream inside `async def` blocks the
    event loop for the whole generation.
    """
    from openai import OpenAI
    return OpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",          # Ollama ignores it, the SDK requires one
        timeout=timeout,
    )


def generate_validated_json(
    llm: Any,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    validate: Callable[[str], T],
    subject: str,
    label: str,
    correction_hint: str,
    transport_error: type[Exception],
    exhausted_error: type[Exception],
    retry_on: type[Exception] | tuple[type[Exception], ...] = Exception,
    max_retries: int = 3,
) -> T:
    """Ask `model` for one JSON object and keep asking until it validates.

    Args:
        llm:           an OpenAI-protocol client. Injectable so the retry and
                       validation logic can be tested without a live model.
        model:         the model id. Resolve it ONCE, above this call — a
                       correction turn hands the model its own bad output, and
                       that is meaningless if a different model produced it
                       (decisions #38).
        system_prompt: the system turn, sent once and never re-sent.
        user_prompt:   the first user turn, fully assembled by the caller.
        validate:      takes the raw response text, returns the validated
                       object, raises on anything it will not accept. This is
                       where the caller's own extract_json + schema check go.
        subject:       what is being generated about — a ticker code. Appears
                       in every log line and in both error messages.
        label:         "thesis", "outlook" — names the artefact in logs.
        correction_hint: the closing clause of the correction turn, e.g.
                       "exactly the six required keys". The key COUNT differs
                       per schema, and telling the model the wrong number is
                       worse than telling it nothing.
        transport_error: raised when the model cannot be reached. An exception
                       that is already of this type passes through unwrapped,
                       so a caller's own error is not relabelled as a network
                       fault.
        exhausted_error: raised when every attempt failed validation. Kept
                       separate from transport_error because "the model is
                       down" and "the model will not follow the schema" call
                       for completely different responses.
        retry_on:      which validation exceptions are worth another attempt.
        max_retries:   total attempts, not retries after the first.

    Returns:
        Whatever `validate` returned on the first attempt that satisfied it.

    Raises:
        transport_error:  the model could not be reached.
        exhausted_error:  max_retries attempts all failed validation.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = llm.chat.completions.create(
                model=model,
                messages=messages,
                temperature=TEMPERATURE,
                response_format=RESPONSE_FORMAT,
            )
            raw = response.choices[0].message.content or ""
        except transport_error:
            raise
        except Exception as exc:                      # transport / model down
            raise transport_error(
                f"Ollama call failed for {subject} on {model} at "
                f"{settings.ollama_base_url}: {exc}"
            ) from exc

        try:
            return validate(raw)
        except retry_on as exc:
            last_error = exc
            logger.warning(
                "%s validation failed for %s on %s (attempt %d/%d): %s",
                label, subject, model, attempt, max_retries, exc,
            )
            _log_trace(response, label, subject, attempt)
            if attempt == max_retries:
                break
            # Hand the model its own output and the specific fault rather than
            # rerolling blind — see the module docstring.
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    f"That response was rejected: {exc}\n\n"
                    "Return the corrected JSON object only — no markdown "
                    f"fence, no commentary, {correction_hint}."
                ),
            })

    raise exhausted_error(
        f"{subject}: no valid {label} from {model} after {max_retries} "
        f"attempts. Last error: {last_error}"
    )


def _log_trace(response: Any, label: str, subject: str, attempt: int) -> None:
    """Log a reasoning model's chain-of-thought, at DEBUG, on a failed attempt.

    Ollama surfaces it separately from `content`. It is normally noise, but
    when every attempt fails and the ticker is abandoned it is the only record
    of what the model was trying to do.

    Never stored, only logged: it is unvalidated free text, and putting a
    scratchpad in the UI invites reading it as the justification when the
    validated field is the part that actually passed the schema.
    """
    trace = getattr(response.choices[0].message, "reasoning", None)
    if trace:
        logger.debug("%s trace for %s (attempt %d): %s",
                     label, subject, attempt, str(trace)[:2000])
