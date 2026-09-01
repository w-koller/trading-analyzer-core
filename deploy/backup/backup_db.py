#!/usr/bin/env python3
"""Back up trading.db, safely, while the backend keeps running.

Uses SQLite's online backup API rather than copying the file. A plain `cp` is
wrong here for two reasons: the database runs in WAL mode, so recent commits
live in the `-wal` sidecar that a single-file copy silently omits, and a copy
taken mid-write can be torn. `Connection.backup()` handles both and needs no
service downtime.

The stdlib is used deliberately: the `sqlite3` CLI is not installed on this
box, so the shell `VACUUM INTO` route would mean adding a package just to
take a backup.

Run from a systemd timer, not from the app's own scheduler — a backup that
shares a failure domain with the thing it protects is missing exactly when
you need it.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(levelname)-7s | backup_db | %(message)s"
)
logger = logging.getLogger(__name__)

HOURLY_PREFIX = "trading-"
DAILY_PREFIX = "daily-"


def _prune(backup_dir: Path, prefix: str, keep: int) -> int:
    files = sorted(backup_dir.glob(f"{prefix}*.db.gz"))
    removed = 0
    for stale in files[:-keep] if keep else files:
        stale.unlink()
        removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/opt/trading-analyzer/core/backend/data/trading.db")
    parser.add_argument(
        "--dest", default="/opt/trading-analyzer/core/backend/data/backups"
    )
    parser.add_argument("--keep-hourly", type=int, default=72)
    parser.add_argument("--keep-daily", type=int, default=14)
    args = parser.parse_args()

    source = Path(args.db)
    if not source.exists():
        logger.error("source database does not exist: %s", source)
        return 1

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    is_daily = datetime.now(timezone.utc).hour == 3
    prefix = DAILY_PREFIX if is_daily else HOURLY_PREFIX
    final = dest_dir / f"{prefix}{stamp}.db.gz"

    with tempfile.TemporaryDirectory(dir=str(dest_dir)) as tmp:
        staged = Path(tmp) / "snapshot.db"

        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(staged))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # Verify the copy, not the original — an unverified backup is a guess.
        check = sqlite3.connect(str(staged))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            logger.error("integrity_check on the snapshot failed: %s", result)
            return 1

        with staged.open("rb") as raw, gzip.open(final, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(raw, gz)

    hourly_gone = _prune(dest_dir, HOURLY_PREFIX, args.keep_hourly)
    daily_gone = _prune(dest_dir, DAILY_PREFIX, args.keep_daily)

    logger.info(
        "wrote %s (%.1f KB, integrity ok); pruned %d hourly, %d daily",
        final.name, final.stat().st_size / 1024, hourly_gone, daily_gone,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
