#!/usr/bin/env bash
# ensure-chart.sh — idempotent starter for the AntV chart MCP server.
#
# Starts `@antv/mcp-server-chart` in streamable-HTTP mode on
# $CHART_PORT (default 1122). On the first call of the session this
# spins up Node + npx (once — npx caches the package after the first
# launch); on every subsequent call it's a fast curl-based no-op.
#
# Once running, any caller can fire a chart generation with:
#
#   curl -s -X POST http://localhost:1122/mcp \
#     -H 'Content-Type: application/json' \
#     -H 'Accept: application/json, text/event-stream' \
#     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
#          "params":{"name":"generate_line_chart","arguments":{...}}}'
#
# Prefer the wrapper `scripts/chart-call.sh` which handles that boilerplate.
#
# This script intentionally mirrors the structure of ensure-visor.sh so the
# two orchestration steps remain easy to read side-by-side.

set -euo pipefail

PORT="${CHART_PORT:-1122}"
URL="http://localhost:${PORT}"
LOG_FILE="${TMPDIR:-/tmp}/antv-chart-mcp.log"
PID_FILE="${TMPDIR:-/tmp}/antv-chart-mcp.pid"
PACKAGE="${CHART_PACKAGE:-@antv/mcp-server-chart@0.9.10}"

# 1. Liveness probe. The streamable endpoint accepts POST only, but an
#    empty POST gives us a fast, cheap 400 — if we get ANY HTTP response
#    the server is up. curl's `-w '%{http_code}'` already emits "000" on
#    connection failure, so we don't need a fallback — adding one
#    (e.g. `|| echo "000"`) would double-print the code on failure and
#    silently break the "is it running?" check.
probe() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 1 \
    -X POST "${URL}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{}' 2>/dev/null || true
}

if [ "$(probe)" != "000" ]; then
  echo "chart: already running at ${URL}"
  exit 0
fi

# 2. Launch detached. npx caches the package on first run; subsequent
#    sessions bypass the npm-registry round-trip.
#
#    --host 0.0.0.0 forces IPv4 binding. Without it, @antv/mcp-server-chart
#    defaults to "localhost" which Node resolves IPv6-only (::1) on macOS,
#    so our Python httpx client (which hits 127.0.0.1) gets ECONNREFUSED.
#    See commit history / root-cause note in src/clients/antv_chart.py.
echo "chart: starting ${PACKAGE} on port ${PORT}..."
nohup npx --yes "${PACKAGE}" -t streamable -p "${PORT}" -h 0.0.0.0 \
  > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"

# 3. Wait up to ~15s (60 × 250ms) for readiness. First-time npx install
#    can take 5–10s on a cold machine; warmed up it's ~1s.
for _ in $(seq 1 60); do
  if [ "$(probe)" != "000" ]; then
    echo "chart: ready at ${URL} (pid $(cat "${PID_FILE}" 2>/dev/null || echo '?'))"
    exit 0
  fi
  sleep 0.25
done

echo "chart: did not become ready within 15s — check ${LOG_FILE}" >&2
exit 1
