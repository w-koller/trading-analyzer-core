#!/usr/bin/env bash
# The sanctioned way to ship a frontend change.
#
# Does the steps in the only order that is safe, and then actually checks
# that the app works — not merely that a page came back. An SSR response
# with a 200 proves almost nothing: the incident this script exists to
# prevent produced perfectly valid HTML whose every script and stylesheet
# 404'd.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trading-analyzer}"
FRONTEND="$APP_DIR/core/frontend"
URL="${URL:-http://127.0.0.1:3000}"

echo "==> stopping trading-frontend"
systemctl stop trading-frontend 2>/dev/null || true

echo "==> clean build"
cd "$FRONTEND"
rm -rf .next

# Retried, because this box fails builds NON-DETERMINISTICALLY. Observed
# 2026-08-31: from one unchanged tree, consecutive builds gave a SIGSEGV in
# the build worker, "SyntaxError: missing ) after argument list" with no
# stack, and a css-loader parse error on a globals.css that was byte-identical
# to git — then succeeded, then SIGSEGV'd again. Same class of fault as the
# single-bit flips CLAUDE.md records for numpy and pandas.
#
# A retry is honest here, not a papering-over: the input is identical each
# time, so a build that passes on attempt 2 is as valid as one that passes on
# attempt 1. If ALL attempts fail, that is a real signal — see below.
#
# Five, not three: measured on the bad afternoon above, roughly one build in
# three succeeded, and a run of three consecutive failures happened twice.
build_ok=0
for attempt in 1 2 3 4 5; do
    if ALLOW_NEXT_GUARD_BYPASS=1 npm run build; then
        [ "$attempt" -gt 1 ] && echo "    (build succeeded on attempt $attempt)"
        build_ok=1
        break
    fi
    echo "  ! build attempt $attempt failed" >&2
    rm -rf .next
done

# The service is ALREADY STOPPED and .next is ALREADY GONE by this point, so
# bailing out silently here leaves the dashboard down with no explanation.
# That really happened. Say so, loudly, and say what to try.
if [ "$build_ok" -ne 1 ]; then
    echo "" >&2
    echo "  ✗ THE BUILD FAILED 5 TIMES. THE FRONTEND IS NOW DOWN." >&2
    echo "" >&2
    echo "    If the errors differ between attempts (SIGSEGV, then a syntax" >&2
    echo "    error in a file git says is clean), suspect the box, not the" >&2
    echo "    code. What recovered it on 2026-08-31:" >&2
    echo "" >&2
    echo "      npm cache verify        # GC'd 92 corrupt entries" >&2
    echo "      rm -rf node_modules && npm ci" >&2
    echo "" >&2
    echo "    A plain 'npm ci' is NOT enough — it reinstalls the same bad" >&2
    echo "    bytes from the cache, which is why the failure looked" >&2
    echo "    reproducible when the underlying fault is intermittent." >&2
    echo "" >&2
    exit 1
fi

echo "==> fixing ownership for the service user"
chown -R trading:trading "$FRONTEND/.next"

echo "==> starting trading-frontend"
systemctl start trading-frontend

echo "==> waiting for the server"
for _ in $(seq 1 30); do
    curl -sf --max-time 3 "$URL" >/dev/null 2>&1 && break
    sleep 1
done

echo "==> verifying static chunks actually load"
# -L is load-bearing. Since login became mandatory (decisions #56) "/" answers
# an unauthenticated request with a 307 to /login and a 6-byte body, so curl
# WITHOUT -L scraped no asset URLs and this check bailed with "references no
# static assets" on every single run. That is worse than having no check at
# all: one that always cries wolf is one nobody reads on the day a chunk
# really is missing, which is the exact incident this script exists to catch.
#
# Landing on /login is fine for the purpose. The failure mode being guarded
# against is .next/ being replaced under a running server, which 404s every
# chunk at once — and /login pulls the shared webpack, main-app, polyfills,
# layout and CSS bundles, so it sees that immediately. It does NOT prove the
# authenticated pages' own chunks are present; nothing reachable without a
# session could.
effective=$(curl -sL -o /dev/null -w '%{url_effective}' --max-time 15 "$URL")
html=$(curl -sL --max-time 15 "$URL")
echo "    verifying $effective"
assets=$(grep -oE '/_next/static/[^"]+\.(css|js)' <<<"$html" | sort -u | head -8)
if [ -z "$assets" ]; then
    echo "  ✗ $effective references no static assets at all — something is very wrong" >&2
    exit 1
fi

failed=0
while read -r asset; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL$asset")
    if [ "$code" != "200" ]; then
        echo "  ✗ $code $asset" >&2
        failed=1
    else
        echo "  ✓ $code $asset"
    fi
done <<<"$assets"

if [ "$failed" -ne 0 ]; then
    echo "" >&2
    echo "  ✗ chunks are missing. The page will render as unstyled text with no data." >&2
    exit 1
fi

echo "==> frontend rebuilt and verified"
