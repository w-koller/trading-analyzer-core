"""Push subscription management, and a test send.

Guarded like everything else: a subscription is a delivery address for this
account's position alerts, so an anonymous caller must not be able to register
one and start receiving them.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import db
from app.config import settings
from app.services import push_service, web_push
from app.services.moomoo_trade_gateway import get_trade_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class SubscriptionIn(BaseModel):
    """Exactly the shape `PushSubscription.toJSON()` produces in the browser,
    so the client can post it through without reshaping."""
    endpoint: str = Field(min_length=1)
    keys: SubscriptionKeys


@router.get("/status")
async def status() -> dict[str, Any]:
    """What the settings toggle needs to render itself.

    The public key is returned here as well as being baked into the bundle, so
    a key rotation is visible to a running client instead of silently making
    every new subscription unroutable.
    """
    subs = await run_in_threadpool(db.list_push_subscriptions)
    return {
        "configured": web_push.configured(),
        "public_key": settings.vapid_public_key or None,
        "subscriptions": len(subs),
        "min_severity": settings.push_min_severity,
    }


@router.post("/subscribe")
async def subscribe(payload: SubscriptionIn, request: Request) -> dict[str, Any]:
    if not web_push.configured():
        raise HTTPException(
            status_code=503,
            detail="Push is not configured on this server. "
                   "Run scripts/generate_vapid.py.")

    await run_in_threadpool(
        db.upsert_push_subscription,
        payload.endpoint, payload.keys.p256dh, payload.keys.auth,
        request.headers.get("user-agent"),
    )
    logger.info("push: subscribed %s", payload.endpoint[:48])
    return {"ok": True}


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1)


@router.post("/unsubscribe")
async def unsubscribe(payload: UnsubscribeIn) -> dict[str, Any]:
    removed = await run_in_threadpool(db.delete_push_subscription, payload.endpoint)
    return {"ok": True, "removed": removed}


@router.post("/test")
async def test_push() -> dict[str, Any]:
    """Send a notification to every registered device.

    Worth having as a first-class endpoint rather than a debugging trick:
    push has a long, silent failure chain — permission, service worker,
    VAPID keys, the push service itself — and "did it arrive?" is the only
    question that tests all of it at once.
    """
    subs = await run_in_threadpool(db.list_push_subscriptions)
    if not subs:
        raise HTTPException(status_code=400,
                            detail="No devices are subscribed to notifications.")

    payload = {
        "title": "Trading Analyzer",
        "body": "Test notification — alerts will look like this.",
        "tag": "test",
        "severity": "info",
        "url": "/",
    }
    tally = {"sent": 0, "gone": 0, "failed": 0}
    for sub in subs:
        outcome = await run_in_threadpool(web_push.send_one, sub, payload)
        tally[outcome] += 1
        if outcome == "gone":
            await run_in_threadpool(db.delete_push_subscription, sub["endpoint"])

    return {"ok": tally["sent"] > 0, **tally}


@router.post("/run")
async def run_now() -> dict[str, Any]:
    """Force a push cycle instead of waiting for the scheduler."""
    return await run_in_threadpool(push_service.run_push_cycle, get_trade_gateway())
