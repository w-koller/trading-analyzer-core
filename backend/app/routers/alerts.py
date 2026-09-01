"""Alerts on held positions, and the acknowledgements that silence them."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app import db
from app.services import alerts as alerts_service
from app.services.moomoo_trade_gateway import get_trade_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def list_alerts() -> dict[str, Any]:
    return await run_in_threadpool(alerts_service.get_alerts, get_trade_gateway())


@router.post("/{fingerprint:path}/ack")
async def ack(fingerprint: str) -> dict[str, Any]:
    """Silence one alert until an absolute time.

    `:path` because a fingerprint is `rule:code:discriminator` and `code`
    contains a dot — plain `{fingerprint}` would still match, but the path
    converter makes it explicit that separators inside are data.

    The expiry is returned so the UI can say "silenced until Wednesday". A
    dismiss that reads as a delete is how a safety feature quietly becomes
    decorative.
    """
    parts = fingerprint.split(":")
    if len(parts) < 3:
        raise HTTPException(
            status_code=400,
            detail="A fingerprint is rule:code:discriminator.",
        )
    rule, code = parts[0], parts[1]

    # The severity is re-derived from the live alert rather than taken from
    # the client, because it decides the TTL — a client claiming 'info' on a
    # critical alert would buy itself 72 hours of silence instead of 12.
    payload = await run_in_threadpool(
        alerts_service.get_alerts, get_trade_gateway()
    )
    match = next((a for a in payload["alerts"] if a["id"] == fingerprint), None)
    severity = match["severity"] if match else "warn"

    expires = await run_in_threadpool(
        db.acknowledge_alert, fingerprint, rule, code, severity,
        alerts_service.ttl_for(severity),
    )
    return {"fingerprint": fingerprint, "acknowledged": True,
            "expires_at": expires, "severity": severity}


@router.delete("/{fingerprint:path}/ack")
async def unack(fingerprint: str) -> dict[str, Any]:
    removed = await run_in_threadpool(db.unacknowledge_alert, fingerprint)
    return {"fingerprint": fingerprint, "acknowledged": False, "removed": removed}
