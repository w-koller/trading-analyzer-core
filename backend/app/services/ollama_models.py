"""Which model writes the theses, and how to change it without a restart.

`settings.ollama_model` is the env-configured default. This module lets a
persisted choice override it at runtime, stored in `app_state` alongside
`rotation_enabled` (decisions #27) so it survives a restart the same way.

Two things are deliberate:

**A new model is validated against the live list before it is accepted.**
An unvalidated name does not fail here — it fails 90 seconds later, inside a
scan, as an opaque Ollama error, once per ticker, and `startup_check` would
only notice at the next boot. Refusing up front is the only place the user
can act on it.

**The active model is resolved per thesis, not cached at import.** That makes
a change take effect from the next thesis without a restart, and it means a
scan already running finishes on the model it started with.

Nothing here is model-specific. `ai_thesis` strips `<think>` blocks and
```json fences unconditionally, which is harmless for models that emit
neither, and its `DEFAULT_TIMEOUT` of 300s is sized for deepseek-r1's
90-120s — a faster model needs no change, a slower one might.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

MODEL_STATE_KEY = "ollama_model"
DEFAULT_LIST_TIMEOUT = 5.0

ModelSource = Literal["persisted", "env_default"]


def list_models(timeout: float = DEFAULT_LIST_TIMEOUT) -> list[str]:
    """Model ids Ollama currently serves. Raises if it cannot be reached."""
    base = settings.ollama_base_url.rstrip("/")
    response = httpx.get(f"{base}/models", timeout=timeout)
    response.raise_for_status()
    return [m.get("id") for m in response.json().get("data", []) if m.get("id")]


def active_model() -> str:
    """The model to use right now — persisted choice, else the env default."""
    return db.get_app_state(MODEL_STATE_KEY) or settings.ollama_model


def active_model_source() -> ModelSource:
    return "persisted" if db.get_app_state(MODEL_STATE_KEY) else "env_default"


def set_active_model(name: str, timeout: float = DEFAULT_LIST_TIMEOUT) -> str:
    """Persist a new model. Raises ValueError if Ollama doesn't serve it."""
    name = (name or "").strip()
    if not name:
        raise ValueError("model name must not be empty")

    available = list_models(timeout=timeout)   # transport errors propagate
    if name not in available:
        raise ValueError(
            f"{name!r} is not served by Ollama at {settings.ollama_base_url}. "
            f"Available: {', '.join(sorted(available)) or '(none)'}"
        )

    db.set_app_state(MODEL_STATE_KEY, name)
    logger.info("active thesis model set to %s (was %s)", name, active_model())
    return name


def clear_active_model() -> str:
    """Drop the override so the env default applies again."""
    db.delete_app_state(MODEL_STATE_KEY)
    logger.info("active thesis model reset to the env default %s", settings.ollama_model)
    return settings.ollama_model
