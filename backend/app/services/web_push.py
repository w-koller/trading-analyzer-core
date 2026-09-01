"""Web Push delivery — VAPID + RFC-8291 aes128gcm, sent with httpx.

Deliberately not `pywebpush`. From 2.x it hard-depends on BOTH aiohttp and
requests, which is two more HTTP stacks (one of them compiled) on a 4 GiB box
that already standardises on httpx, in order to do what amounts to "POST an
encrypted blob with a signed JWT". The cryptography is not the part worth
outsourcing here — py-vapid signs the token and http-ece does the payload
encryption, and both are used directly below.

## The delivery contract

`send_one` returns one of three outcomes, and the distinction matters:

  sent    delivered to the push service (NOT to the device — the push service
          queues, and a phone that is off receives it later or not at all).
  gone    404/410. The subscription is dead: the PWA was uninstalled, or the
          browser rotated it. This is an ANSWER, not an error, and the row
          should be deleted rather than retried forever.
  failed  anything else. Transient until proven otherwise; the caller counts
          consecutive failures and gives up eventually.

## Payload size

Push services cap the encrypted payload (4KB is the common floor, and FCM
enforces it). The alert text here is two short strings, but `_encrypt` still
guards the limit rather than discovering it as an opaque 413 per subscription.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import urlparse

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from app.config import settings

logger = logging.getLogger(__name__)

# The conservative floor across push services. Chrome/FCM rejects above ~4096.
MAX_PAYLOAD_BYTES = 3800

# How long the push service should hold an undelivered message. A position
# alert is worth delivering to a phone that was off for an hour; it is not
# worth delivering a day later, by which point the fact has probably changed
# and the user would be acting on stale advice.
TTL_SECONDS = 6 * 3600

REQUEST_TIMEOUT = 10.0


def _unb64(raw: str) -> bytes:
    """Decode unpadded base64url, which is what the Push API uses throughout."""
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def configured() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key
                and settings.vapid_subject)


def _vapid() -> Vapid02:
    """Rebuild the signer from the stored raw private scalar.

    Vapid02, not Vapid01: draft-02 is RFC 8292 and sends a single
    `Authorization: vapid t=<jwt>, k=<key>` header. Vapid01 splits it across
    `Authorization: WebPush …` plus `Crypto-Key: p256ecdsa=…`, which some push
    services still accept and others no longer do.
    """
    scalar = int.from_bytes(_unb64(settings.vapid_private_key), "big")
    key = ec.derive_private_key(scalar, ec.SECP256R1())
    return Vapid02.from_pem(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


def _encrypt(payload: dict, p256dh: str, auth: str) -> bytes:
    """Encrypt for one subscription. A fresh ephemeral key per message —
    reusing one across sends would leak the relationship between them."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"push payload is {len(raw)}B, over the {MAX_PAYLOAD_BYTES}B limit")
    return http_ece.encrypt(
        raw,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_unb64(p256dh),
        auth_secret=_unb64(auth),
        version="aes128gcm",
    )


def send_one(subscription: dict, payload: dict) -> str:
    """Deliver one notification. Returns 'sent' | 'gone' | 'failed'."""
    if not configured():
        logger.warning("push: VAPID keys are not configured — "
                       "run scripts/generate_vapid.py")
        return "failed"

    endpoint = subscription["endpoint"]
    try:
        body = _encrypt(payload, subscription["p256dh"], subscription["auth"])
    except Exception as exc:                                     # noqa: BLE001
        # A malformed key is permanent, but it is not 'gone' — 'gone' means the
        # push service disowned it. Let the failure counter retire it.
        logger.error("push: cannot encrypt for %s: %s", endpoint[:48], exc)
        return "failed"

    # `aud` is the ORIGIN of the endpoint, not the full URL. Sending the whole
    # URL is a common mistake and fails as a 401 from the push service, which
    # reads like a credential problem rather than a claims problem.
    origin = urlparse(endpoint)
    headers = _vapid().sign({
        "aud": f"{origin.scheme}://{origin.netloc}",
        "sub": settings.vapid_subject,
    })
    headers.update({
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        # Position alerts are worth waking a screen for; that is the whole
        # point of the feature. Anything lower can be delayed by the OS.
        "Urgency": "high",
    })

    try:
        resp = httpx.post(endpoint, content=body, headers=headers,
                          timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("push: transport error for %s: %s", endpoint[:48], exc)
        return "failed"

    if resp.status_code in (404, 410):
        logger.info("push: subscription gone (%s) for %s",
                    resp.status_code, endpoint[:48])
        return "gone"
    if 200 <= resp.status_code < 300:
        return "sent"

    logger.warning("push: %s from %s — %s", resp.status_code,
                   endpoint[:48], resp.text[:200])
    return "failed"
