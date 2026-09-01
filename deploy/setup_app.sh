#!/usr/bin/env bash
# Run this INSIDE the trading-analyzer LXC as root (provision_lxc.sh does
# this for you via `pct exec`). Idempotent — safe to re-run for updates.

set -euo pipefail

APP_DIR="/opt/trading-analyzer"
# The repo is a monorepo as of 2026-09-01: the self-hosted app lives under
# core/, and cloud/ holds the Informed Trader deployment. APP_DIR stays the
# CLONE root because `git clone` targets it; CORE_DIR is what every
# application path hangs off.
CORE_DIR="$APP_DIR/core"
OPEND_DIR="/opt/opend"
REPO_URL="${REPO_URL:?REPO_URL not set}"
BRANCH="${BRANCH:-main}"
SERVICE_USER="trading"

echo "== System packages =="
# Debian 12 (bookworm) ships Python 3.11 in its default apt repos, not 3.12 —
# using the generic python3/python3-venv packages rather than a hardcoded
# version keeps this script correct across Debian point releases and future
# rebuilds. Confirm the actual version with `python3 --version` after this.
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  nodejs npm \
  git curl ca-certificates

echo "== Service user =="
id -u "$SERVICE_USER" &>/dev/null || \
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

echo "== App code =="
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "== Backend venv =="
cd "$CORE_DIR/backend"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== Backend .env =="
if [[ ! -f "$CORE_DIR/backend/.env" ]]; then
  cp "$CORE_DIR/backend/.env.example" "$CORE_DIR/backend/.env"
  echo "  -> wrote default .env, edit before starting: $CORE_DIR/backend/.env"
fi
mkdir -p "$CORE_DIR/backend/data"

echo "== OpenD =="
mkdir -p "$OPEND_DIR"
if [[ ! -f "$OPEND_DIR/OpenD" ]]; then
  echo "  -> OpenD binary not found at $OPEND_DIR/OpenD."
  echo "     Download it from the Moomoo OpenAPI portal and place it there,"
  echo "     then run the first-time interactive login (see README)."
fi
if [[ ! -f "$OPEND_DIR/opend.env" ]]; then
  cp "$CORE_DIR/deploy/opend.env.example" "$OPEND_DIR/opend.env"
  chmod 600 "$OPEND_DIR/opend.env"
  echo "  -> wrote default opend.env, edit MOOMOO_LOGIN_ACCOUNT before starting: $OPEND_DIR/opend.env"
fi

echo "== Frontend build =="
# Stop the service first: building over a running server replaces .next/
# underneath it and leaves it serving HTML whose every chunk 404s. Harmless
# on a first install, essential when re-running this against a live box.
systemctl stop trading-frontend 2>/dev/null || true
cd "$CORE_DIR/frontend"
npm ci
ALLOW_NEXT_GUARD_BYPASS=1 npm run build

echo "== Ownership =="
# After the build, so the freshly written .next/ is covered too.
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$OPEND_DIR"
chmod 640 "$CORE_DIR/backend/.env" 2>/dev/null || true

echo "== systemd units =="
# moomoo-opend, not opend: that is the name the running box uses, and the
# backend unit's After=/Wants= refer to it.
cp "$CORE_DIR/deploy/systemd/moomoo-opend.service" /etc/systemd/system/moomoo-opend.service
cp "$CORE_DIR/deploy/systemd/trading-backend.service" /etc/systemd/system/trading-backend.service
cp "$CORE_DIR/deploy/systemd/trading-frontend.service" /etc/systemd/system/trading-frontend.service
cp "$CORE_DIR/deploy/systemd/trading-watchdog.service" /etc/systemd/system/
cp "$CORE_DIR/deploy/systemd/trading-watchdog.timer" /etc/systemd/system/
cp "$CORE_DIR/deploy/systemd/trading-db-backup.service" /etc/systemd/system/
cp "$CORE_DIR/deploy/systemd/trading-db-backup.timer" /etc/systemd/system/

mkdir -p /etc/systemd/journald.conf.d
cp "$CORE_DIR/deploy/journald/10-trading.conf" /etc/systemd/journald.conf.d/
systemctl restart systemd-journald

echo "== Firewall: restrict :3000 to the reverse proxy =="
# The dashboard is public, and only the reverse proxy should ever open :3000.
# Leaving it open to the LAN is not cosmetic: the backend trusts
# X-Forwarded-For from 127.0.0.1 and the Next proxy passes the incoming header
# through, so any LAN client could forge one and mint a fresh per-IP
# login-lockout bucket per request. NPM overwrites the header with the real
# client address, which is what makes the limit mean anything.
#
# PROXY_IP must be the address of whatever terminates TLS in front of this box.
# Getting it wrong takes the dashboard offline, so it is checked below rather
# than assumed.
PROXY_IP="${PROXY_IP:-192.168.68.48}"
if command -v nft >/dev/null 2>&1; then
    mkdir -p /etc/nftables.d
    sed "s/192\.168\.68\.48/$PROXY_IP/" \
        "$CORE_DIR/deploy/nftables/trading.nft" | sed '1{/^#!/d}' \
        > /etc/nftables.d/trading.nft
    grep -q 'nftables.d' /etc/nftables.conf || printf '\ninclude "/etc/nftables.d/*.nft"\n' >> /etc/nftables.conf
    if nft -c -f /etc/nftables.conf; then
        nft -f /etc/nftables.conf
        systemctl enable --now nftables >/dev/null 2>&1 || true
        echo "  :3000 restricted to $PROXY_IP (and loopback)"
    else
        echo "  WARNING: nftables config did not validate; :3000 left open" >&2
        rm -f /etc/nftables.d/trading.nft
    fi
else
    echo "  WARNING: nft not installed; :3000 is reachable from the whole LAN" >&2
fi

chmod +x "$CORE_DIR/deploy/watchdog/trading-watchdog.sh" \
         "$CORE_DIR/deploy/rebuild_frontend.sh" \
         "$CORE_DIR/deploy/guard_next.sh"

systemctl daemon-reload
# --now, not a bare enable. Enabling without starting is why this box could be
# fully provisioned and still have nothing running until someone noticed.
systemctl enable --now moomoo-opend trading-backend trading-frontend
systemctl enable --now trading-watchdog.timer trading-db-backup.timer

echo ""
echo "Setup complete. Verify with:"
echo "  systemctl is-active moomoo-opend trading-backend trading-frontend"
echo "  curl -s localhost:8000/livez && curl -s localhost:8000/readyz"
echo "  nft list table inet trading_fw   # :3000 restricted to the proxy"
echo ""
echo "Then set the login credentials — until you do, every endpoint is closed:"
echo "  cd $CORE_DIR/backend && .venv/bin/python scripts/set_password.py"
