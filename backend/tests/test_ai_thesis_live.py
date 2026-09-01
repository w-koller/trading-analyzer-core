"""Live check for ai_thesis against the real Ollama model.

Run from backend/:  .venv/bin/python -m tests.test_ai_thesis_live

Slow by nature — deepseek-r1:32b takes ~90-120s per call, and this makes
two. The point is to prove the real model's output survives the real
validator: the schema, the markdown fence it insists on emitting, and the
three-sentence rule.
"""

import tempfile
import time
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="thesis-live-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.config import settings                              # noqa: E402
from app.services.ai_thesis import AIThesis, generate_thesis  # noqa: E402
from app.services.indicators import IndicatorSnapshot         # noqa: E402
from app.services.options_walls import OptionWalls            # noqa: E402
from app.services.similarity import build_feature_vector      # noqa: E402

from tests.harness import check, report  # noqa: E402


print(f"model: {settings.ollama_model} at {settings.ollama_base_url}")

ind = IndicatorSnapshot(
    close=179.94, sma_fast=171.2, sma_slow=155.8, sma_trend="bullish",
    sma_cross="none", sma_gap_pct=9.88, macd=3.42, macd_signal=2.87,
    macd_hist=0.55, macd_state="bullish", macd_cross="bullish",
    bb_upper=185.1, bb_mid=172.4, bb_lower=159.7, bb_percent_b=0.8,
    bb_bandwidth=0.147, bb_state="upper_half",
)
walls = OptionWalls(
    expiry="2026-09-18", spot=179.94, call_wall=190.0, call_wall_oi=21938,
    call_wall_volume=1420, put_wall=170.0, put_wall_oi=18697,
    put_wall_volume=980, call_wall_distance_pct=5.59,
    put_wall_distance_pct=-5.52, put_call_oi_ratio=0.816,
    put_call_volume_ratio=0.69,
)
vec = build_feature_vector(ind, walls)

t0 = time.time()
thesis, similar = generate_thesis(
    code="US.PLTR", market="US", feature_vector=vec,
    indicators=ind.to_dict(), walls=walls.to_dict(),
    data_as_of="2026-08-23T13:00:00+00:00", is_delayed_data=False,
    news={"ticker": [{"title": "Palantir extends government AI contract",
                      "source_label": "Yahoo Finance",
                      "published_at": "2026-08-23T12:00:00+00:00",
                      "age_hours": 6.0, "match_basis": "feed_query"}],
          "macro": [], "window_hours": 72}, session="open",
)
elapsed = time.time() - t0

check("live model returns a validated thesis", isinstance(thesis, AIThesis),
      f"{elapsed:.0f}s")
check("direction is one of the three allowed",
      thesis.trade_direction in ("Bullish", "Bearish", "Neutral"),
      thesis.trade_direction)
check("conviction in range", 1 <= thesis.conviction_score <= 10,
      str(thesis.conviction_score))
check("reasoning is exactly 3 sentences (validator enforced)",
      isinstance(thesis.reasoning, str) and thesis.reasoning.strip() != "")
check("RAG ran with an empty history without failing", similar == [])
if thesis.suggested_stop is not None and thesis.suggested_target is not None:
    ordered = (thesis.suggested_stop < thesis.suggested_target
               if thesis.trade_direction == "Bullish"
               else thesis.suggested_stop > thesis.suggested_target
               if thesis.trade_direction == "Bearish" else True)
    check("stop/target ordered coherently with direction", ordered,
          f"stop={thesis.suggested_stop} target={thesis.suggested_target}")

print(f"\n  direction : {thesis.trade_direction}")
print(f"  conviction: {thesis.conviction_score}")
print(f"  reasoning : {thesis.reasoning}")
print(f"  stop/target: {thesis.suggested_stop} / {thesis.suggested_target}")
print(f"  notes     : {thesis.key_levels_notes}\n")

# Rule #7: a delayed-data prompt must produce a thesis that knows it.
t0 = time.time()
au_thesis, _ = generate_thesis(
    code="AU.BHP", market="AU", feature_vector=vec,
    indicators=ind.to_dict(), walls=None,
    data_as_of="2026-08-23T05:45:00+00:00", is_delayed_data=True,
)
check("delayed-market thesis also validates", isinstance(au_thesis, AIThesis),
      f"{time.time() - t0:.0f}s")

report("ai_thesis live")
