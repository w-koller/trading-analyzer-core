"""Boot-time self-check.

Runs once during startup and logs, loudly, anything that will bite later.
It **never raises and never prevents the app from starting** — the app has to
come up in order to report the problem through `/health`, and a backend that
refuses to boot because Ollama is down is strictly less useful than one that
boots and says so.

The checks are chosen for failures that are otherwise silent or misleading:

  * A read-only `SELECT` would not have caught the real incident where
    `trading.db` ended up `root:root` while the service runs as `trading`.
    Only an actual write proves the service can do its job, so this does a
    write and rolls it back.
  * A wrong `trd_security_firm` does not error — it returns RET_OK with only
    the simulated account, so holdings come back empty and read as "you hold
    nothing" rather than as a misconfiguration.
  * OpenD is probed with a raw socket rather than an SDK call, which would
    block for up to 20s and would build the quote context before the
    scheduler exists.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Any

from app import db
from app.config import settings
from app.services import ollama_models

logger = logging.getLogger(__name__)

# Populated at boot, surfaced on /health so problems are visible without
# shell access to the journal.
LAST_RESULT: dict[str, Any] | None = None


def _check_db() -> dict[str, Any]:
    try:
        with db.get_connection() as conn:
            conn.execute("PRAGMA quick_check").fetchone()
            # A real write: ownership/permission problems only show up here.
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                ("_startup_probe", "ok", "probe"),
            )
            conn.execute("DELETE FROM app_state WHERE key = '_startup_probe'")
        return {"ok": True, "detail": "readable and writable"}
    except Exception as exc:
        return {"ok": False, "detail": f"DB not writable: {exc}"}


def _check_config() -> dict[str, Any]:
    problems: list[str] = []
    if not settings.cors_origin_list:
        problems.append("cors_origins is empty — no browser can call this API")
    if not settings.ollama_base_url.rstrip("/").endswith("/v1"):
        problems.append(
            f"ollama_base_url should end in /v1, got {settings.ollama_base_url}"
        )
    if settings.scan_interval_seconds <= 0:
        problems.append("scan_interval_seconds must be > 0")
    if settings.trd_security_firm != "FUTUAU":
        problems.append(
            f"trd_security_firm={settings.trd_security_firm!r}: a wrong firm "
            "returns RET_OK with only the SIMULATE account, so holdings look "
            "empty rather than misconfigured"
        )
    return {"ok": not problems, "detail": "; ".join(problems) or "config sane"}


def _check_opend() -> dict[str, Any]:
    try:
        with socket.create_connection(
            (settings.opend_host, settings.opend_port), timeout=2.0
        ):
            return {"ok": True, "detail": f"{settings.opend_host}:{settings.opend_port} accepting"}
    except OSError as exc:
        return {"ok": False, "detail": f"OpenD not reachable: {exc}"}


def _check_ollama() -> dict[str, Any]:
    import httpx

    base = settings.ollama_base_url.rstrip("/")
    try:
        response = httpx.get(f"{base}/models", timeout=4.0)
        response.raise_for_status()
        models = [m.get("id") for m in response.json().get("data", [])]
    except Exception as exc:
        return {"ok": False, "detail": f"Ollama unreachable: {exc}"}

    active = ollama_models.active_model()
    if active not in models:
        return {
            "ok": False,
            "detail": f"model {active} not present — every "
                      f"thesis will fail at scan time",
        }
    return {"ok": True, "detail": f"{active} present"}


def run_startup_checks() -> dict[str, Any]:
    """Run every check, log a line each, and return a summary. Never raises."""
    global LAST_RESULT
    started = time.monotonic()

    checks: dict[str, dict[str, Any]] = {}
    for name, fn in (
        ("database", _check_db),
        ("config", _check_config),
        ("opend", _check_opend),
        ("ollama", _check_ollama),
    ):
        try:
            checks[name] = fn()
        except Exception as exc:  # a check must never take the app down
            checks[name] = {"ok": False, "detail": f"check itself failed: {exc}"}

        entry = checks[name]
        (logger.info if entry["ok"] else logger.warning)(
            "startup check %-8s %s — %s",
            name, "OK  " if entry["ok"] else "WARN", entry["detail"],
        )

    failed = [n for n, c in checks.items() if not c["ok"]]
    LAST_RESULT = {
        "checks": checks,
        "ok": not failed,
        "failed": failed,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }

    if failed:
        logger.warning(
            "startup self-check: %d ok, %d warn (%s) — starting anyway so "
            "/health can report it",
            len(checks) - len(failed), len(failed), ", ".join(failed),
        )
    else:
        logger.info("startup self-check: all %d checks OK", len(checks))
    return LAST_RESULT
