"""Checks for runtime model selection.

Run from backend/:  .venv/bin/python -m tests.test_model_settings

Offline: temp database, and `list_models` is stubbed so the validation path
is exercised without depending on what happens to be pulled on the Ollama
box today.
"""

import tempfile
from pathlib import Path

from app import db

_tmp = tempfile.mkdtemp(prefix="model-settings-")
db.DB_PATH = Path(_tmp) / "test.db"
db.init_db()

from app.config import settings                                   # noqa: E402
from app.services import ollama_models                            # noqa: E402

from tests.harness import check, report  # noqa: E402


AVAILABLE = ["deepseek-r1:32b", "qwen3.8:latest", "gemma4:31b"]
ollama_models.list_models = lambda timeout=5.0: list(AVAILABLE)

# --- the default, with nothing persisted -------------------------------
check("with no override, the env default is active",
      ollama_models.active_model() == settings.ollama_model,
      ollama_models.active_model())
check("and the source says so", ollama_models.active_model_source() == "env_default")

# --- a valid switch persists -------------------------------------------
ollama_models.set_active_model("qwen3.8:latest")
check("a valid model becomes active", ollama_models.active_model() == "qwen3.8:latest")
check("the source flips to persisted",
      ollama_models.active_model_source() == "persisted")
check("it is stored where a restart will find it",
      db.get_app_state(ollama_models.MODEL_STATE_KEY) == "qwen3.8:latest")

# --- validation is not optional ----------------------------------------
# An unvalidated name doesn't fail here; it fails 90s later inside a scan,
# once per ticker, as an opaque Ollama error.
for bad, why in (("no-such-model:70b", "unknown model"),
                 ("", "empty name"),
                 ("   ", "whitespace-only name")):
    try:
        ollama_models.set_active_model(bad)
        check(f"{why} is refused", False, "accepted")
    except ValueError:
        check(f"{why} is refused", True)

check("a refused switch does not change the active model",
      ollama_models.active_model() == "qwen3.8:latest",
      ollama_models.active_model())

# --- the error names what IS available ---------------------------------
try:
    ollama_models.set_active_model("nope:1b")
except ValueError as exc:
    msg = str(exc)
    check("the rejection lists the available models",
          all(m in msg for m in AVAILABLE), msg[:90])

# --- reset restores the env default ------------------------------------
ollama_models.clear_active_model()
check("reset falls back to the env default",
      ollama_models.active_model() == settings.ollama_model)
check("reset removes the row rather than blanking it",
      db.get_app_state(ollama_models.MODEL_STATE_KEY) is None)
check("the source reports the default again",
      ollama_models.active_model_source() == "env_default")
check("resetting twice is harmless",
      (ollama_models.clear_active_model(), ollama_models.active_model())[1]
      == settings.ollama_model)

# --- an unreachable model host propagates, it does not silently pass ----
def _boom(timeout=5.0):
    raise RuntimeError("connection refused")


ollama_models.list_models = _boom
try:
    ollama_models.set_active_model("deepseek-r1:32b")
    check("an unreachable Ollama blocks the change", False, "accepted blind")
except RuntimeError:
    check("an unreachable Ollama blocks the change", True)
check("and the active model is untouched",
      ollama_models.active_model() == settings.ollama_model)

report("model settings")
