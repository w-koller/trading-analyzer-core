#!/usr/bin/env bash
# Restart the backend when it is alive but not answering.
#
# systemd already restarts a process that *dies*. What it cannot see is a
# process that is running, holding its port, and wedged — the failure mode
# that looks like "the backend is down" while every process check says fine.
#
# Two things this must NOT do:
#
#   1. Fire during a legitimate long scan. A full-watchlist scan takes over
#      an hour and a restart throws all of it away. The primary defence is
#      that /livez does no IO and never touches the threadpool, so a scan
#      cannot make it slow; the scan_in_progress check below is a second
#      layer, not the main one.
#   2. Storm. If restarting is not fixing it, restarting harder will not
#      either — after a few attempts this backs off to logging only, so the
#      journal shows a persistent fault instead of a restart loop hiding it.

set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
UNIT="${UNIT:-trading-backend.service}"
STATE_DIR="${RUNTIME_DIRECTORY:-/run/trading-watchdog}"

FAIL_THRESHOLD=3          # consecutive /livez failures before acting
FAIL_THRESHOLD_SCANNING=5 # stricter while a scan is in flight
COOLDOWN_SECONDS=900      # 15 min between restarts
MAX_RESTARTS_PER_HOUR=3
PROBE_TIMEOUT=5
# 2x the 45s worst-case OpenD call, plus margin. Alert threshold only.
WEDGE_ALERT_SECONDS=120

FAIL_FILE="$STATE_DIR/consecutive_failures"
LAST_RESTART_FILE="$STATE_DIR/last_restart"
RESTART_LOG="$STATE_DIR/restart_times"

mkdir -p "$STATE_DIR"
now=$(date +%s)
failures=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)

# --- wedged gateway: alive, answering, and unable to do any work ------
#
# /livez deliberately does no IO, so a jammed OpenD gateway passes every
# liveness probe while every scan fails per-ticker. That state is invisible
# to a process check and to /livez by design, so it is caught here instead.
#
# Alert-only on purpose. The threshold below is a first guess against a 45s
# worst-case call, and a restart costs an in-flight scan — so this earns its
# restart privileges only after the logs show it firing on real wedges and
# not on slow paginated kline fetches.
check_gateway_wedge() {
    local readyz held scanning
    readyz=$(curl -sf --max-time "$PROBE_TIMEOUT" "$BASE_URL/readyz" 2>/dev/null) || return 0
    held=$(sed -n 's/.*"lock_held_seconds": *\([0-9.]*\).*/\1/p' <<<"$readyz" | head -1)
    scanning=$(grep -q '"scan_in_progress": *true' <<<"$readyz" && echo yes || echo no)
    [ -z "$held" ] && return 0
    # Integer compare; a long hold during a scan is normal work, not a wedge.
    if [ "${held%.*}" -gt "$WEDGE_ALERT_SECONDS" ] && [ "$scanning" = "no" ]; then
        echo "ERROR: gateway lock held ${held}s with no scan running — OpenD" \
             "may be wedged. Scans will fail per-ticker while this persists." >&2
    fi
}

if curl -sf --max-time "$PROBE_TIMEOUT" "$BASE_URL/livez" >/dev/null 2>&1; then
    [ "$failures" -gt 0 ] && echo "livez recovered after $failures failure(s)"
    echo 0 >"$FAIL_FILE"
    check_gateway_wedge
    exit 0
fi

failures=$((failures + 1))
echo "$failures" >"$FAIL_FILE"
echo "livez probe failed ($failures consecutive)"

# A scan in flight raises the bar rather than granting immunity: the backend
# could still be genuinely wedged mid-scan.
threshold=$FAIL_THRESHOLD
if readyz=$(curl -sf --max-time "$PROBE_TIMEOUT" "$BASE_URL/readyz" 2>/dev/null); then
    if grep -q '"scan_in_progress": *true' <<<"$readyz"; then
        threshold=$FAIL_THRESHOLD_SCANNING
        echo "a scan is in progress — requiring $threshold failures before restarting"
    fi
fi

[ "$failures" -lt "$threshold" ] && exit 0

last_restart=$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo 0)
if [ $((now - last_restart)) -lt "$COOLDOWN_SECONDS" ]; then
    echo "would restart $UNIT, but within the ${COOLDOWN_SECONDS}s cooldown — skipping"
    exit 0
fi

# Rolling hour window.
recent=0
if [ -f "$RESTART_LOG" ]; then
    recent=$(awk -v cutoff=$((now - 3600)) '$1 > cutoff' "$RESTART_LOG" | wc -l)
    awk -v cutoff=$((now - 3600)) '$1 > cutoff' "$RESTART_LOG" >"$RESTART_LOG.tmp" \
        && mv "$RESTART_LOG.tmp" "$RESTART_LOG"
fi

if [ "$recent" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    echo "ERROR: $UNIT has been restarted $recent times in the last hour and is" \
         "still failing /livez. Not restarting again — this is not transient." >&2
    exit 1
fi

echo "ERROR: restarting $UNIT after $failures consecutive /livez failures" >&2
systemctl restart "$UNIT"
echo "$now" >"$LAST_RESTART_FILE"
echo "$now" >>"$RESTART_LOG"
echo 0 >"$FAIL_FILE"
