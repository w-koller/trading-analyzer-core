"""The intrinsic-value calculator's "brief me" button.

The DCF/GGM/NAV math itself runs entirely in the browser — nothing here
computes a valuation. This router only turns an already-computed result
into a plain-English explanation, on request, through the same LLM slot
every other interactive generation in this app shares.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.services import llm_slots, valuation_narrative

router = APIRouter(prefix="/valuation", tags=["valuation"])

# Wide enough for any real equity's inputs, narrow enough to keep a stray
# browser bug (or a deliberately hostile payload) from putting an absurd
# figure into a prompt that gets logged and retried up to three times.
_MAX_MONEY = 1e13
_MAX_RATE = 100.0
_MIN_RATE = -100.0


def _money_field(**kwargs: Any) -> Any:
    return Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False, **kwargs)


def _rate_field(**kwargs: Any) -> Any:
    return Field(ge=_MIN_RATE, le=_MAX_RATE, allow_inf_nan=False, **kwargs)


class DCFInputs(BaseModel):
    fcf: float = _money_field()
    forecast_years: int = Field(ge=1, le=30)
    growth_rate: float = _rate_field()
    discount_rate: float = _rate_field()
    terminal_growth: float = _rate_field()
    cash: float = _money_field()
    debt: float = _money_field()
    shares_outstanding: float = Field(gt=0, le=_MAX_MONEY, allow_inf_nan=False)


class GGMInputs(BaseModel):
    dividend: float = _money_field()
    growth_rate: float = _rate_field()
    discount_rate: float = _rate_field()


class NAVInputs(BaseModel):
    assets: float = _money_field()
    liabilities: float = _money_field()
    preferred: float = _money_field()
    shares_outstanding: float = Field(gt=0, le=_MAX_MONEY, allow_inf_nan=False)


class ComputedResult(BaseModel):
    intrinsic_value: float = _money_field()
    verdict: Literal["undervalued", "fair", "overvalued"]
    market_price: float = Field(ge=0, le=_MAX_MONEY, allow_inf_nan=False)
    target_buy_price: float = _money_field()
    upside_pct: float = Field(ge=-100.0, le=100_000.0, allow_inf_nan=False)
    sensitivity_min: float | None = Field(default=None, allow_inf_nan=False)
    sensitivity_max: float | None = Field(default=None, allow_inf_nan=False)


class ValuationInterpretRequest(BaseModel):
    model_kind: Literal["dcf", "ggm", "nav"]
    dcf_inputs: DCFInputs | None = None
    ggm_inputs: GGMInputs | None = None
    nav_inputs: NAVInputs | None = None
    computed: ComputedResult

    def resolved_inputs(self) -> dict[str, Any]:
        by_kind = {
            "dcf": self.dcf_inputs,
            "ggm": self.ggm_inputs,
            "nav": self.nav_inputs,
        }
        inputs = by_kind[self.model_kind]
        if inputs is None:
            raise ValueError(f"model_kind is {self.model_kind!r} but "
                              f"{self.model_kind}_inputs was not sent")
        return inputs.model_dump()


@router.post("/{code}/interpret")
async def interpret(code: str, payload: ValuationInterpretRequest) -> dict[str, Any]:
    """Explain an already-computed valuation in plain English.

    Blocks for up to a few minutes on one model call, same reasoning as the
    scan button and the earnings outlook — no client timeout, this is meant
    to be waited on.
    """
    try:
        inputs = payload.resolved_inputs()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    token = await run_in_threadpool(
        llm_slots.acquire, f"valuation {code}", llm_slots.INTERACTIVE_TIMEOUT
    )
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="The model is busy. Try again in a moment.",
        )
    try:
        return await run_in_threadpool(
            valuation_narrative.generate_interpretation,
            code=code,
            model_kind=payload.model_kind,
            inputs=inputs,
            computed=payload.computed.model_dump(),
        )
    except valuation_narrative.ValuationNarrativeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        llm_slots.release(token)
