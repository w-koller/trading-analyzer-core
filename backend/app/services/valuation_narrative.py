"""The "brief me" button for the intrinsic-value calculator.

WHERE THE LINE IS, AND WHY IT IS DRAWN HERE

`sector_narrative.py` already answered this question once, quoting
`alerts.py` (decisions #53): "If a narrative is ever wanted, the shape is a
'brief me' button handing the already-computed list to the model." This
module is exactly that, for the valuation calculator: DCF/GGM/NAV are
computed in the browser before this is ever called, the model never sees a
raw input before the number exists, and it cannot adjust or recompute
anything — it only narrates a result that already exists.

THE SCHEMA CARRIES NO NUMBER, DELIBERATELY

Two keys, both prose. Exactly the `sector_narrative` reasoning (decisions
#52): a model-authored figure sitting beside the real, code-computed
intrinsic value would look comparable to it and would not be.

THE RULE THAT MATTERS MOST — AND WHY IT NEEDED A NEW MECHANISM

Every other schema in this app that constrains what a model may claim does
it with a fixed whitelist: `validate_thesis` checks ordering against
numbers the caller already trusts, `sector_narrative` checks cited
headlines against the exact set of titles supplied. There is no equivalent
fixed set here — the whole point is prose *about* numbers — so this module
adds a numeric-fidelity check instead: every number-shaped token in the
model's prose must be within a small tolerance of a number that was
actually placed in the prompt. A token that matches nothing given back is
rejected and the offending figure is fed back on the correction turn, the
same reject-not-coerce stance every schema here takes (decisions #14).

WHAT IS NEW ABOUT THE INPUT SIDE

Every other prompt builder in this app assembles its prompt from
backend-computed or database-stored numbers. This is the first one built
from numbers a user typed into a browser slider — a hypothetical, not a
fact about a real position. Nothing here is persisted: an interpretation of
one moment's assumptions has no business in the RAG corpus or the thesis
scorecard, and re-running it after changing an input is the expected use.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.services import ai_thesis, llm_json, ollama_models

logger = logging.getLogger(__name__)

VALUATION_TIMEOUT = 300.0
VALUATION_MAX_RETRIES = 3

MAX_PROSE = 500
MIN_SUMMARY_SENTENCES, MAX_SUMMARY_SENTENCES = 2, 4
MIN_NOTE_SENTENCES, MAX_NOTE_SENTENCES = 1, 2

REQUIRED_KEYS = frozenset({"summary", "sensitivity_note"})

_MODEL_LABELS = {
    "dcf": "Discounted Cash Flow (DCF)",
    "ggm": "Gordon Growth Model / Dividend Discount Model",
    "nav": "Net Asset Value (NAV)",
}

SYSTEM_PROMPT = """You are explaining the output of a deterministic stock-\
valuation calculator to the person who just configured it. Every number \
below was computed in code before you saw it — read them as given, never \
recalculate, adjust, or invent a number of your own. If you are unsure of a \
figure, describe it in words rather than guessing a value.

This tool is advisory-only: it has no order path, and you never tell anyone \
to buy, sell, hold, add or trim. Describe what the numbers say and what the \
result is most sensitive to — you are annotating a calculation, not making \
a recommendation.

Respond with a single JSON object and nothing else, with exactly these keys:
  "summary"          - 2 to 4 sentences on what is driving the verdict
  "sensitivity_note" - 1 to 2 sentences naming the single input the result \
is most sensitive to, and roughly how much it would need to move to flip \
the verdict"""


class ValuationNarrativeError(RuntimeError):
    """The model could not be reached."""


class ValuationNarrativeValidationError(ValuationNarrativeError):
    """The response was reached but rejected. Worth another attempt."""


# --- numeric fidelity: the whitelist mechanism this schema needs -----------

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")


def extract_numbers(text: str) -> list[float]:
    """Public — cloud's advisor_narrative.py imports this rather than
    duplicating it, the same cloud #28e precedent `hydrate_setup` set: a
    private name imported across a package boundary is the worst of both
    worlds."""
    out: list[float] = []
    for m in _NUMBER_RE.finditer(text):
        token = m.group(0).replace("$", "").replace(",", "").replace("%", "")
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def _is_always_allowed(n: float) -> bool:
    """Small integers and 100 read as year labels, counts or percent bases,
    not fabricated figures — rejecting "year 3" or "a 100% weight" here would
    make the check brittle without catching anything real."""
    return n == 100.0 or (n == int(n) and 0 <= n <= 30)


def collect_numbers(*sources: dict[str, Any]) -> list[float]:
    """Every numeric value actually placed in the prompt, flattened."""
    out: list[float] = []
    for source in sources:
        for v in source.values():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def first_ungrounded_number(text: str, allowed: list[float]) -> str | None:
    """Public for the same reason `extract_numbers` above is."""
    for n in extract_numbers(text):
        if _is_always_allowed(n):
            continue
        if any(abs(n - a) <= max(abs(a) * 0.02, 0.5) for a in allowed):
            continue
        return f"{n:g}"
    return None


# --- schema ------------------------------------------------------------


def validate_interpretation(
    payload: dict[str, Any], allowed_numbers: list[float]
) -> dict[str, Any]:
    """Reject rather than coerce — the same stance as `validate_thesis`."""
    if not isinstance(payload, dict):
        raise ValuationNarrativeValidationError(
            f"expected a JSON object, got {type(payload).__name__}"
        )

    missing = REQUIRED_KEYS - payload.keys()
    extra = payload.keys() - REQUIRED_KEYS
    if missing:
        raise ValuationNarrativeValidationError(f"missing keys: {sorted(missing)}")
    if extra:
        raise ValuationNarrativeValidationError(
            f"unexpected keys: {sorted(extra)} — this schema carries no score, "
            "no rating, and no numeric field of its own"
        )

    summary = payload["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValuationNarrativeValidationError("summary must be a non-empty string")
    if len(summary) > MAX_PROSE:
        raise ValuationNarrativeValidationError(
            f"summary is {len(summary)} chars, max {MAX_PROSE}"
        )
    s_sentences = ai_thesis.count_sentences(summary)
    if not MIN_SUMMARY_SENTENCES <= s_sentences <= MAX_SUMMARY_SENTENCES:
        raise ValuationNarrativeValidationError(
            f"summary has {s_sentences} sentences, needs "
            f"{MIN_SUMMARY_SENTENCES} to {MAX_SUMMARY_SENTENCES}"
        )

    note = payload["sensitivity_note"]
    if not isinstance(note, str) or not note.strip():
        raise ValuationNarrativeValidationError(
            "sensitivity_note must be a non-empty string"
        )
    if len(note) > MAX_PROSE:
        raise ValuationNarrativeValidationError(
            f"sensitivity_note is {len(note)} chars, max {MAX_PROSE}"
        )
    n_sentences = ai_thesis.count_sentences(note)
    if not MIN_NOTE_SENTENCES <= n_sentences <= MAX_NOTE_SENTENCES:
        raise ValuationNarrativeValidationError(
            f"sensitivity_note has {n_sentences} sentences, needs "
            f"{MIN_NOTE_SENTENCES} to {MAX_NOTE_SENTENCES}"
        )

    for field, text in (("summary", summary), ("sensitivity_note", note)):
        bad = first_ungrounded_number(text, allowed_numbers)
        if bad is not None:
            raise ValuationNarrativeValidationError(
                f"{field} mentions {bad!r}, which is not one of the numbers "
                "you were given above — reference only the supplied figures"
            )

    return {"summary": summary.strip(), "sensitivity_note": note.strip()}


# --- the evidence put in front of the model --------------------------------


def build_prompt(
    code: str, model_kind: str, inputs: dict[str, Any], computed: dict[str, Any]
) -> str:
    lines = [
        f"TICKER: {code}",
        f"MODEL: {_MODEL_LABELS.get(model_kind, model_kind)}",
        "",
        "INPUTS YOU WERE GIVEN:",
    ]
    for key, value in inputs.items():
        if value is not None:
            lines.append(f"  {key}: {value}")

    lines.append("")
    lines.append("COMPUTED RESULT YOU WERE GIVEN:")
    for key, value in computed.items():
        if value is not None:
            lines.append(f"  {key}: {value}")

    lines.append("")
    lines.append("Produce the JSON object now, using only the numbers above.")
    return "\n".join(lines)


def generate_interpretation(
    *,
    code: str,
    model_kind: str,
    inputs: dict[str, Any],
    computed: dict[str, Any],
    model: str | None = None,
    timeout: float = VALUATION_TIMEOUT,
    client: Any = None,
    max_retries: int = VALUATION_MAX_RETRIES,
) -> dict[str, Any]:
    """One validated interpretation. The caller owns the LLM slot.

    Nothing is written to the database — see the module docstring on why an
    interpretation of one moment's slider positions has no business in the
    RAG corpus.
    """
    model = model or ollama_models.active_model()
    prompt = build_prompt(code, model_kind, inputs, computed)
    allowed_numbers = collect_numbers(inputs, computed)

    result = llm_json.generate_validated_json(
        client if client is not None else llm_json.client(timeout),
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        validate=lambda raw: validate_interpretation(
            ai_thesis.extract_json(raw), allowed_numbers
        ),
        subject=code,
        label="valuation interpretation",
        correction_hint=(
            "exactly the two required keys, and every number you mention "
            "must be one you were given above"
        ),
        transport_error=ValuationNarrativeError,
        exhausted_error=ValuationNarrativeValidationError,
        max_retries=max_retries,
    )
    logger.info("valuation interpretation generated for %s (%s model)", code, model_kind)
    return {"code": code, "model_kind": model_kind, "model": model, **result}
