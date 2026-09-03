"""The scan cycle: watchlist -> indicators -> walls -> RAG -> thesis -> DB.

This is where every rule meets. One cycle, per ticker:

  1. bars  -> indicators.compute()          (rule #1, deterministic in Python)
  2. chain -> options_walls.fetch_walls()   (rule #1)
  3. those -> similarity.build_feature_vector()
  4. that  -> ai_thesis.generate_thesis(), which does the RAG retrieval
              itself before contacting the model (rule #3)
  5. validated thesis -> db.insert_trade_setup() with data_as_of and
     is_delayed_data recorded (rule #7)

Pacing is the hard constraint here, and it is not a tuning detail. A single
deepseek-r1:32b thesis takes 90-120 seconds on this hardware, so a 60-second
scan interval cannot evaluate a 45-ticker watchlist — not slowly, but at all.
The scanner therefore treats each cycle as a *slice*: it takes the
`max_tickers` least-recently-analysed enabled tickers and leaves the rest for
subsequent cycles. Over time every ticker is covered, oldest first, and no
cycle overruns into the next.

History klines draw on a per-account quota, so bars are cached per
(code, window) for `market_data.cache_ttl()` rather than refetched every
cycle.

Failure is per-ticker, never per-cycle: one dead chain or one model timeout
costs that ticker's setup and is counted in the run record. A watchlist sync
that partially fails still scans the tickers it did get.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.services import (
    ai_thesis, indicators, market_data, news_service, ollama_models, options_walls,
)
from app.services.similarity import FEATURE_VERSION, build_feature_vector
from app.services.watchlist_service import sync_watchlist
from app.utils import market_hours

logger = logging.getLogger(__name__)

DEFAULT_MAX_TICKERS = 3        # ~90-120s of inference each; see module docstring


@dataclass
class TickerResult:
    code: str
    ok: bool
    setup_id: int | None = None
    error: str | None = None
    elapsed: float = 0.0


@dataclass
class CycleResult:
    run_id: int
    scanned: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[TickerResult] = field(default_factory=list)
    sync: dict[str, Any] | None = None
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scanned": self.scanned,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "elapsed_seconds": round(self.elapsed, 1),
            "sync": self.sync,
            "results": [
                {"code": r.code, "ok": r.ok, "setup_id": r.setup_id,
                 "error": r.error, "elapsed_seconds": round(r.elapsed, 1)}
                for r in self.results
            ],
        }


def _spot_price(gateway, code: str) -> float | None:
    try:
        rows = gateway.get_snapshot([code])
    except Exception as exc:
        logger.warning("%s: snapshot failed: %s", code, exc)
        return None
    if not rows:
        return None
    price = rows[0].get("last_price")
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def _walls_for(gateway, code: str, spot: float | None):
    """Best-effort option walls. Most tickers have a chain; some don't."""
    try:
        walls = options_walls.fetch_walls(gateway, code, spot=spot)
        return walls if walls.has_walls else None
    except Exception as exc:
        logger.info("%s: no usable options chain (%s)", code, exc)
        return None


def scan_ticker(
    gateway,
    code: str,
    market: str,
    run_id: int | None = None,
    with_walls: bool = True,
    with_news: bool = True,
    force: bool = False,
) -> TickerResult:
    """Run the full pipeline for one ticker and persist the setup.

    `force` bypasses the kline cache, so an on-demand scan can be made to
    refetch bars rather than reusing a cached window up to
    `market_data.cache_ttl()` old. It costs quota — that is the point of
    it being opt-in.
    """
    started = time.time()
    try:
        bars = market_data.get_cached_bars(gateway, code, use_cache=not force)
        if bars is None or bars.empty:
            raise RuntimeError("no history klines returned")

        snapshot = indicators.compute(bars)

        # Track whether `spot` is a live price or a fallback to the last
        # close. The wall distances are computed from it and then fed to both
        # the prompt and the feature vector, so silently substituting a stale
        # close makes those numbers mean something different than they claim.
        live_spot = _spot_price(gateway, code)
        spot = live_spot if live_spot is not None else snapshot.close
        walls = _walls_for(gateway, code, spot) if with_walls else None

        vector = build_feature_vector(snapshot, walls)

        # Rule #7: record what the data actually reflects, and tell the model.
        #
        # `data_as_of` comes from the newest BAR, not the clock. Using the
        # clock told the model that Friday's close was real-time on a Monday
        # — a 72-hour lie in the one field whose job is to prevent exactly
        # that. The clock-based value remains correct for live quotes, which
        # is why market_hours now has two functions.
        last_bar_time = bars["time_key"].iloc[-1] if "time_key" in bars.columns else None
        is_delayed = market_hours.is_delayed_data(market)
        as_of = market_hours.bars_as_of(last_bar_time, market).isoformat()
        bar_age = market_hours.bar_age_days(last_bar_time)
        stale = market_hours.bars_are_stale(last_bar_time)
        session = market_hours.session_of(market).value

        if stale:
            logger.warning(
                "%s: newest bar is %.1f days old (%s) — thesis will be built "
                "on stale data", code, bar_age or -1, last_bar_time,
            )

        # Reads STORED news — the scan path makes no outbound HTTP call
        # for news at all now, so a slow feed cannot add seconds per
        # ticker to a cycle.
        news = news_service.get_thesis_context(code) if with_news else None

        indicator_payload = {
            "indicators": snapshot.to_dict(),
            "walls": walls.to_dict() if walls else None,
            "spot": spot,
            "spot_is_live": live_spot is not None,
            "session": session,
            "feature_version": FEATURE_VERSION,
            # Which model wrote this. A corpus that silently mixes models is
            # uninterpretable, and get_similar_setups reads from exactly this
            # corpus — so a thesis has to carry its own provenance. Goes in
            # the JSON column, so no schema change.
            "model": ollama_models.active_model(),
            "bars_used": int(len(bars)),
            "last_bar_time": str(last_bar_time) if last_bar_time is not None else None,
            "bar_age_days": round(bar_age, 2) if bar_age is not None else None,
            "bars_stale": stale,
        }

        # generate_thesis performs the RAG retrieval internally, before the
        # model call — rule #3 is enforced by that call graph, not by
        # remembering to do it here.
        thesis, similar = ai_thesis.generate_thesis(
            code=code,
            market=market,
            feature_vector=vector,
            indicators=snapshot.to_dict(),
            walls=walls.to_dict() if walls else None,
            data_as_of=as_of,
            is_delayed_data=is_delayed,
            news=news,
            session=session,
            bar_age_days=bar_age,
            bars_stale=stale,
        )

        setup_id = db.insert_trade_setup(
            scanner_run_id=run_id,
            code=code,
            market=market,
            data_as_of=as_of,
            is_delayed_data=is_delayed,
            indicator_snapshot=indicator_payload,
            feature_vector=vector,
            trade_direction=thesis.trade_direction,
            conviction_score=thesis.conviction_score,
            reasoning=thesis.reasoning,
            suggested_entry=thesis.suggested_entry,
            suggested_stop=thesis.suggested_stop,
            suggested_target=thesis.suggested_target,
            key_levels_notes=thesis.key_levels_notes,
            similar_setup_ids=[s["setup_id"] for s in similar],
        )
        elapsed = time.time() - started
        logger.info(
            "%s: %s conviction %d -> setup %d (%.0fs)",
            code, thesis.trade_direction, thesis.conviction_score, setup_id, elapsed,
        )
        return TickerResult(code=code, ok=True, setup_id=setup_id, elapsed=elapsed)

    except Exception as exc:
        logger.warning("%s: scan failed: %s", code, exc, exc_info=False)
        return TickerResult(code=code, ok=False, error=str(exc),
                            elapsed=time.time() - started)


def _scan_order(tickers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Least-recently-analysed first, so a partial cycle still rotates fairly.

    A ticker that has never produced a setup sorts before every ticker that
    has, so new additions to the watchlist are picked up on the next cycle
    rather than waiting out a full rotation.
    """
    def key(t: dict[str, Any]) -> str:
        latest = db.get_latest_setup_for_code(t["code"])
        return latest["created_at"] if latest else ""
    return sorted(tickers, key=key)


def run_cycle(
    gateway,
    max_tickers: int | None = DEFAULT_MAX_TICKERS,
    sync_first: bool = True,
    market: str | None = None,
    codes: list[str] | None = None,
    with_walls: bool = True,
    with_news: bool = True,
    force: bool = False,
) -> CycleResult:
    """Run one scan cycle. Returns a summary; never raises for one bad ticker.

    `codes` overrides the watchlist selection entirely — used by the manual
    "scan this ticker now" endpoint. `max_tickers=None` means the whole
    enabled watchlist for the market, used by the pre-market full scan.
    """
    started = time.time()
    run_id = db.insert_scanner_run()
    result = CycleResult(run_id=run_id)

    try:
        if sync_first:
            try:
                result.sync = sync_watchlist(gateway).to_dict()
            except Exception as exc:
                logger.warning("watchlist sync failed, scanning cached list: %s", exc)
                result.sync = {"error": str(exc)}

        if codes:
            enabled = {t["code"]: t for t in db.get_enabled_tickers()}
            targets = [enabled[c] for c in codes if c in enabled]
            missing = [c for c in codes if c not in enabled]
            for code in missing:
                result.results.append(TickerResult(
                    code=code, ok=False,
                    error="not in the watchlist, or not enabled",
                ))
                result.failed += 1
        else:
            ordered = _scan_order(db.get_enabled_tickers(market))
            targets = ordered if max_tickers is None else ordered[:max_tickers]

        logger.info("scan cycle %d: %d target(s)", run_id, len(targets))

        for target in targets:
            outcome = scan_ticker(
                gateway, target["code"], target["market"], run_id=run_id,
                with_walls=with_walls, with_news=with_news, force=force,
            )
            result.results.append(outcome)
            result.scanned += 1
            result.succeeded += int(outcome.ok)
            result.failed += int(not outcome.ok)

        result.elapsed = time.time() - started
        db.finish_scanner_run(
            run_id, result.scanned, result.succeeded, result.failed,
            status="completed",
            error_summary="; ".join(
                f"{r.code}: {r.error}" for r in result.results if not r.ok
            ) or None,
        )
        return result

    except Exception as exc:
        result.elapsed = time.time() - started
        db.finish_scanner_run(
            run_id, result.scanned, result.succeeded, result.failed,
            status="failed", error_summary=str(exc),
        )
        logger.error("scan cycle %d failed: %s", run_id, exc)
        raise


def clear_kline_cache() -> None:
    """Kept as the scanner's public name for the now-shared cache."""
    market_data.clear_kline_cache()
