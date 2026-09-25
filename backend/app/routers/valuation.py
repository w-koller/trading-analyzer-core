"""The intrinsic-value calculator's "brief me" button.

The DCF/GGM/NAV math itself runs entirely in the browser — nothing here
computes a valuation. This router only turns an already-computed result
into a plain-English explanation, on request, through the same LLM slot
every other interactive generation in this app shares.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.services import (llm_json, llm_slots, llm_stream, ollama_models,
                          valuation_narrative)

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
    # Whether `dividend` is the last twelve months (D0) or next year's (D1).
    # The model was never told, and the two differ by a year of growth.
    # Defaults to D0, which is what every caller before this sent.
    use_next_year: bool = False
    growth_rate: float = _rate_field()
    discount_rate: float = _rate_field()


class NAVInputs(BaseModel):
    assets: float = _money_field()
    liabilities: float = _money_field()
    preferred: float = _money_field()
    # Defaults to 0 so a caller that predates the field still validates.
    minority_interest: float = _money_field(default=0.0)
    shares_outstanding: float = Field(gt=0, le=_MAX_MONEY, allow_inf_nan=False)


class ComputedResult(BaseModel):
    intrinsic_value: float = _money_field()
    verdict: Literal["undervalued", "fair", "overvalued"]
    market_price: float = Field(ge=0, le=_MAX_MONEY, allow_inf_nan=False)
    target_buy_price: float = _money_field()
    upside_pct: float = Field(ge=-100.0, le=100_000.0, allow_inf_nan=False)
    margin_of_safety_pct: float | None = Field(default=None, ge=0, le=100,
                                               allow_inf_nan=False)
    sensitivity_min: float | None = Field(default=None, allow_inf_nan=False)
    sensitivity_max: float | None = Field(default=None, allow_inf_nan=False)
    # Solved in the browser, never asked of the model (cloud #52): the
    # prompt used to ask how far an input "would need to move to flip the
    # verdict", which is a calculation. All optional, so an older caller
    # still validates.
    implied_growth_rate: float | None = Field(default=None, ge=-1000, le=1000,
                                              allow_inf_nan=False)
    breakeven_discount_rate: float | None = Field(default=None, ge=-100, le=100,
                                                  allow_inf_nan=False)
    terminal_value_share_pct: float | None = Field(default=None, ge=-10_000,
                                                   le=10_000, allow_inf_nan=False)
    price_to_nav: float | None = Field(default=None, ge=0, le=100_000,
                                       allow_inf_nan=False)


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
            computed=payload.computed.model_dump(exclude_none=True),
        )
    except valuation_narrative.ValuationNarrativeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        llm_slots.release(token)


@router.post("/{code}/interpret/stream")
async def interpret_stream(code: str, payload: ValuationInterpretRequest,
                           request: Request):
    """`interpret`, streamed: the model's reasoning live, then the validated
    explanation (cloud #53). Same prompt, validator and correction words as
    the blocking route — `prepare_interpretation` builds both — so the two
    cannot hold an answer to different rules. The blocking route stays for
    the self-hosted frontend, which still calls it.

    Errors after the stream has begun arrive as an `error` SSE frame, not an
    HTTP status: by then the 200 has been sent.
    """
    try:
        inputs = payload.resolved_inputs()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    model = await run_in_threadpool(ollama_models.active_model)
    events = llm_stream.stream_validated_json(
        llm_json.async_client(valuation_narrative.VALUATION_TIMEOUT),
        model=model,
        **valuation_narrative.prepare_interpretation(
            code=code, model_kind=payload.model_kind, inputs=inputs,
            computed=payload.computed.model_dump(exclude_none=True)),
    )
    return llm_stream.sse_response(
        events,
        request=request,
        slot_label=f"valuation {code}",
        meta={"model": model, "model_kind": payload.model_kind},
        result_extra={"code": code, "model_kind": payload.model_kind, "model": model},
        errors=(valuation_narrative.ValuationNarrativeError,),
    )
