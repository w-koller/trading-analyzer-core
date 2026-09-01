"""The prose blocks a prompt is assembled from.

Extracted from `ai_thesis` so the chat endpoint can describe a setup in
exactly the words the thesis prompt used. That matters more than it sounds:
two modules independently formatting the same MACD histogram will drift, and
then the model is told the same number twice in two shapes and the user is
shown a third in the UI.

These are formatters and nothing else — they read values that Python already
calculated and turn them into lines. No arithmetic happens here beyond
rounding for display, so rule #1 is not in play.

Deliberately left behind in `ai_thesis`: SYSTEM_PROMPT, build_prompt,
extract_json, validate_thesis and count_sentences. Those encode rule #2 —
strict JSON, exactly three sentences, six exact keys — which is a contract of
the thesis endpoint specifically. A conversation has no such shape, and a
chat that imported the thesis validator would be enforcing a rule that does
not apply to it.
"""

from __future__ import annotations

from typing import Any

def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:,.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _freshness_line(
    market: str,
    is_delayed: bool,
    data_as_of: str,
    bar_age_days: float | None = None,
    bars_stale: bool = False,
) -> str:
    """Rule #7 — the model must never read old data as if it were live.

    `data_as_of` is the newest BAR's timestamp, not the clock, so this states
    when the data is genuinely from. The age is spelled out because "as of
    2026-08-21" does not read as "three days ago" to a model that has no
    reliable sense of today's date.
    """
    age = ""
    if bar_age_days is not None:
        if bar_age_days < 1:
            age = f" (about {bar_age_days * 24:.0f} hours old)"
        else:
            age = f" (about {bar_age_days:.1f} days old)"

    if bars_stale:
        return (
            f"DATA FRESHNESS: **The most recent bar is from {data_as_of} "
            f"(UTC){age}, which is unusually old.** The market may have moved "
            f"substantially since. Say so explicitly in your reasoning and "
            f"keep conviction low — do not present these levels as current."
        )
    if is_delayed:
        return (
            f"DATA FRESHNESS: {market} quotes are DELAYED. Every price below "
            f"reflects the market as of {data_as_of} (UTC){age}, not this "
            f"moment. Treat levels as approximate and say so if it matters."
        )
    return (
        f"DATA FRESHNESS: {market} quotes are real-time; the most recent bar "
        f"is from {data_as_of} (UTC){age}. Prices between then and now are "
        f"not reflected here."
    )


def _indicator_block(ind: dict[str, Any]) -> str:
    lines = [
        f"  Last close:        {_fmt(ind.get('close'))}",
        f"  SMA fast / slow:   {_fmt(ind.get('sma_fast'))} / {_fmt(ind.get('sma_slow'))}"
        f"  (trend: {ind.get('sma_trend', 'unknown')}, gap: "
        f"{_fmt(ind.get('sma_gap_pct'), suffix='%')})",
        f"  SMA cross event:   {ind.get('sma_cross', 'none')}",
        f"  MACD / signal:     {_fmt(ind.get('macd'), 4)} / {_fmt(ind.get('macd_signal'), 4)}"
        f"  (histogram: {_fmt(ind.get('macd_hist'), 4)}, state: "
        f"{ind.get('macd_state', 'unknown')})",
        f"  MACD cross event:  {ind.get('macd_cross', 'none')}",
        f"  Bollinger u/m/l:   {_fmt(ind.get('bb_upper'))} / {_fmt(ind.get('bb_mid'))}"
        f" / {_fmt(ind.get('bb_lower'))}",
        f"  Position in bands: %B {_fmt(ind.get('bb_percent_b'), 3)}"
        f" ({ind.get('bb_state', 'unknown')}), bandwidth "
        f"{_fmt(ind.get('bb_bandwidth'), 4)}",
    ]
    for warning in ind.get("warnings") or []:
        lines.append(f"  NOTE: {warning}")
    return "\n".join(lines)


def _walls_block(walls: dict[str, Any] | None) -> str:
    if not walls or not walls.get("has_walls"):
        return "  No options chain data available for this ticker."
    lines = [
        f"  Expiry analysed:   {walls.get('expiry')}",
        f"  Call wall:         {_fmt(walls.get('call_wall'))}"
        f"  (OI {walls.get('call_wall_oi', 0):,} + volume "
        f"{walls.get('call_wall_volume', 0):,}, distance "
        f"{_fmt(walls.get('call_wall_distance_pct'), suffix='%')})",
        f"  Put wall:          {_fmt(walls.get('put_wall'))}"
        f"  (OI {walls.get('put_wall_oi', 0):,} + volume "
        f"{walls.get('put_wall_volume', 0):,}, distance "
        f"{_fmt(walls.get('put_wall_distance_pct'), suffix='%')})",
        f"  Put/call ratio:    {_fmt(walls.get('put_call_oi_ratio'), 3)} by open "
        f"interest, {_fmt(walls.get('put_call_volume_ratio'), 3)} by volume",
    ]
    return "\n".join(lines)


def _similar_block(similar: list[dict[str, Any]]) -> str:
    """The RAG payload: past setups shaped like this one, and how they ended."""
    if not similar:
        return (
            "  No comparable historical setups have been recorded yet. Judge "
            "this setup on its own technicals and keep conviction modest — "
            "there is no track record to lean on."
        )
    lines = []
    for i, s in enumerate(similar, 1):
        outcome = s.get("outcome") or {}
        if outcome:
            pnl = outcome.get("pnl_pct")
            result = (
                f"realized {_fmt(pnl, 2, '%')}"
                if pnl is not None else "closed, P&L not recorded"
            )
            hold = outcome.get("hold_time_hours")
            if hold is not None:
                result += f" over {_fmt(hold, 1)}h"
            if outcome.get("exit_reason"):
                result += f", exit: {outcome['exit_reason']}"
        else:
            result = "no outcome recorded yet"
        lines.append(
            f"  {i}. {s.get('code')} on {s.get('created_at')} — similarity "
            f"{_fmt(s.get('similarity'), 3)}, called "
            f"{s.get('trade_direction')} at conviction "
            f"{s.get('conviction_score')} -> {result}"
        )

    # State the sample size plainly. A similarity score reads as authority on
    # its own, and two or three precedents is not a track record — without
    # this the model weighted a handful of rows as if they were a base rate.
    lines.append(
        f"  (Sample size: {len(similar)}. This is a handful of past cases, not"
        f" a statistical base rate — weight it accordingly and do not treat"
        f" the similarity scores as calibrated probabilities.)"
    )
    return "\n".join(lines)


def _news_block(news: dict[str, Any] | None, code: str) -> str:
    """Ticker news and market context, separately labelled and dated.

    The previous version padded a short ticker list with unrelated market
    headlines under one flat heading and threw the publication date away — so
    the model could not tell "IBM beats on cloud" from "Stocks slide as Fed
    holds", and "RECENT" was an unverified claim in a prompt whose freshness
    line exists precisely because the model has no reliable sense of today's
    date.

    An empty ticker list now says so. That is strictly more useful than five
    unrelated market headlines dressed as news about this ticker.
    """
    if not news:
        return "  No recent headlines retrieved."

    window = news.get("window_hours", 72)
    lines: list[str] = []

    ticker_items = news.get("ticker") or []
    lines.append(f"  NEWS ABOUT {code} — most recent first:")
    if ticker_items:
        for item in ticker_items:
            lines.append(f"    - {age_label(item)} ({item['source_label']}): {item['title']}")
    else:
        lines.append(f"    (none in the last {window} hours)")

    macro_items = news.get("macro") or []
    if macro_items:
        lines.append("")
        lines.append("  MARKET-WIDE AND MACRO CONTEXT — these are NOT about this ticker:")
        for item in macro_items:
            lines.append(f"    - {age_label(item)} ({item['source_label']}): {item['title']}")
    return "\n".join(lines)


def age_label(item: dict[str, Any]) -> str:
    """"6h ago" — spelled out, because a bare date means little to a model."""
    hours = item.get("age_hours")
    if hours is None:
        return item.get("published_at", "undated")
    if hours < 1:
        return "under an hour ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"
