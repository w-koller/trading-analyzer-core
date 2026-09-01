"""Compare thesis models on the only axes that matter: speed AND compliance.

A script, not a test. It costs real GPU minutes and talks to a live Ollama,
so it must never end up in `tests/` where `npm run verify`-style habits would
start running it.

The question this answers is not "which model is fastest". A model that is
three times faster is worse than useless if it fails `validate_thesis` half
the time, because every rejection costs another full generation as a
correction turn (`ai_thesis` hands the model its own bad output back). Wall
time per *accepted* thesis is the number that decides anything, and it is the
product of raw speed and first-attempt compliance.

It replays REAL stored setups rather than a synthetic prompt: the same
indicators, walls, news and RAG precedents the scanner fed the model, rebuilt
from `trade_setups.indicator_snapshot`. A hand-written prompt would measure
the model against something the scanner never asks it.

Nothing is written to the database. `trade_setups` is the RAG corpus that
future advice is retrieved from (rule #3), and a benchmark that seeds it with
throwaway output would quietly change what every later thesis is compared
against. The active model in `app_state` is not touched either — the pin goes
through `generate_thesis(model=...)`, so a scan running concurrently keeps the
model the user chose.

    python -m scripts.benchmark_models qwen3.8:latest deepseek-r1:32b -n 6
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.services import ai_thesis, llm_json, news_service, ollama_models  # noqa: E402


class CountingClient:
    """Wraps the real client so attempts are observable.

    `generate_thesis` reports only its final outcome, and the retry count is
    the whole point here — one accepted thesis after three attempts is three
    generations of GPU time, and averaging that into "seconds per thesis"
    without saying so hides the actual cost difference between models.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls = 0
        self.chat = self._Chat(self)

    class _Chat:
        def __init__(self, outer: "CountingClient"):
            self._outer = outer
            self.completions = self._Completions(outer)

        class _Completions:
            def __init__(self, outer: "CountingClient"):
                self._outer = outer

            def create(self, **kwargs):
                self._outer.calls += 1
                return self._outer._inner.chat.completions.create(**kwargs)


def sample_setups(limit: int) -> list[dict[str, Any]]:
    """The most recent setup per code, newest codes first.

    One per code, because replaying two setups for the same ticker measures
    the same prompt twice and tells you nothing extra about the model.
    """
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM trade_setups s
            JOIN (SELECT code, MAX(id) AS id FROM trade_setups GROUP BY code) m
              ON s.id = m.id
            ORDER BY s.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_one(setup: dict[str, Any], model: str, timeout: float) -> dict[str, Any]:
    """One thesis, timed. Never raises — a failure is a result."""
    snap = json.loads(setup["indicator_snapshot"])
    code = setup["code"]

    # News is re-read rather than stored on the setup, so this is "the same
    # prompt shape", not "the identical bytes". Stated plainly because it is
    # the one input that can drift between the original scan and the replay.
    try:
        news = news_service.get_thesis_context(code)
    except Exception:                                    # noqa: BLE001
        news = None

    client = CountingClient(llm_json.client(timeout))
    started = time.monotonic()
    try:
        thesis, _ = ai_thesis.generate_thesis(
            code=code,
            market=setup["market"],
            feature_vector=json.loads(setup["feature_vector"]),
            indicators=snap.get("indicators") or {},
            walls=snap.get("walls"),
            data_as_of=setup["data_as_of"],
            is_delayed_data=bool(setup["is_delayed_data"]),
            news=news,
            session=snap.get("session"),
            bar_age_days=snap.get("bar_age_days"),
            bars_stale=bool(snap.get("bars_stale")),
            timeout=timeout,
            client=client,
            model=model,
        )
        elapsed = time.monotonic() - started
        return {
            "code": code, "model": model, "ok": True,
            "seconds": round(elapsed, 1), "attempts": client.calls,
            "first_try": client.calls == 1,
            "direction": thesis.trade_direction,
            "conviction": thesis.conviction_score,
            "error": None,
        }
    except Exception as exc:                             # noqa: BLE001
        return {
            "code": code, "model": model, "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "attempts": client.calls, "first_try": False,
            "direction": None, "conviction": None,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def summarise(model: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in runs if r["ok"]]
    times = [r["seconds"] for r in ok]
    return {
        "model": model,
        "n": len(runs),
        "accepted": len(ok),
        "first_try": sum(1 for r in ok if r["first_try"]),
        "median_s": round(statistics.median(times), 1) if times else None,
        "mean_s": round(statistics.mean(times), 1) if times else None,
        "max_s": max(times) if times else None,
        "total_attempts": sum(r["attempts"] for r in runs),
        "convictions": [r["conviction"] for r in ok],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="+", help="Ollama model names to compare")
    ap.add_argument("-n", "--tickers", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=ai_thesis.DEFAULT_TIMEOUT)
    ap.add_argument("--out", default=None, help="write raw results as JSON here")
    args = ap.parse_args()

    # Validate up front. Discovering a typo'd name 90 seconds into the run,
    # once per ticker, is the exact failure decisions #38 added validation to
    # avoid — it applies just as much here.
    try:
        served = set(ollama_models.list_models())
    except Exception as exc:                             # noqa: BLE001
        print(f"cannot reach Ollama: {exc}", file=sys.stderr)
        return 2
    missing = [m for m in args.models if m not in served]
    if missing:
        print(f"not served by Ollama: {missing}\navailable: {sorted(served)}",
              file=sys.stderr)
        return 2

    setups = sample_setups(args.tickers)
    if not setups:
        print("no stored setups to replay — run a scan first", file=sys.stderr)
        return 2

    print(f"replaying {len(setups)} stored setups "
          f"({', '.join(s['code'] for s in setups)})")
    print(f"active model stays {ollama_models.active_model()!r} throughout\n")

    all_runs: list[dict[str, Any]] = []
    for model in args.models:
        print(f"--- {model} ---")
        for setup in setups:
            r = run_one(setup, model, args.timeout)
            all_runs.append(r)
            if r["ok"]:
                flag = "" if r["first_try"] else f"  ({r['attempts']} attempts)"
                print(f"  {r['code']:<10} {r['seconds']:>6.1f}s  "
                      f"{r['direction']:<8} {r['conviction']}/10{flag}")
            else:
                print(f"  {r['code']:<10} {r['seconds']:>6.1f}s  FAILED  {r['error']}")
        print()

    rows = [summarise(m, [r for r in all_runs if r["model"] == m]) for m in args.models]

    print("\n| model | accepted | 1st-try | median | mean | slowest | GPU calls |")
    print("|---|---|---|---|---|---|---|")
    for s in rows:
        print(f"| `{s['model']}` | {s['accepted']}/{s['n']} | "
              f"{s['first_try']}/{s['accepted'] or 1} | {s['median_s']}s | "
              f"{s['mean_s']}s | {s['max_s']}s | {s['total_attempts']} |")

    print("\nConviction spread (the corpus these scores end up in):")
    for s in rows:
        c = s["convictions"]
        if c:
            print(f"  {s['model']:<20} {sorted(c)}  "
                  f"median {statistics.median(c)}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "summary": rows, "runs": all_runs}, indent=2))
        print(f"\nraw results -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
