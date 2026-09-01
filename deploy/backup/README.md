# Database backups

Hourly snapshots of `backend/data/trading.db`, taken by
`trading-db-backup.timer` → `backup_db.py`.

- **Where:** `backend/data/backups/` (gitignored)
- **Retention:** 72 hourly (`trading-*.db.gz`) + 14 daily (`daily-*.db.gz`,
  promoted from the 03:00 UTC run)
- **Size:** ~10 KB gzipped each, so the whole set is well under 10 MB

## Why it works the way it does

The database runs in WAL mode, so recent commits live in a `-wal` sidecar
that a single-file `cp` would silently omit — and a copy taken mid-write can
be torn. `sqlite3.Connection.backup()` handles both correctly, online, with
no need to stop the backend.

Every snapshot is `PRAGMA integrity_check`ed **after** it is written, and the
run fails loudly if the check does not return `ok`. A backup you have never
verified is a guess, not a backup.

The timer is deliberately systemd's rather than an APScheduler job inside the
app: a backup that shares a failure domain with the thing it protects is
missing exactly when you need it.

## Checking on it

```bash
systemctl list-timers trading-db-backup.timer
journalctl -u trading-db-backup --since today
ls -la /opt/trading-analyzer/core/backend/data/backups/
```

Force one now:

```bash
systemctl start trading-db-backup.service
```

## Restoring

**Move all three files aside, not just the `.db`.** Leaving a stale `-wal` or
`-shm` next to a restored database is how you get a file that opens fine and
contains the wrong data.

```bash
systemctl stop trading-backend

cd /opt/trading-analyzer/core/backend/data
mkdir -p /tmp/db-rollback
mv trading.db trading.db-wal trading.db-shm /tmp/db-rollback/ 2>/dev/null

gunzip -c backups/daily-20260823T030000Z.db.gz > trading.db
chown trading:trading trading.db

systemctl start trading-backend
curl -s localhost:8000/readyz
```

Then confirm the data is what you expected before deleting the rollback copy:

```bash
/opt/trading-analyzer/core/backend/.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("/opt/trading-analyzer/core/backend/data/trading.db")
for t in ("trade_setups", "trade_outcomes", "watchlist_cache", "scanner_runs"):
    print(t, c.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
EOF
```

## Off-box copies

These snapshots live on the same LXC rootfs as the database, so they cover
operator error and corruption but **not** a `pct destroy` or a rootfs loss.
Check whether the Proxmox host already `vzdump`s this container; if it does,
the two together are a reasonable homelab pairing. If it does not, add an
off-box copy.
