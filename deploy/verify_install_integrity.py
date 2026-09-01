#!/usr/bin/env python3
"""Check every installed Python file against the hash its own RECORD declares.

    core/backend/.venv/bin/python core/deploy/verify_install_integrity.py
    core/backend/.venv/bin/python core/deploy/verify_install_integrity.py --fix

**Run this FIRST whenever something on this box misbehaves in a way that makes
no sense.** It has now paid for itself twice:

  * 2026-08-24 — numpy's bundled libgfortran had three bytes changed, every
    one xor 0x10, inside a 617-byte span. `import numpy` failed with an
    undefined symbol; the backend only kept running because it already had the
    library mapped, so a restart would have crash-looped it.
  * 2026-08-25 — pandas' `_libs/lib...so` had 15 single-bit flips, all
    xor 0x08 or xor 0x10, in a 22KB span. `import pandas` segfaulted at
    interpreter shutdown roughly 20% of the time, and (separately installed)
    argon2 returned WRONG password hashes ~10% of the time, which would have
    rejected correct logins at random with nothing in any log.

The signature is the giveaway: a handful of SINGLE-BIT flips at the same bit
positions, clustered in one span. That is hardware, not software. RAM was
ruled out both times (611 GiB of write-and-verify, zero errors), so the fault
is somewhere in the write path to disk — the host still wants a memtest and a
look at the storage layer.

Note two benign classes of mismatch this reports:
  * `__pycache__/*.pyc` — regenerated locally, so they legitimately differ.
  * a very large file may hash differently once under memory pressure; re-run
    before believing it, and compare against the wheel to be sure.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import subprocess
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "backend/.venv/lib/python3.11/site-packages"


def scan(site: Path) -> list[tuple[str, str]]:
    bad: list[tuple[str, str]] = []
    checked = 0
    for record in sorted(site.glob("*.dist-info/RECORD")):
        dist = record.parent.name.split("-")[0]
        for row in csv.reader(record.open()):
            if len(row) < 2 or not row[1].startswith("sha256="):
                continue
            f = site / row[0]
            if not f.is_file():
                continue
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(f.read_bytes()).digest()).rstrip(b"=").decode()
            checked += 1
            if digest != row[1].split("=", 1)[1]:
                bad.append((dist, row[0]))
    print(f"checked {checked:,} files across {len(list(site.glob('*.dist-info')))} packages")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="reinstall affected packages with --force-reinstall")
    args = ap.parse_args()

    bad = scan(SITE)
    if not bad:
        print("OK — every installed file matches its recorded hash")
        return 0

    print(f"\n{len(bad)} MISMATCHED file(s):")
    dists = sorted({d for d, _ in bad})
    for dist, path in bad:
        note = "  (pyc, regenerated locally — usually benign)" if ".pyc" in path else ""
        print(f"  {dist:22s} {path}{note}")

    if not args.fix:
        print("\nRe-run to rule out a one-off hashing error, then --fix to reinstall:")
        print(f"  {sys.executable} {__file__} --fix")
        return 1

    # --force-reinstall --no-deps: replace exactly these distributions and do
    # not let a transitive resolve pull anything else in behind them.
    print(f"\nreinstalling: {' '.join(dists)}")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "-q",
                          "--force-reinstall", "--no-deps", *dists])
    if rc != 0:
        print("reinstall FAILED", file=sys.stderr)
        return rc
    print("\nre-verifying...")
    return 0 if not scan(SITE) else 1


if __name__ == "__main__":
    raise SystemExit(main())
