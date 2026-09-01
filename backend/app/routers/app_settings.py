"""Runtime settings a user can change without editing .env and restarting.

Currently just the thesis model. Named `app_settings` rather than `settings`
because `main.py` already imports `from app.config import settings`, and a
module that shadows it there is a trap waiting for whoever adds the next
import.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import scheduler
from app.config import settings
from app.services import ollama_models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


class ModelChoice(BaseModel):
    model: str


def _state(warning: str | None = None) -> dict:
    """Everything the UI needs to render the picker, in one shape.

    Never raises when Ollama is down — reports `reachable: false` with an
    empty list, matching /health's always-answer contract (decisions #26).
    A settings page that 500s because a dependency is down cannot tell you
    that the dependency is down.
    """
    try:
        available = ollama_models.list_models()
        reachable, error = True, None
    except Exception as exc:
        available, reachable, error = [], False, str(exc)

    return {
        "active": ollama_models.active_model(),
        "source": ollama_models.active_model_source(),
        "env_default": settings.ollama_model,
        "available": sorted(available),
        "reachable": reachable,
        "error": error,
        "scan_in_progress": bool(scheduler.scheduler_status().get("scan_in_progress")),
        "warning": warning,
    }


@router.get("/model")
async def get_model():
    return await run_in_threadpool(_state)


@router.put("/model")
async def put_model(payload: ModelChoice):
    """Switch the model used for future theses.

    Refuses when Ollama is unreachable rather than accepting blind: an
    unvalidated name doesn't fail here, it fails 90 seconds later inside a
    scan, once per ticker, as an opaque error.

    A scan already running is a **warning, not a 409**. The model is resolved
    once per thesis, so the change only affects theses that start after it —
    the in-flight scan finishes on what it started with. Blocking the user
    for the hour an unattended pre-market scan takes would be the worse
    failure.
    """
    def _apply() -> dict:
        try:
            ollama_models.set_active_model(payload.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Cannot reach Ollama to validate the model: {exc}",
            ) from exc

        warning = None
        if scheduler.scheduler_status().get("scan_in_progress"):
            warning = (
                "A scan is in progress. It finishes on the previous model; "
                "this change applies from the next thesis."
            )
        return _state(warning)

    return await run_in_threadpool(_apply)


@router.post("/model/reset")
async def reset_model():
    """Drop the override and fall back to the env-configured default."""
    await run_in_threadpool(ollama_models.clear_active_model)
    return await run_in_threadpool(_state)
