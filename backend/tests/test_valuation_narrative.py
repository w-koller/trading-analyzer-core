"""Checks for the valuation calculator's "brief me" narrative layer.

Run from backend/:  .venv/bin/python -m tests.test_valuation_narrative

Two rules carry this suite, the same two `test_sector_narrative.py` carries
for its own module: the schema must never be able to carry a number, and a
number the model DOES mention in prose must be traceable back to something
it was actually given — the numeric-fidelity check that stands in for a
fixed citation whitelist here, since there is no fixed set of citable
strings for a prose explanation of arithmetic.

Offline throughout: a fake LLM client, no network and no model, and nothing
is persisted (there is no table to check against — see the module docstring
on why an interpretation of one moment's inputs is never stored).
"""

import json

from app.services import valuation_narrative as vn
from tests.harness import check, check_eq, report

INPUTS = {
    "fcf": 500.0,
    "forecast_years": 5,
    "growth_rate": 8.0,
    "discount_rate": 9.0,
    "terminal_growth": 2.5,
    "cash": 1200.0,
    "debt": 800.0,
    "shares_outstanding": 100.0,
}
COMPUTED = {
    "intrinsic_value": 142.35,
    "verdict": "undervalued",
    "market_price": 120.0,
    "target_buy_price": 106.76,
    "upside_pct": 18.6,
}
ALLOWED = vn.allowed_numbers(vn.build_prompt("US.TEST", "dcf", INPUTS, COMPUTED),
                             INPUTS, COMPUTED)

GOOD = {
    "summary": "At a 9% discount rate against 8% near-term growth, the model "
                "puts intrinsic value at $142.35 a share, well above the "
                "$120.00 market price. That gap of 18.6% is what the "
                "undervalued read comes from.",
    "sensitivity_note": "The result is most sensitive to the 2.5% terminal "
                        "growth assumption, which drives most of the "
                        "present value here.",
}


def payload(**over):
    out = dict(GOOD)
    out.update(over)
    return out


def rejects(label, bad, fragment="", allowed=ALLOWED):
    try:
        vn.validate_interpretation(bad, allowed)
    except vn.ValuationNarrativeValidationError as exc:
        return check(label, fragment in str(exc), f"{exc}")
    return check(label, False, "accepted when it should have been rejected")


# --- the happy path ---------------------------------------------------------

ok = vn.validate_interpretation(payload(), ALLOWED)
check_eq("a valid object passes", ok["summary"], GOOD["summary"].strip())
check_eq("...and sensitivity_note comes back stripped",
         ok["sensitivity_note"], GOOD["sensitivity_note"].strip())


# --- the schema carries NO number, and cannot be made to --------------------

rejects("a confidence_score key is rejected outright",
        payload(confidence_score=7), "unexpected keys")
rejects("...and so is a numeric verdict field",
        payload(intrinsic_value=142.35), "unexpected keys")
try:
    vn.validate_interpretation(payload(confidence_score=7), ALLOWED)
except vn.ValuationNarrativeValidationError as exc:
    check("the extra-key message names what this schema refuses to carry",
          "no score" in str(exc) and "no rating" in str(exc), str(exc))

check_eq("exactly two required keys, both prose",
         vn.REQUIRED_KEYS, frozenset({"summary", "sensitivity_note"}))

rejects("a missing key is rejected",
        {"summary": GOOD["summary"]}, "missing keys")
rejects("an empty summary is rejected",
        payload(summary="   "), "non-empty string")
rejects("a one-sentence summary is rejected (needs 2-4)",
        payload(summary="It is undervalued."), "sentences")
rejects("a five-sentence summary is rejected (needs 2-4)",
        payload(summary="One. Two. Three. Four. Five."), "sentences")
rejects("a three-sentence sensitivity_note is rejected (needs 1-2)",
        payload(sensitivity_note="One. Two. Three."), "sentences")


# --- numeric fidelity: the mechanism this schema needs ----------------------

rejects("a fabricated dollar figure is rejected",
        payload(summary="Fair value comes in around $999.00 a share, well "
                        "above market. That is a big gap."),
        "not one of the numbers")
rejects("a fabricated percentage is rejected",
        payload(sensitivity_note="A 47% swing in growth would flip this. "
                                 "That is the key lever."),
        "not one of the numbers")

check("a number that matches a given figure within rounding is accepted",
      vn.first_ungrounded_number("about $142 a share", ALLOWED) is None,
      "142 vs the given 142.35 is well inside a rounding tolerance")
check("a given figure quoted exactly is accepted",
      vn.first_ungrounded_number("$120.00 market price", ALLOWED) is None)
check("small integers read as year/count labels, not fabricated figures",
      vn.first_ungrounded_number("in year 3 of the forecast", ALLOWED) is None)
check("100 reads as a percent base, not a fabricated figure",
      vn.first_ungrounded_number("scaled against 100", ALLOWED) is None)
check("an unrelated large number is still caught",
      vn.first_ungrounded_number("a $50000 valuation", ALLOWED) is not None)


# --- the prompt actually carries the given numbers --------------------------

prompt = vn.build_prompt("US.TEST", "dcf", INPUTS, COMPUTED)
check("the prompt names the ticker", "US.TEST" in prompt)
check("the prompt names the model", "Discounted Cash Flow" in prompt)
# Checked NUMERICALLY, not as `str(v) in prompt`: the prompt prints figures
# the way a reader would ("$1,200.00", "8.00%"), not as Python float text.
_in_prompt = vn.extract_numbers(prompt)
check("every input value appears in the prompt",
      all(any(abs(n - float(v)) < 1e-9 for n in _in_prompt) for v in INPUTS.values()))
check("the computed intrinsic value appears in the prompt",
      any(abs(n - COMPUTED["intrinsic_value"]) < 1e-9 for n in _in_prompt))
check("a None-valued field is omitted rather than printed as 'None'",
      "None" not in vn.build_prompt(
          "US.TEST", "ggm",
          {"dividend": 2.0, "growth_rate": 3.0, "discount_rate": 8.0},
          {**COMPUTED, "sensitivity_min": None, "sensitivity_max": None}))


# --- real-company scale: the fidelity bug a raw float prompt caused ---------
# cloud #52: `fcf: 127006000000.0` in the prompt, "$127 billion" in the
# answer, and 127 matched nothing given — so a correct sentence was rejected
# and retried. The prompt now prints the scale too, and the numbers a model
# may cite are built from the prompt's own text.
BIG_INPUTS = {**INPUTS, "fcf": 127_006_000_000.0, "cash": 22_443_000_000.0,
              "debt": 33_366_000_000.0, "shares_outstanding": 24_100_000_000.0}
BIG_COMPUTED = {**COMPUTED, "implied_growth_rate": 11.234,
                "breakeven_discount_rate": 10.46, "terminal_value_share_pct": 71.8,
                "margin_of_safety_pct": 25.0}
big_prompt = vn.build_prompt("US.NVDA", "dcf", BIG_INPUTS, BIG_COMPUTED)
BIG_ALLOWED = vn.allowed_numbers(big_prompt, BIG_INPUTS, BIG_COMPUTED)
check("a large amount is printed with commas and its scale",
      "$127,006,000,000 (about $127.01 billion)" in big_prompt, big_prompt.splitlines()[4])
check("shares print their scale too",
      "24,100,000,000 (about 24.10 billion)" in big_prompt)
check("'$127 billion' is accepted against a 127,006,000,000 input",
      vn.first_ungrounded_number("free cash flow of $127 billion", BIG_ALLOWED) is None)
check("...and so is '$127.01 billion'",
      vn.first_ungrounded_number("about $127.01 billion", BIG_ALLOWED) is None)
check("...but not against the old raw-value-only list, which is the bug",
      vn.first_ungrounded_number("free cash flow of $127 billion",
                                 vn.collect_numbers(BIG_INPUTS, BIG_COMPUTED)) is not None)
check("a supplied break-even figure may be cited",
      vn.first_ungrounded_number("the verdict flips near 11.2% growth", BIG_ALLOWED) is None)
check("...and the break-even line says what it is",
      "BREAK-EVEN" in big_prompt and "11.23%" in big_prompt)
check("a negative figure may be quoted as its magnitude ('39% below' for -38.98)",
      vn.first_ungrounded_number(
          "the value sits 39% below the price",
          vn.allowed_numbers("", {"upside_pct": -38.98})) is None)
check("...which does not open the door to an unrelated positive number",
      vn.first_ungrounded_number(
          "the value sits 64% below the price",
          vn.allowed_numbers("", {"upside_pct": -38.98})) is not None)
check("an invented figure is still rejected at company scale",
      vn.first_ungrounded_number("a fair value of $912 a share", BIG_ALLOWED) is not None)
check("the system prompt no longer asks the model to estimate a break-even",
      "roughly how much it would need to move" not in vn.SYSTEM_PROMPT
      and "never estimate a break-even" in vn.SYSTEM_PROMPT)
ggm_prompt = vn.build_prompt("US.KO", "ggm",
                             {"dividend": 2.06, "use_next_year": False,
                              "growth_rate": 3.0, "discount_rate": 8.0}, COMPUTED)
check("the GGM dividend basis reaches the model in words, not as 'False'",
      "the last twelve months' dividend (D0)" in ggm_prompt and "False" not in ggm_prompt)

prepared = vn.prepare_interpretation(code="US.TEST", model_kind="dcf",
                                     inputs=INPUTS, computed=COMPUTED)
check_eq("prepare_interpretation carries the same prompt build_prompt makes",
         prepared["user_prompt"], vn.build_prompt("US.TEST", "dcf", INPUTS, COMPUTED))
check("...and a validator that accepts the good answer",
      prepared["validate"](json.dumps(payload()))["summary"] == GOOD["summary"].strip())


# --- generation: retry-and-correct, same shape as every other schema here --

class FakeClient:
    """Returns queued responses in order, recording what it was asked."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                body = outer.responses.pop(0)
                text = body if isinstance(body, str) else json.dumps(body)
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": text, "reasoning": None})()
                })()]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


fake = FakeClient(payload())
out = vn.generate_interpretation(
    code="US.TEST", model_kind="dcf", inputs=INPUTS, computed=COMPUTED,
    model="test-model", client=fake,
)
check_eq("generation returns a validated interpretation",
         out["summary"], GOOD["summary"].strip())
check_eq("...records which model wrote it", out["model"], "test-model")
check_eq("...and which ticker/model_kind it was about",
         (out["code"], out["model_kind"]), ("US.TEST", "dcf"))
check("nothing here writes to a database — the result carries no id",
      "id" not in out)

# The retry loop must hand the fault back and accept a corrected answer.
retry = FakeClient(
    payload(summary="Fair value comes in around $999.00 a share here. "
                    "That is well above market."),
    payload(),
)
out2 = vn.generate_interpretation(
    code="US.TEST", model_kind="dcf", inputs=INPUTS, computed=COMPUTED,
    model="test-model", client=retry,
)
check_eq("a fabricated number is retried, not accepted", len(retry.calls), 2)
check("...and the correction turn carries the specific fault",
      any("not one of the numbers" in str(m.get("content", ""))
          for m in retry.calls[1]["messages"]),
      "a generic 'try again' teaches the model nothing")
check_eq("...and the corrected answer is returned",
         out2["summary"], GOOD["summary"].strip())

stubborn = FakeClient(
    payload(summary="Fair value is $999.00, far above market."),
    payload(summary="Still $999.00, way above market."),
    payload(summary="Definitely $999.00 here."),
)
try:
    vn.generate_interpretation(
        code="US.TEST", model_kind="dcf", inputs=INPUTS, computed=COMPUTED,
        model="test-model", client=stubborn,
    )
    check("an uncorrectable response raises", False)
except vn.ValuationNarrativeValidationError:
    check("an uncorrectable response raises rather than degrading", True,
          f"{len(stubborn.calls)} attempts")

report("valuation narrative")
