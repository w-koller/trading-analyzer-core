#!/usr/bin/env bash
# Refuse to run a Next build/dev that would collide with a live server.
#
# This exists because of a real incident: `npm run build` was run while
# `next dev` was serving, the build replaced .next/ underneath it, and the
# dev server carried on emitting SSR HTML while every /_next/static chunk
# 404'd. The page rendered as unstyled text with no data, which looks
# exactly like a dead backend — the backend was fine the whole time, and the
# wrong thing got debugged for a while as a result.
#
# Usage: guard_next.sh build | dev
set -uo pipefail
MODE="${1:-build}"

if [ "${ALLOW_NEXT_GUARD_BYPASS:-0}" = "1" ]; then
    echo "guard_next: bypassed via ALLOW_NEXT_GUARD_BYPASS=1" >&2
    exit 0
fi

fail() {
    echo "" >&2
    echo "  ✗ guard_next: $1" >&2
    echo "    $2" >&2
    echo "" >&2
    echo "    Override with ALLOW_NEXT_GUARD_BYPASS=1 if you are certain." >&2
    echo "" >&2
    exit 1
}

if [ "$MODE" = "build" ]; then
    if pgrep -af "next dev" >/dev/null 2>&1; then
        fail "a 'next dev' server is running." \
             "Building now would overwrite .next/ underneath it and leave the running server serving 404s for every chunk. Stop it first."
    fi
    if systemctl is-active --quiet trading-frontend 2>/dev/null; then
        fail "trading-frontend.service is active." \
             "Use deploy/rebuild_frontend.sh, which stops the service, rebuilds, restarts, and verifies the chunks actually load."
    fi
fi

if [ "$MODE" = "dev" ]; then
    if systemctl is-active --quiet trading-frontend 2>/dev/null; then
        fail "trading-frontend.service is active and owns port 3000." \
             "Run 'systemctl stop trading-frontend' before starting a dev server."
    fi
fi

exit 0
