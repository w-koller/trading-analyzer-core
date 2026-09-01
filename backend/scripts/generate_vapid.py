#!/usr/bin/env python
"""Generate the VAPID keypair for Web Push.

    cd backend && .venv/bin/python scripts/generate_vapid.py

Run ONCE and keep the result. Regenerating invalidates every existing push
subscription: a browser's subscription is bound to the public key it was
created with, so new keys mean every installed PWA silently stops receiving
notifications until it re-subscribes. There is no error anywhere to tell you
this has happened — which is why it is worth saying here.

Prints the three .env lines plus the one NEXT_PUBLIC_ line the frontend needs.
Does not write either file: backend/.env holds the OpenD account and the API
token, and a script that rewrites it is one bug away from truncating the lot.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization            # noqa: E402
from py_vapid import Vapid01                                        # noqa: E402

DEFAULT_SUBJECT = "mailto:you@example.com"


def _b64(raw: bytes) -> str:
    """Base64url, unpadded — the encoding the Web Push spec and the browser
    PushManager both expect. Standard base64 will be rejected."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    vapid = Vapid01()
    vapid.generate_keys()

    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    public_b64 = _b64(public_raw)
    private_b64 = _b64(private_raw)

    subject = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SUBJECT

    print("=" * 72)
    print("Add to backend/.env:\n")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print(f"VAPID_SUBJECT={subject}")
    print("\n" + "=" * 72)
    print("Add to frontend/.env.local:\n")
    print(f"NEXT_PUBLIC_VAPID_PUBLIC_KEY={public_b64}")
    print("\n" + "=" * 72)
    if subject == DEFAULT_SUBJECT:
        print("\nNOTE: pass a real contact as the first argument —")
        print("      push services reject a JWT whose `sub` is not a real")
        print("      mailto: or https: URL, and some only reject it later.")
        print("      e.g. scripts/generate_vapid.py mailto:you@yourdomain\n")
    print("The PUBLIC key is meant to be public — it is handed to the browser's")
    print("PushManager by design. The PRIVATE key must never leave backend/.env.")
    print("\nThe frontend change is build-time: run deploy/rebuild_frontend.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
