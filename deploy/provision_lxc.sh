#!/usr/bin/env bash
# Run this ON THE PROXMOX HOST as root.
# Creates (or reuses) an unprivileged LXC for the trading analyzer, then
# hands off to setup_app.sh to install everything inside it. Re-running
# this script after the container already exists just re-runs setup —
# safe to use for both first deploy and later updates.

set -euo pipefail

CTID="${CTID:-201}"
HOSTNAME_="${HOSTNAME_:-trading-analyzer}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst}"
STORAGE="${STORAGE:-local-lvm}"
DISK_GB="${DISK_GB:-16}"
MEM_MB="${MEM_MB:-2048}"
CORES="${CORES:-2}"
BRIDGE="${BRIDGE:-vmbr0}"
IP_CIDR="${IP_CIDR:-dhcp}"      # or e.g. 192.168.68.50/24
GATEWAY="${GATEWAY:-}"          # required if IP_CIDR is static
REPO_URL="${REPO_URL:?Set REPO_URL to your git remote, e.g. git@github.com:you/trading-analyzer.git}"
BRANCH="${BRANCH:-main}"

if pct status "$CTID" &>/dev/null; then
  echo "CTID $CTID already exists — reusing it. For a clean rebuild: pct stop $CTID && pct destroy $CTID"
else
  NET_OPTS="name=eth0,bridge=${BRIDGE},ip=${IP_CIDR}"
  [[ -n "$GATEWAY" ]] && NET_OPTS="${NET_OPTS},gw=${GATEWAY}"

  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME_" \
    --unprivileged 1 \
    --features nesting=0 \
    --cores "$CORES" \
    --memory "$MEM_MB" \
    --swap 512 \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --net0 "$NET_OPTS" \
    --onboot 1 \
    --start 1

  echo "Created and started CTID $CTID ($HOSTNAME_)."
  echo "Waiting for network..."
  sleep 5
  pct exec "$CTID" -- bash -c "until ping -c1 1.1.1.1 &>/dev/null; do sleep 1; done"
fi

echo "Pushing setup_app.sh into the container..."
pct push "$CTID" "$(dirname "$0")/setup_app.sh" /root/setup_app.sh

echo "Running in-container setup (REPO_URL=$REPO_URL, BRANCH=$BRANCH)..."
pct exec "$CTID" -- env REPO_URL="$REPO_URL" BRANCH="$BRANCH" bash /root/setup_app.sh

echo ""
echo "Done. Container $CTID's IP:"
pct exec "$CTID" -- ip -4 -o addr show eth0 | awk '{print $4}'
echo ""
echo "Remaining manual steps (see setup_app.sh output above):"
echo "  1. Drop the OpenD binary at /opt/opend/OpenD inside the container"
echo "  2. Run OpenD's first-time interactive login"
echo "  3. Edit /opt/trading-analyzer/core/backend/.env (Ollama URL, etc.)"
echo "  4. systemctl is-active moomoo-opend trading-backend trading-frontend   # setup_app.sh now starts them"
