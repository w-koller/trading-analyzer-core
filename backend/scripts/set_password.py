#!/usr/bin/env python
"""Generate the login credentials for backend/.env.

Prints the two lines to add. Deliberately does NOT write .env itself: that file
holds the OpenD account, the API token and the DB path, and a script that
rewrites it is one bug away from truncating the lot. Copy the output in.

    cd backend && .venv/bin/python scripts/set_password.py

The TOTP secret is printed once, as a QR-scannable otpauth:// URI. Scan it
before you close the terminal — it is not stored anywhere else, and losing it
means editing .env to enrol again.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyotp                                                    # noqa: E402
from app.services import auth_service                           # noqa: E402

MIN_LENGTH = 12


def main() -> int:
    print("Trading Analyzer — login credentials\n")

    pw = getpass.getpass("New password: ")
    if len(pw) < MIN_LENGTH:
        # This endpoint is reachable from the public internet. A short
        # password here is not a style preference.
        print(f"\nToo short — use at least {MIN_LENGTH} characters.",
              file=sys.stderr)
        return 1
    if pw != getpass.getpass("Confirm password: "):
        print("\nPasswords do not match.", file=sys.stderr)
        return 1

    hashed = auth_service.hash_password(pw)
    secret = pyotp.random_base32()
    uri = auth_service.totp_provisioning_uri(secret)

    print("\n" + "=" * 72)
    print("Add these to backend/.env (replacing any existing pair):\n")
    print(f"AUTH_PASSWORD_HASH={hashed}")
    print(f"AUTH_TOTP_SECRET={secret}")
    print("\n" + "=" * 72)
    print("\nEnrol your authenticator app with this URI:\n")
    print(f"  {uri}\n")
    print("Or type the secret in manually:", secret)
    print("\nThen restart the backend:  systemctl restart trading-backend\n")

    try:
        import qrcode                                           # noqa: F401
    except ImportError:
        print("(install `qrcode` if you would rather scan a QR code than type it)")
    else:
        qr = qrcode.QRCode()
        qr.add_data(uri)
        qr.make()
        qr.print_ascii(invert=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
