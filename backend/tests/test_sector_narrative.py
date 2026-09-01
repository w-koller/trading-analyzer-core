"""Checks for the sector narrative layer.

Run from backend/:  .venv/bin/python -m tests.test_sector_narrative

Two rules carry this suite. First, the schema must never be able to carry a
number — a model-authored score sitting beside a Python-computed one would
look comparable when it is nothing of the kind (decisions #52). Second, a
cited headline must have been in the prompt, which is what makes fabricating
a source impossible rather than merely discouraged.

Offline: temp database, a fake LLM client, no network and no model.
"""

import json
import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="sector-narrative-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.services import sector_narrative as sn  # noqa: E402
from tests.harness import check, check_eq, report  # noqa: E402

TITLES = {
    "Chipmaker guides revenue below consensus",
    "Software vendor lifts annual outlook",
    "Fed holds rates steady",
}

GOOD = {
    "headline": "Software strength came with a semiconductor guidance cut.",
    "candidate_driver": "A large vendor lifted its annual outlook. A chipmaker "
                        "guided below consensus on the same day.",
    "supporting_headlines": ["Software vendor lifts annual outlook"],
    "contradicts": "Nothing in the supplied commentary cuts the other way.",
    "confidence_label": "news explains it",
}


def payload(**over):
    out = dict(GOOD)
    out.update(over)
    return out


def rejects(label, bad, fragment=""):
    try:
        sn.validate_narrative(bad, TITLES)
    except sn.NarrativeValidationError as exc:
        return check(label, fragment in str(exc), f"{exc}")
    return check(label, False, "accepted when it should have been rejected")


# --- the happy path --------------------------------------------------------

ok = sn.validate_narrative(payload(), TITLES)
check_eq("a valid object passes", ok["confidence_label"], "news explains it")
check_eq("...and is returned stripped",
         ok["headline"], GOOD["headline"].strip())
check_eq("...with its citations intact",
         ok["supporting_headlines"], ["Software vendor lifts annual outlook"])


# --- the schema carries NO number, and cannot be made to ------------------

rejects("a rotation_score key is rejected outright",
        payload(rotation_score=0.7), "unexpected keys")
rejects("...and so is a conviction_score",
        payload(conviction_score=7), "unexpected keys")
rejects("...and a suggested_entry",
        payload(suggested_entry=205.0), "unexpected keys")
try:
    sn.validate_narrative(payload(rotation_score=0.7), TITLES)
except sn.NarrativeValidationError as exc:
    check("the extra-key message names what this schema refuses to carry",
          "no score" in str(exc) and "no rating" in str(exc), str(exc))

check("no key in the schema is numeric",
      all(k in {"headline", "candidate_driver", "supporting_headlines",
                "contradicts", "confidence_label"} for k in sn.REQUIRED_KEYS)
      and len(sn.REQUIRED_KEYS) == 5)
check("confidence is three fixed words, not a rating",
      all(isinstance(v, str) and " " in v for v in sn.CONFIDENCE_LABELS),
      "a 1-10 field would be averaged, plotted, and set beside a conviction")

rejects("a missing key is rejected",
        {k: v for k, v in GOOD.items() if k != "contradicts"}, "missing keys")


# --- the rule that matters most: citations must exist --------------------

rejects("a fabricated headline is rejected",
        payload(supporting_headlines=["Analysts turn bullish on rare earths"]),
        "was not in the headlines you were given")

try:
    sn.validate_narrative(
        payload(supporting_headlines=["Totally invented headline"]), TITLES)
except sn.NarrativeValidationError as exc:
    check("...with the offending string quoted back for the correction turn",
          "Totally invented headline" in str(exc), str(exc))

rejects("a PARAPHRASE of a real headline is still rejected",
        payload(supporting_headlines=["Software vendor raises outlook"]),
        "was not in the headlines")
check("an exact citation is accepted",
      sn.validate_narrative(
          payload(supporting_headlines=sorted(TITLES)[:2]), TITLES
      )["supporting_headlines"] == sorted(TITLES)[:2])
check("citing nothing is legal",
      sn.validate_narrative(
          payload(supporting_headlines=[],
                  confidence_label="no news explains it"), TITLES
      )["supporting_headlines"] == [])
rejects("more than five citations is rejected",
        payload(supporting_headlines=sorted(TITLES) * 2), "max 5")
rejects("a non-string citation is rejected",
        payload(supporting_headlines=[42]), "non-empty strings")


# --- the coherence rule ---------------------------------------------------

rejects("'news explains it' with no citation is rejected",
        payload(supporting_headlines=[], confidence_label="news explains it"),
        "cite the headlines")
check("'no news explains it' with no citation is fine",
      sn.validate_narrative(
          payload(supporting_headlines=[],
                  confidence_label="no news explains it"), TITLES
      )["confidence_label"] == "no news explains it",
      "the honest answer must never be the hard one to give")


# --- the remaining field rules -------------------------------------------

rejects("a one-sentence driver is rejected",
        payload(candidate_driver="Software did well."), "1 sentences")
rejects("a four-sentence driver is rejected",
        payload(candidate_driver="One. Two. Three. Four."), "4 sentences")
check("a three-sentence driver is accepted",
      sn.validate_narrative(
          payload(candidate_driver="One thing. Two things. Three things."),
          TITLES)["candidate_driver"].endswith("Three things."))
check("a decimal does not count as a sentence break",
      sn.validate_narrative(
          payload(candidate_driver="The sector moved 3.5% today. Volume followed."),
          TITLES) is not None,
      "count_sentences protects decimals — a narrative quotes numbers")

rejects("a wrong-cased label is rejected",
        payload(confidence_label="News Explains It"), "exactly one of")
rejects("an invented label is rejected",
        payload(confidence_label="probably"), "exactly one of")
rejects("an over-long headline is rejected",
        payload(headline="x" * (sn.MAX_HEADLINE + 1)), "max 160")
rejects("an empty headline is rejected", payload(headline="   "), "non-empty")
rejects("a non-dict payload is rejected", ["not", "a", "dict"], "expected a JSON object")


# ==========================================================================
# The news side, and generation end to end with a fake client
# ==========================================================================

def seed_world():
    db.upsert_sector_plates([
        {"plate_code": "US.LIST2015", "market": "US", "plate_name": "Semiconductors",
         "plate_class": "INDUSTRY", "plate_id": "L2015", "sector_group": "Semiconductors"},
        {"plate_code": "US.LIST9999", "market": "US", "plate_name": "Orphan Sector",
         "plate_class": "CONCEPT", "plate_id": "L9999", "sector_group": "Orphan"},
    ])
    for code, name in [("US.NVDA", "NVIDIA"), ("US.AMD", "AMD")]:
        db.upsert_watchlist_ticker(code=code, name=name, market="US")
    db.replace_plate_members("US.LIST2015", [
        {"code": "US.NVDA", "stock_name": "NVIDIA"},
        {"code": "US.AMD", "stock_name": "AMD"},
        {"code": "US.OFFWL", "stock_name": "Not on the watchlist"},
    ])
    # The orphan holds only off-watchlist names, so nothing links to it.
    db.replace_plate_members("US.LIST9999", [
        {"code": "US.NOBODY", "stock_name": "Nobody"}])
    db.insert_news_articles([
        {"dedup_key": "k1", "url": "https://x/1", "title": "Chipmaker guides revenue below consensus",
         "title_norm": "chipmaker guides revenue below consensus", "summary": "",
         "feed_key": "f", "source_label": "Reuters", "category": "shocks",
         "published_at": db.now_iso(), "published_estimated": 0,
         "codes": [("US.NVDA", "company_name")]},
        {"dedup_key": "k2", "url": "https://x/2", "title": "Fed holds rates steady",
         "title_norm": "fed holds rates steady", "summary": "",
         "feed_key": "f", "source_label": "CNBC", "category": "macro",
         "published_at": db.now_iso(), "published_estimated": 0, "codes": []},
    ])
    db.upsert_rotation_scores([
        {"plate_code": "US.LIST2015", "as_of_date": "2026-08-28", "window_days": 5,
         "score": 0.62, "components": {"rel_return": 0.9}, "rel_return_pct": 4.2,
         "turnover_thrust": 0.31, "breadth": 0.4, "persistence": 0.6,
         "sessions_used": 10, "constituents": 72, "coverage": 1.0, "sufficient": True},
        {"plate_code": "US.LIST9999", "as_of_date": "2026-08-28", "window_days": 5,
         "score": -0.55, "components": {"rel_return": -0.8}, "rel_return_pct": -3.1,
         "turnover_thrust": -0.2, "sessions_used": 10, "constituents": 0,
         "coverage": 0.75, "sufficient": False},
    ])


seed_world()

news = sn.gather_news("US.LIST2015")
check_eq("ticker news reaches a sector through its watchlist constituents",
         [n["title"] for n in news["ticker"]],
         ["Chipmaker guides revenue below consensus"])
check_eq("macro news comes along as context",
         [n["title"] for n in news["macro"]], ["Fed holds rates steady"])
check("an article shown as ticker news is NOT repeated under macro",
      "Chipmaker guides revenue below consensus" not in
      [n["title"] for n in news["macro"]],
      "'shocks' qualifies as both; showing it twice reads as two independent "
      "pieces of evidence corroborating each other")
check_eq("...and the watchlist join is reported",
         sorted(news["watchlist_codes"]), ["US.AMD", "US.NVDA"])

orphan = sn.gather_news("US.LIST9999")
check_eq("a sector with no watchlist names gets NO ticker news", orphan["ticker"], [])
check_eq("...and no watchlist codes", orphan["watchlist_codes"], [])
check("...but still gets macro context", len(orphan["macro"]) == 2,
      "silence about the sector is structural, not evidence nothing happened; "
      "with no ticker news to dedupe against, both macro-category rows show")

titles = sn.allowed_titles(news)
check("the citation whitelist is exactly what the prompt showed",
      titles == {"Chipmaker guides revenue below consensus", "Fed holds rates steady"},
      f"{titles}")

plate = {p["plate_code"]: p for p in db.get_sector_universe()}["US.LIST2015"]
score = db.get_rotation_board(window_days=5)[0]
prompt = sn.build_prompt(plate, score, news)
check("the prompt states the computed score as settled fact",
      "+0.62" in prompt and "not by you" in prompt,
      "the model explains a number; it never produces one")
check("...names the sector and the window", "Semiconductors" in prompt and "sessions" in prompt)
check("...quotes every citable headline verbatim",
      all(t in prompt for t in titles))
check("...labels ticker and macro news SEPARATELY, never merged",
      "MARKET-WIDE HEADLINES" in prompt and "HEADLINES ABOUT COMPANIES" in prompt,
      "decisions #46: a flat block let macro read as sector news")
check("...dates every item", "ago)" in prompt)
check("...states how much of the SECTOR the news actually covers",
      "of this sector's 3 constituents" in prompt, prompt.split("\n")[8:14])
# The warning fires only when the news covers less than a third of the
# sector. The fixture above is 3 constituents with 1 covered — exactly a
# third, so it correctly does NOT warn. Widen the sector to make it a sliver,
# which is the live shape: 1 of 14.
check("a third of the sector covered does NOT trigger the sliver warning",
      "not the whole sector" not in prompt,
      "1 of 3 is thin, but it is not a sliver")
db.replace_plate_members("US.LIST2015", [
    {"code": "US.NVDA", "stock_name": "NVIDIA"},
    {"code": "US.AMD", "stock_name": "AMD"},
] + [{"code": f"US.W{i:02d}", "stock_name": f"W{i}"} for i in range(12)])
wide = sn.gather_news("US.LIST2015")
wide_prompt = sn.build_prompt(plate, score, wide)
check("...but 1 of 14 does",
      "not the whole sector" in wide_prompt,
      "observed live: a 14-name sector got 12 headlines, all about its one "
      "watchlist constituent, and nothing said so")
check("...naming the count so the model can weigh it",
      "of this sector's 14 constituents" in wide_prompt)

# The same article linked under two match bases must appear ONCE.
db.insert_news_articles([
    {"dedup_key": "k3", "url": "https://x/3", "title": "Shared headline about both",
     "title_norm": "shared headline about both", "summary": "",
     "feed_key": "f", "source_label": "Reuters", "category": "shocks",
     "published_at": db.now_iso(), "published_estimated": 0,
     "codes": [("US.NVDA", "company_name"), ("US.AMD", "feed_query")]},
])
dedup = sn.gather_news("US.LIST2015")
titles_seen = [n["title"] for n in dedup["ticker"]]
check_eq("an article linked to two constituents appears ONCE",
         titles_seen.count("Shared headline about both"), 1)
check("...and a repeated headline never reaches the prompt",
      sn.build_prompt(plate, score, dedup).count("Shared headline about both") == 1,
      "a duplicate reads as a second corroborating source")
check("...while coverage now counts both names",
      len(dedup["covered_codes"]) == 2, f"{dedup['covered_codes']}")
check("...invites 'no' as an answer",
      "no news explains it" in prompt and "acceptable" in sn.SYSTEM_PROMPT,
      "a model with no honest way out invents a driver")

orphan_plate = {p["plate_code"]: p for p in db.get_sector_universe()}["US.LIST9999"]
orphan_score = [r for r in db.get_rotation_board(window_days=5)
                if r["plate_code"] == "US.LIST9999"][0]
orphan_prompt = sn.build_prompt(orphan_plate, orphan_score, orphan)
check("an unconfirmed reading is flagged to the model",
      "UNCONFIRMED" in orphan_prompt)
check("...and the structural reason for no ticker news is spelled out",
      "no company in this sector is on the watchlist" in orphan_prompt,
      "otherwise the model reads silence as 'nothing happened'")


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


fake = FakeClient(payload(supporting_headlines=["Fed holds rates steady"]))
out = sn.generate_narrative(plate, score, model="test-model", client=fake)
check_eq("generation returns a validated narrative",
         out["confidence_label"], "news explains it")
check_eq("...records which model wrote it", out["model"], "test-model")
check_eq("...and persists", db.get_sector_narrative("US.LIST2015", 5)["headline"],
         GOOD["headline"])
stored = db.get_sector_narrative("US.LIST2015", 5)
check_eq("...with citations as a real list, not a JSON string",
         stored["supporting_headlines"], ["Fed holds rates steady"])
live_news = sn.gather_news("US.LIST2015")
check("...and provenance about what it was shown",
      stored["sources"]["ticker_news"] == len(live_news["ticker"])
      and stored["sources"]["macro_news"] == len(live_news["macro"]),
      f"{stored['sources']} against {len(live_news['ticker'])} ticker / "
      f"{len(live_news['macro'])} macro")
check("...including how wide the news window was",
      stored["sources"]["window_hours"] == sn.NEWS_WINDOW_HOURS)
check("no numeric field reached the row",
      not any(isinstance(v, (int, float)) and k not in
              ("id", "window_days") for k, v in stored.items()
              if k not in ("sources", "supporting_headlines")),
      "the score lives in sector_rotation_scores and nowhere else")

# The retry loop must hand the fault back and accept a corrected answer.
retry = FakeClient(
    payload(supporting_headlines=["An article that does not exist"]),
    payload(supporting_headlines=["Fed holds rates steady"]),
)
out2 = sn.generate_narrative(plate, score, model="test-model", client=retry, persist=False)
check_eq("a fabricated citation is retried, not accepted", len(retry.calls), 2)
check("...and the correction turn carries the specific fault",
      any("was not in the headlines" in str(m.get("content", ""))
          for m in retry.calls[1]["messages"]),
      "a generic 'try again' teaches the model nothing")
check_eq("...and the corrected answer is returned",
         out2["supporting_headlines"], ["Fed holds rates steady"])

# A model that will not comply must raise, not return something half-valid.
stubborn = FakeClient(*[payload(confidence_label="maybe")] * 3)
try:
    sn.generate_narrative(plate, score, model="test-model", client=stubborn,
                          persist=False)
    check("an uncorrectable response raises", False)
except sn.NarrativeValidationError:
    check("an uncorrectable response raises rather than degrading", True,
          f"{len(stubborn.calls)} attempts")


# --- selection: bounded, biggest first, and never repeated ----------------

picks = sn.select_sectors(window_days=5, top_n=3)
check_eq("selection skips sectors already narrated today",
         [p["plate_code"] for p in picks], ["US.LIST9999"],
         )
check("...ordered by absolute move, so the biggest are done first",
      all(abs(picks[i]["score"]) >= abs(picks[i + 1]["score"])
          for i in range(len(picks) - 1)))
check_eq("selection is bounded rather than watchlist-wide",
         sn.TOP_N_EACH_WAY, 3)
check("a market with no scores selects nothing",
      sn.select_sectors(market="HK", window_days=5) == [])

check_eq("narrative_for reads without generating",
         sn.narrative_for("US.LIST2015", 5)["confidence_label"], "news explains it")
check("narrative_for returns None when there is none",
      sn.narrative_for("US.LIST9999", 5) is None,
      "the UI must render nothing rather than an empty shell")

report("sector narrative")
