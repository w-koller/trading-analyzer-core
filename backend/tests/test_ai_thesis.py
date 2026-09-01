"""Checks for ai_thesis validation, retry, and prompt assembly (no live model).

Run from backend/:  .venv/bin/python -m tests.test_ai_thesis

Uses a fake client so every rejection path is exercised in milliseconds —
a live deepseek-r1:32b call costs ~90s, so driving 20 malformed responses
through the real model would take half an hour. The live path is covered
separately by tests.test_ai_thesis_live.
"""

import json
import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="thesis-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services.ai_thesis import (          # noqa: E402
    AIThesis,
    ThesisError,
    ThesisValidationError,
    build_prompt,
    count_sentences,
    extract_json,
    generate_thesis,
    validate_thesis,
)

from tests.harness import check, report  # noqa: E402


def rejects(label, payload, expect_fragment=""):
    try:
        validate_thesis(payload)
        check(label, False, "accepted invalid payload")
    except ThesisValidationError as exc:
        ok = expect_fragment.lower() in str(exc).lower()
        check(label, ok, "" if ok else f"wrong message: {exc}")


GOOD = {
    "conviction_score": 7,
    "trade_direction": "Bullish",
    "reasoning": "Price closed above the 50-day at 179.94. MACD turned positive. The call wall sits 5% above.",
    "suggested_entry": 178.0,
    "suggested_stop": 172.0,
    "suggested_target": 190.0,
    "key_levels_notes": "call wall 190",
}


# --- sentence counting -------------------------------------------------
check("three plain sentences", count_sentences("One. Two. Three.") == 3)
check("decimals are not sentence breaks",
      count_sentences("It closed at 179.94. Then 180.25 held. Done.") == 3,
      f"got {count_sentences('It closed at 179.94. Then 180.25 held. Done.')}")
check("abbreviations are not sentence breaks",
      count_sentences("U.S. markets rallied. Volume rose. It held.") == 3)
check("question and exclamation marks count",
      count_sentences("Is it up? Yes it is! Good.") == 3)
check("missing final period still counts", count_sentences("One. Two. Three") == 3)
check("empty string is zero sentences", count_sentences("") == 0)

# --- the news block separates ticker news from market noise -------------
# The old version padded a short ticker list with unrelated market headlines
# under one flat undated heading, so the model could not tell company news
# from macro, and "RECENT" was an unverified claim.
from app.services.ai_thesis import _news_block                    # noqa: E402

_news = {
    "ticker": [{"title": "ServiceNow beats on cloud", "source_label": "Yahoo",
                "published_at": "2026-08-24T02:00:00+00:00", "age_hours": 6.0,
                "match_basis": "feed_query"}],
    "macro": [{"title": "FOMC holds rates steady", "source_label": "Fed",
               "published_at": "2026-08-24T05:00:00+00:00", "age_hours": 3.0}],
    "window_hours": 72,
}
_block = _news_block(_news, "US.NOW")
check("ticker news is labelled with the ticker", "NEWS ABOUT US.NOW" in _block)
check("macro news is labelled as NOT about the ticker",
      "NOT about this ticker" in _block)
check("the two are separate sections",
      _block.index("NEWS ABOUT") < _block.index("MARKET-WIDE"))
check("every item carries an age", "6h ago" in _block and "3h ago" in _block,
      "a bare date means little to a model with no sense of today")
_empty = _news_block({"ticker": [], "macro": [], "window_hours": 72}, "US.NOW")
check("no ticker news says so explicitly", "(none in the last 72 hours)" in _empty,
      "better than padding with five unrelated market headlines")
check("no ticker news omits the macro heading entirely",
      "MARKET-WIDE" not in _empty)

# Abbreviations must match as words, not as substrings. Without a \b the
# "est." entry swallowed the period in "open interest.", so genuinely
# three-sentence output counted as two and was rejected three times before
# the ticker was abandoned. This really happened to US.TSLA and US.GNRC in
# one scan, and "open interest" is in almost every options thesis.
check("'interest.' is not read as the abbreviation 'est.'",
      count_sentences(
          "Bearish SMA trend with a wide gap suggests downward pressure. "
          "Bullish MACD histogram indicates potential buying interest. "
          "Overbought Bollinger Bands and bearish put/call ratio suggest caution."
      ) == 3,
      "the two tickers this cost in a real scan")
for word, sentence in (
    ("interest", "Options show high open interest. Volume rose. It held."),
    ("Cisco", "It lagged Cisco. Volume rose. It held."),
    ("zinc", "Miners tracked zinc. Volume rose. It held."),
    ("casino", "It moved with the casino. Volume rose. It held."),
    ("suggest", "The indicators suggest. Volume rose. It held."),
):
    check(f"'{word}.' does not swallow a sentence break",
          count_sentences(sentence) == 3, f"got {count_sentences(sentence)}")

check("real abbreviations are still protected",
      count_sentences("Volume hit approx. 40M today. It held. Done.") == 3,
      "the \\b must not break the cases the list exists for")
check("whitespace is zero sentences", count_sentences("   ") == 0)

# --- happy path --------------------------------------------------------
t = validate_thesis(GOOD)
check("valid payload produces AIThesis", isinstance(t, AIThesis))
check("conviction preserved", t.conviction_score == 7)
check("stop/target are floats", isinstance(t.suggested_stop, float))
check("entry is a float", isinstance(t.suggested_entry, float))
check("to_dict is JSON-serialisable", json.loads(json.dumps(t.to_dict())) == t.to_dict())
check("nulls are allowed for stop/target/notes",
      validate_thesis({**GOOD, "suggested_stop": None, "suggested_target": None,
                       "key_levels_notes": None}).suggested_stop is None)
check("null entry is allowed — 'act at the current price' is a real answer",
      validate_thesis({**GOOD, "suggested_entry": None}).suggested_entry is None)

# --- rule #2: reject, never coerce ------------------------------------
rejects("missing key rejected", {k: v for k, v in GOOD.items() if k != "reasoning"},
        "missing required keys")
rejects("extra key rejected", {**GOOD, "ticker": "PLTR"}, "unexpected keys")
rejects("score 0 rejected", {**GOOD, "conviction_score": 0}, "1-10")
rejects("score 11 rejected", {**GOOD, "conviction_score": 11}, "1-10")
rejects("float score rejected, not rounded",
        {**GOOD, "conviction_score": 7.5}, "integer")
rejects("string score rejected, not parsed",
        {**GOOD, "conviction_score": "7"}, "integer")
rejects("bool score rejected", {**GOOD, "conviction_score": True}, "integer")
rejects("lowercase direction rejected, not title-cased",
        {**GOOD, "trade_direction": "bullish"}, "case-sensitive")
rejects("invented direction rejected", {**GOOD, "trade_direction": "Very Bullish"},
        "trade_direction")
rejects("two-sentence reasoning rejected",
        {**GOOD, "reasoning": "One. Two."}, "exactly 3 sentences")
rejects("four-sentence reasoning rejected",
        {**GOOD, "reasoning": "One. Two. Three. Four."}, "exactly 3 sentences")
rejects("empty reasoning rejected", {**GOOD, "reasoning": "   "}, "non-empty")
rejects("string stop rejected, not parsed",
        {**GOOD, "suggested_stop": "172.0"}, "number or null")
rejects("bool target rejected", {**GOOD, "suggested_target": True}, "number or null")
rejects("string entry rejected, not parsed",
        {**GOOD, "suggested_entry": "178.0"}, "number or null")
rejects("bool entry rejected", {**GOOD, "suggested_entry": True}, "number or null")
rejects("missing suggested_entry rejected — the schema is seven keys now",
        {k: v for k, v in GOOD.items() if k != "suggested_entry"},
        "missing required keys")
rejects("over-long notes rejected", {**GOOD, "key_levels_notes": "x" * 501},
        "at most 500")
rejects("non-string notes rejected", {**GOOD, "key_levels_notes": 42}, "string or null")

# Directional coherence: advice that would lose money if followed.
rejects("bullish stop above target rejected",
        {**GOOD, "suggested_stop": 195.0, "suggested_target": 190.0}, "below")
rejects("bearish stop below target rejected",
        {**GOOD, "trade_direction": "Bearish", "suggested_stop": 172.0,
         "suggested_target": 190.0}, "above")
check("bearish with correct ordering accepted",
      validate_thesis({**GOOD, "trade_direction": "Bearish",
                       "suggested_stop": 190.0,
                       "suggested_target": 172.0}).trade_direction == "Bearish")
check("neutral ignores stop/target ordering",
      validate_thesis({**GOOD, "trade_direction": "Neutral",
                       "suggested_stop": 195.0,
                       "suggested_target": 190.0}).trade_direction == "Neutral")

# The entry is held to the same standard as the stop and the target. An entry
# outside its own stop/target band describes a trade that is already lost, or
# already won, at the moment it is opened.
rejects("bullish entry below its own stop rejected",
        {**GOOD, "suggested_entry": 170.0}, "below")
rejects("bullish entry above its own target rejected",
        {**GOOD, "suggested_entry": 195.0}, "below")
rejects("entry equal to the stop rejected — the comparison is strict",
        {**GOOD, "suggested_entry": 172.0}, "below")
rejects("bearish entry above its own stop rejected",
        {**GOOD, "trade_direction": "Bearish", "suggested_stop": 190.0,
         "suggested_target": 172.0, "suggested_entry": 195.0}, "above")
rejects("bearish entry below its own target rejected",
        {**GOOD, "trade_direction": "Bearish", "suggested_stop": 190.0,
         "suggested_target": 172.0, "suggested_entry": 170.0}, "above")
check("bearish with all three ordered accepted",
      validate_thesis({**GOOD, "trade_direction": "Bearish",
                       "suggested_stop": 190.0, "suggested_entry": 178.0,
                       "suggested_target": 172.0}).suggested_entry == 178.0)
check("neutral ignores entry ordering too",
      validate_thesis({**GOOD, "trade_direction": "Neutral",
                       "suggested_entry": 999.0}).suggested_entry == 999.0)
# Each pair is checked independently, so naming only two of the three levels
# is still held to the ordering of the pair that WAS named.
rejects("entry above target rejected even with no stop",
        {**GOOD, "suggested_stop": None, "suggested_entry": 195.0}, "below")
check("a null level skips its pairs rather than defaulting",
      validate_thesis({**GOOD, "suggested_entry": None,
                       "suggested_stop": None}).suggested_stop is None)

# --- parsing the wrappers the live model actually emits ---------------
check("bare JSON parses", extract_json(json.dumps(GOOD))["conviction_score"] == 7)
check("```json fence stripped",
      extract_json("```json\n" + json.dumps(GOOD) + "\n```")["conviction_score"] == 7)
check("bare ``` fence stripped",
      extract_json("```\n" + json.dumps(GOOD) + "\n```")["conviction_score"] == 7)
check("<think> block stripped",
      extract_json("<think>hmm, let me consider {not: json}</think>\n"
                   + json.dumps(GOOD))["conviction_score"] == 7)
check("leading prose tolerated",
      extract_json("Here is the analysis:\n" + json.dumps(GOOD))["conviction_score"] == 7)

for label, bad in (("empty response", ""), ("no object", "I cannot help with that"),
                   ("truncated JSON", '{"conviction_score": 7,'),
                   ("array not object", "[1, 2, 3]")):
    try:
        extract_json(bad)
        check(f"{label} rejected", False, "parsed anyway")
    except ThesisValidationError:
        check(f"{label} rejected", True)


# --- retry behaviour (rule #2: reject/retry, informed not blind) ------
class FakeClient:
    """Replays scripted responses and records the messages it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        body = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body

        class R:
            choices = [type("C", (), {"message": type("M", (), {"content": body})()})()]
        return R()


ARGS = dict(
    code="US.PLTR", market="US", feature_vector=[0.1] * 13,
    indicators={"close": 179.94, "sma_trend": "bullish"}, walls=None,
    data_as_of="2026-08-23T13:00:00+00:00", is_delayed_data=False,
)

fc = FakeClient([json.dumps(GOOD)])
thesis, similar = generate_thesis(**ARGS, client=fc)
check("first-attempt success returns thesis", thesis.conviction_score == 7)
check("only one call made when valid", len(fc.calls) == 1)
check("similar setups returned for the audit trail", similar == [])

# Malformed then valid: must retry and succeed.
fc = FakeClient(['{"conviction_score": 42}', json.dumps(GOOD)])
thesis, _ = generate_thesis(**ARGS, client=fc)
check("retries after malformed output", thesis.conviction_score == 7)
check("second attempt actually made", len(fc.calls) == 2)
check("retry feeds the model its own bad output",
      any(m["role"] == "assistant" for m in fc.calls[1]))
check("retry names the specific fault",
      any("rejected" in m["content"] for m in fc.calls[1] if m["role"] == "user"))

# Exhausting retries must raise, not return a coerced best-effort object.
fc = FakeClient(["nope", "still nope", "nope again"])
try:
    generate_thesis(**ARGS, client=fc, max_retries=3)
    check("gives up after max_retries", False, "returned a thesis anyway")
except ThesisValidationError as exc:
    check("gives up after max_retries", "3 attempts" in str(exc))
check("exactly max_retries attempts made", len(fc.calls) == 3)

# A transport failure is a different error class from a schema failure.
fc = FakeClient([ConnectionError("connection refused")])
try:
    generate_thesis(**ARGS, client=fc)
    check("transport failure raises ThesisError", False)
except ThesisError as exc:
    check("transport failure raises ThesisError",
          not isinstance(exc, ThesisValidationError) and "failed" in str(exc))

# --- rule #3: RAG retrieval happens before the model call -------------
calls_order: list[str] = []
_real_get_similar = db.get_similar_setups


def spy(*a, **kw):
    calls_order.append("rag")
    return _real_get_similar(*a, **kw)


db.get_similar_setups = spy
import app.services.ai_thesis as ai_thesis_mod   # noqa: E402
ai_thesis_mod.db.get_similar_setups = spy


class OrderedFake(FakeClient):
    def create(self, **kwargs):
        calls_order.append("llm")
        return super().create(**kwargs)


generate_thesis(**ARGS, client=OrderedFake([json.dumps(GOOD)]))
check("RAG runs before the LLM call", calls_order == ["rag", "llm"], str(calls_order))
db.get_similar_setups = _real_get_similar
ai_thesis_mod.db.get_similar_setups = _real_get_similar

# --- prompt assembly ---------------------------------------------------
p = build_prompt(
    code="AU.BHP", market="AU",
    indicators={"close": 45.2, "sma_trend": "bearish", "warnings": ["short history"]},
    walls={"has_walls": True, "expiry": "2026-09-18", "call_wall": 50.0,
           "call_wall_oi": 100, "call_wall_volume": 20,
           "call_wall_distance_pct": 10.6, "put_wall": 42.0, "put_wall_oi": 80,
           "put_wall_volume": 10, "put_wall_distance_pct": -7.1,
           "put_call_oi_ratio": 0.8, "put_call_volume_ratio": 0.5},
    similar_setups=[{"code": "AU.BHP", "created_at": "2026-01-05", "similarity": 0.91,
                     "trade_direction": "Bullish", "conviction_score": 8,
                     "outcome": {"pnl_pct": -4.2, "hold_time_hours": 30.0,
                                 "exit_reason": "stop"}}],
    data_as_of="2026-08-23T05:45:00+00:00", is_delayed_data=True,
    news={"ticker": [{"title": "BHP reports record iron ore shipments",
                      "source_label": "Yahoo Finance",
                      "published_at": "2026-08-23T12:00:00+00:00",
                      "age_hours": 6.0, "match_basis": "feed_query"}],
          "macro": [], "window_hours": 72}, session="open",
)
check("rule #7: delayed data is stated in the prompt", "DELAYED" in p)
check("delayed prompt carries the as-of timestamp", "2026-08-23T05:45:00+00:00" in p)
check("prompt forbids recomputation", "do not recompute" in p.lower())
check("RAG outcomes reach the prompt", "-4.20%" in p and "exit: stop" in p)
check("news reaches the prompt", "record iron ore" in p)
check("indicator warnings reach the prompt", "short history" in p)
check("wall volume component shown", "volume 20" in p)

live = build_prompt(code="US.PLTR", market="US", indicators={"close": 179.94},
                    walls=None, similar_setups=[],
                    data_as_of="2026-08-23T13:00:00+00:00", is_delayed_data=False)
check("real-time data is labelled real-time", "real-time" in live)
check("empty RAG history is stated, not silently omitted",
      "No comparable historical setups" in live)
check("missing chain is stated", "No options chain data" in live)

report("ai_thesis")
