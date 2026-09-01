# Trading Analyzer

Advisory-only AI trading analyzer and watchlist scanner, built for a
single-user homelab deployment.

**It never places, modifies or cancels an order, and it never will.** The
Moomoo trade context it opens is read-only by construction — it exposes
position and deal queries and nothing else, and there is deliberately no
trading-password setting anywhere in the configuration, so the SDK could not
submit an order even if some future code tried to. What this produces is a
thesis and suggested levels for a human to act on manually.

## What it does

- Fetches daily bars and quotes from Moomoo's OpenD gateway.
- Computes SMA cross, MACD, Bollinger bands and options walls **deterministically
  in Python**. The language model is never asked to calculate a number — it
  interprets, it does not compute.
- Retrieves the most similar historical setups by cosine similarity over a
  fixed-order feature vector, and injects their realised outcomes into the
  prompt before every evaluation.
- Asks a locally-hosted model (Ollama) for a strictly-schema'd JSON thesis, and
  **rejects rather than coerces** malformed output — including semantic faults,
  such as a bullish thesis whose stop sits above its target.
- Scores past theses against the bars that actually followed them, with explicit
  guards against the scorecard flattering itself.
- Serves all of it through a responsive Next.js dashboard.

## Stack

Python 3.11 · FastAPI · APScheduler · SQLite (WAL) · pandas ·
Ollama · Next.js 15 · TypeScript · Tailwind · lightweight-charts

## Layout

    backend/    FastAPI app, services, and the standalone test scripts
    frontend/   Next.js dashboard
    deploy/     systemd units, nftables rules, backup and watchdog scripts

## Tests

There is no pytest here, deliberately. Each suite is a standalone script that
prints one line per check and exits non-zero if any failed:

    cd backend && .venv/bin/python -m tests.test_indicators

Suites ending in `_live` talk to real OpenD / Ollama / RSS endpoints and are
excluded from offline runs.

## A note on this repository

This is a **read-only public mirror**, published automatically from the `core/`
tree of a private monorepo. Pull requests opened here cannot be merged —
history is force-pushed from upstream on every change and would overwrite them.
