#!/usr/bin/env bash
# ensure-chrome.sh — idempotent launcher for a Chrome instance with CDP enabled.
#
# Behavior:
#   • If Chrome's DevTools Protocol is already reachable on
#     http://127.0.0.1:$CHROME_CDP_PORT, skips launch. Then verifies the
#     tab URLs passed in arg order exist, opening any missing ones via
#     the CDP /json/new endpoint.
#   • Otherwise launches Google Chrome with:
#       --remote-debugging-port=$CHROME_CDP_PORT
#       --user-data-dir=$TMPDIR/nova-chrome-cdp   (isolated profile)
#       --no-first-run --no-default-browser-check
#     plus each URL argument as an initial tab.
#     Waits up to 8s for CDP to become reachable.
#
# The isolated --user-data-dir keeps this Chrome separate from the user's
# personal profile (no extensions leaking in, no session contamination).
#
# Intended usage:
#   scripts/ensure-chrome.sh http://localhost:3000 http://localhost:3333
#
# Safe to call many times per session. All arguments must be full URLs
# (including the scheme).

set -euo pipefail

PORT="${CHROME_CDP_PORT:-9222}"
CDP_URL="http://127.0.0.1:${PORT}"
CHROME_APP="${CHROME_APP:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${TMPDIR:-/tmp}/nova-chrome-cdp}"
LOG_FILE="${TMPDIR:-/tmp}/nova-chrome-cdp.log"
PID_FILE="${TMPDIR:-/tmp}/nova-chrome-cdp.pid"

# Helper: URL-encode one string. Python3 is available everywhere on macOS.
url_encode() {
  python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

# Helper: does CDP have a tab whose URL starts with $1?
has_tab() {
  local prefix="$1"
  curl -sf --max-time 2 "${CDP_URL}/json" 2>/dev/null | python3 -c "
import sys, json
try:
    tabs = json.load(sys.stdin)
except Exception:
    sys.exit(2)
prefix = sys.argv[1]
sys.exit(0 if any((t.get('url') or '').startswith(prefix) for t in tabs) else 1)
" "$prefix"
}

# Helper: open a new tab at $1 via CDP.
open_tab() {
  local url="$1"
  local enc; enc="$(url_encode "$url")"
  curl -sf --max-time 3 -X PUT "${CDP_URL}/json/new?${enc}" >/dev/null \
    || curl -sf --max-time 3 "${CDP_URL}/json/new?${enc}" >/dev/null
}

# ------------------------------------------------------------------ #
# 1. Already running?
# ------------------------------------------------------------------ #
if curl -sfo /dev/null --max-time 1 "${CDP_URL}/json/version" 2>/dev/null; then
  echo "chrome: CDP already active on :${PORT}"
  # Open any required tabs that aren't there yet.
  for url in "$@"; do
    if ! has_tab "$url"; then
      echo "chrome: opening missing tab ${url}"
      open_tab "$url" || echo "chrome: warn — could not open ${url} (non-fatal)"
    fi
  done
  exit 0
fi

# ------------------------------------------------------------------ #
# 2. Not running — launch.
# ------------------------------------------------------------------ #
if [ ! -x "${CHROME_APP}" ]; then
  echo "chrome: ERROR — Chrome binary not found at ${CHROME_APP}" >&2
  echo "        Set CHROME_APP=/path/to/Google\\ Chrome to override." >&2
  exit 1
fi

mkdir -p "${USER_DATA_DIR}"

echo "chrome: launching with CDP on :${PORT} (user-data-dir=${USER_DATA_DIR})..."
nohup "${CHROME_APP}" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${USER_DATA_DIR}" \
  --no-first-run \
  --no-default-browser-check \
  --start-maximized \
  "$@" \
  > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"

# ------------------------------------------------------------------ #
# 3. Wait up to ~8 s for CDP.
# ------------------------------------------------------------------ #
for _ in $(seq 1 32); do
  if curl -sfo /dev/null --max-time 1 "${CDP_URL}/json/version" 2>/dev/null; then
    echo "chrome: CDP ready on :${PORT} (pid $(cat "${PID_FILE}" 2>/dev/null || echo '?'))"
    exit 0
  fi
  sleep 0.25
done

echo "chrome: ERROR — CDP did not become ready within 8 s. Check ${LOG_FILE}" >&2
exit 1
