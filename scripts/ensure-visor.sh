#!/usr/bin/env bash
# ensure-visor.sh — idempotent starter for the Finalysis visor web viewer.
#
# Behavior:
#   • If the visor is already responding on $VISOR_PORT (default 3333),
#     skips its boot step — a no-op.
#   • Otherwise starts visor/server.mjs detached (nohup + &), waits up to
#     ~10s for it to become ready, then opens the default browser at
#     http://localhost:$PORT.
#   • In either case, calls ensure-chart.sh last so the AntV chart MCP
#     server is also warm on port $CHART_PORT (default 1122). Keeping
#     the rendering stack together behind one entrypoint means the
#     agent only needs to know about ensure-visor.sh.
#
# Intended usage: invoked at the start of every agent request. Safe to call
# many times per session — only the first call actually starts anything.

set -euo pipefail

PORT="${VISOR_PORT:-3333}"
URL="http://localhost:${PORT}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VISOR_DIR="${ROOT_DIR}/visor"
LOG_FILE="${TMPDIR:-/tmp}/finalysis-visor.log"
PID_FILE="${TMPDIR:-/tmp}/finalysis-visor.pid"

# 1. Already running? Skip visor boot but still make sure the chart stack is warm.
if curl -sfo /dev/null --max-time 1 "${URL}/api/latest" 2>/dev/null; then
  echo "visor: already running at ${URL}"
  bash "$(dirname "$0")/ensure-chart.sh"
  exit 0
fi

# 2. Ensure Node deps are installed the first time.
if [ ! -d "${VISOR_DIR}/node_modules" ]; then
  echo "visor: installing dependencies..."
  (cd "${VISOR_DIR}" && npm install --silent)
fi

# 3. Launch detached so it survives the agent's shell subprocess.
echo "visor: starting on port ${PORT}..."
(
  cd "${VISOR_DIR}"
  nohup env PORT="${PORT}" node server.mjs > "${LOG_FILE}" 2>&1 &
  echo $! > "${PID_FILE}"
)

# 4. Wait up to ~10s (40 × 250ms) for the server to accept requests.
for _ in $(seq 1 40); do
  if curl -sfo /dev/null --max-time 1 "${URL}/api/latest" 2>/dev/null; then
    echo "visor: ready at ${URL} (pid $(cat "${PID_FILE}" 2>/dev/null || echo '?'))"
    # 5. Open the browser once.
    open "${URL}" 2>/dev/null || true
    # 6. Warm the AntV chart MCP server so the first chart call is fast.
    bash "$(dirname "$0")/ensure-chart.sh"
    exit 0
  fi
  sleep 0.25
done

echo "visor: did not become ready within 10s — check ${LOG_FILE}" >&2
exit 1
