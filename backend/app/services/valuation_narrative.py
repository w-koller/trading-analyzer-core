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

Large amounts are shown with their scale as well ("$127,006,000,000 \
(about $127.01 billion)"); quote whichever form reads better.

Respond with a single JSON object and nothing else, with exactly these keys:
  "summary"          - 2 to 4 sentences on what is driving the verdict
  "sensitivity_note" - 1 to 2 sentences naming the single input the result \
is most sensitive to. Where a break-even figure is given (the growth rate or \
discount rate at which value equals the market price), use it to say how \
far that input would have to move to flip the verdict. Where none is given, \
say which input matters most in words — never estimate a break-even \
yourself."""


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


# How each field reads to a person, and in what unit. The prompt used to
# print Python's raw float text — `fcf: 69987000000.0` — and the fidelity
# check then rejected the model for saying "$70 billion", because 70 was not
# a number it had been given (cloud #52). Same bug class as cloud #38: the
# numbers a model may cite are the ones the prompt TEXT says, and the text
# should say them the way a reader would.
_FIELDS: dict[str, tuple[str, str]] = {
    "fcf": ("free cash flow, last twelve months", "money"),
    "cash": ("cash and short-term investments", "money"),
    "debt": ("total debt", "money"),
    "assets": ("total assets", "money"),
    "liabilities": ("total liabilities", "money"),
    "preferred": ("preferred stock", "money"),
    "minority_interest": ("minority interest", "money"),
    "shares_outstanding": ("shares outstanding", "shares"),
    "forecast_years": ("forecast period", "years"),
    "growth_rate": ("growth rate a year", "rate"),
    "discount_rate": ("discount rate / required return", "rate"),
    "terminal_growth": ("growth after the forecast, forever", "rate"),
    "dividend": ("dividend per share", "per_share"),
    "use_next_year": ("dividend basis", "basis"),
    "intrinsic_value": ("intrinsic value per share", "per_share"),
    "market_price": ("market price (last close)", "per_share"),
    "target_buy_price": ("target buy price", "per_share"),
    "upside_pct": ("value above (+) or below (-) the price", "rate"),
    "margin_of_safety_pct": ("margin of safety", "rate"),
    "sensitivity_min": ("lowest value in the sensitivity table", "per_share"),
    "sensitivity_max": ("highest value in the sensitivity table", "per_share"),
    "implied_growth_rate": ("BREAK-EVEN: the growth rate at which value "
                            "equals the market price", "rate"),
    "breakeven_discount_rate": ("BREAK-EVEN: the discount rate at which "
                                "value equals the market price", "rate"),
    "terminal_value_share_pct": ("share of the value from after the "
                                 "forecast period", "rate"),
    "price_to_nav": ("price as a multiple of net assets per share", "multiple"),
}

_SCALES = ((1e12, "trillion"), (1e9, "billion"), (1e6, "million"))


def _scaled(value: float) -> str | None:
    for size, word in _SCALES:
        if abs(value) >= size:
            return f"{value / size:,.2f} {word}"
    return None


def _render(key: str, value: Any) -> str:
    """One field as a person would read it, keeping the exact figure."""
    unit = _FIELDS.get(key, ("", ""))[1]
    if isinstance(value, bool):
        if unit == "basis":
            return ("next year's expected dividend (D1)" if value
                    else "the last twelve months' dividend (D0)")
        return "yes" if value else "no"
    if not isinstance(value, (int, float)):
        return str(value)
    sign = "-" if value < 0 else ""
    if unit == "money":
        scaled = _scaled(abs(value))
        exact = f"{sign}${abs(value):,.0f}"
        return f"{exact} (about {sign}${scaled})" if scaled else f"{sign}${abs(value):,.2f}"
    if unit == "shares":
        scaled = _scaled(value)
        return f"{value:,.0f}" + (f" (about {scaled})" if scaled else "")
    if unit == "per_share":
        return f"{sign}${abs(value):,.2f}"
    if unit == "rate":
        return f"{value:,.2f}%"
    if unit == "years":
        return f"{value:g} years"
    if unit == "multiple":
        return f"{value:,.2f}x"
    return f"{value:,}"


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
            label = _FIELDS.get(key, (key, ""))[0]
            lines.append(f"  {key} ({label}): {_render(key, value)}")

    lines.append("")
    lines.append("COMPUTED RESULT YOU WERE GIVEN (all computed in code):")
    for key, value in computed.items():
        if value is not None:
            label = _FIELDS.get(key, (key, ""))[0]
            lines.append(f"  {key} ({label}): {_render(key, value)}")

    lines.append("")
    lines.append("Produce the JSON object now, using only the numbers above.")
    return "\n".join(lines)


def allowed_numbers(prompt: str, *sources: dict[str, Any]) -> list[float]:
    """Every number the model may cite: the raw values AND every figure the
    prompt text prints — the rounded "127.01" of "about $127.01 billion"
    is what lets "$127 billion" through. Built from the text for the reason
    cloud #38 recorded: a figure the prompt shows is only the same value as
    the one in the data structure when nothing transformed it on the way.

    A negative figure is allowed as its MAGNITUDE too. People write "39%
    below the price", not "-39% above it" — found by the first live streamed
    run, which rejected exactly that sentence about an upside of -38.98%."""
    numbers = collect_numbers(*sources) + extract_numbers(prompt)
    return numbers + [-n for n in numbers if n < 0]


def prepare_interpretation(
    *,
    code: str,
    model_kind: str,
    inputs: dict[str, Any],
    computed: dict[str, Any],
    max_retries: int = VALUATION_MAX_RETRIES,
) -> dict[str, Any]:
    """Everything `llm_json.generate_validated_json` needs apart from the
    client and the model — the prompt, the validator and the correction
    hint. Shared by the blocking call below and the streamed one
    (`llm_stream.stream_validated_json`), so the two cannot drift into
    validating the same answer differently."""
    prompt = build_prompt(code, model_kind, inputs, computed)
    allowed = allowed_numbers(prompt, inputs, computed)
    return {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": prompt,
        "validate": lambda raw: validate_interpretation(
            ai_thesis.extract_json(raw), allowed),
        "subject": code,
        "label": "valuation interpretation",
        "correction_hint": (
            "exactly the two required keys, and every number you mention "
            "must be one you were given above"
        ),
        "transport_error": ValuationNarrativeError,
        "exhausted_error": ValuationNarrativeValidationError,
        "max_retries": max_retries,
    }


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
    result = llm_json.generate_validated_json(
        client if client is not None else llm_json.client(timeout),
        model=model,
        **prepare_interpretation(code=code, model_kind=model_kind, inputs=inputs,
                                 computed=computed, max_retries=max_retries),
    )
    logger.info("valuation interpretation generated for %s (%s model)", code, model_kind)
    return {"code": code, "model_kind": model_kind, "model": model, **result}
