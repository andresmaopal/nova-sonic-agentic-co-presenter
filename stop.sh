#!/usr/bin/env bash
# stop.sh — graceful teardown for nova-sonic-agentic-co-presenter.
#
# Shutdown order:
#   1. Ask the Node server to close every active Nova Sonic stream
#      cleanly (best-effort — no hard error if the endpoint isn't
#      reachable; Node may already be dead).
#   2. Send SIGTERM to Python + Node + visor + AntV chart MCP via the
#      PID files written by start.sh and the ensure-*.sh helpers.
#   3. Follow up with SIGKILL if anything is still up 300 ms later.
#
# Chrome is intentionally left running — the user may want to keep
# their tabs. Close it manually if desired, or set CHROME_STOP=1 to
# kill the CDP Chrome too.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
TMP="${TMPDIR:-/tmp}"

NODE_PORT="${PORT:-3000}"

# Best-effort HTTP call — 2 s timeout, all errors swallowed.
http_best_effort() {
  local url="$1" method="${2:-POST}"
  curl -sfo /dev/null --max-time 2 -X "$method" "$url" 2>/dev/null || true
}

# Stop a process named $1 via its PID file at $2.
stop_by_pidfile() {
  local name="$1" pidfile="$2"
  if [ ! -f "${pidfile}" ]; then
    return 0
  fi
  local pid
  pid="$(cat "${pidfile}" 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    rm -f "${pidfile}"
    return 0
  fi
  if kill -0 "${pid}" 2>/dev/null; then
    echo "stopping ${name} (pid ${pid})..."
    kill "${pid}" 2>/dev/null || true
    sleep 0.3
    if kill -0 "${pid}" 2>/dev/null; then
      echo "  ${name} still alive — SIGKILL"
      kill -9 "${pid}" 2>/dev/null || true
    fi
  fi
  rm -f "${pidfile}"
}

# ------------------------------------------------------------------ #
# 1. Tell the Node server to gracefully close Bedrock streams first.
#    The session manager closes both Session A and Session B cleanly
#    so Bedrock doesn't log spurious "stream disconnected" errors.
# ------------------------------------------------------------------ #
echo "stop.sh: signalling Node for graceful Bedrock shutdown..."
# The /healthz endpoint exists; the session manager cleanup runs on
# ws.close(), so killing Node with SIGTERM (next step) is what actually
# closes the streams. We still probe the health endpoint to give the
# user a visible signal that Node was alive.
http_best_effort "http://127.0.0.1:${NODE_PORT}/healthz" "GET"

# ------------------------------------------------------------------ #
# 2. Kill the four managed processes.
# ------------------------------------------------------------------ #
stop_by_pidfile "python"  "${LOG_DIR}/python.pid"
stop_by_pidfile "node"    "${LOG_DIR}/node.pid"
stop_by_pidfile "visor"   "${TMP}/finalysis-visor.pid"
stop_by_pidfile "chart"   "${TMP}/antv-chart-mcp.pid"
# Mute helper (macOS-only). Owns no port and no Bedrock stream, so
# it's safe to kill last — even if it's already dead, stop_by_pidfile
# is a no-op when the pidfile is missing or stale.
stop_by_pidfile "mute-helper" "${LOG_DIR}/mute_helper.pid"

# ------------------------------------------------------------------ #
# 2b. Zombie sweep: if any process is still listening on a managed
#     port (because a pidfile was stale or empty), kill it. This is the
#     belt-and-suspenders fix for the "zombie masked a crashed node"
#     bug hit during development.
# ------------------------------------------------------------------ #
PYTHON_PORT="${PYTHON_PORT:-8000}"
VISOR_PORT="${VISOR_PORT:-3333}"
CHART_PORT="${CHART_PORT:-1122}"
for spec in "python:${PYTHON_PORT}" "node:${NODE_PORT}" "visor:${VISOR_PORT}" "chart:${CHART_PORT}"; do
  label="${spec%:*}"; port="${spec#*:}"
  zombie_pid="$(lsof -iTCP:"${port}" -sTCP:LISTEN -P -n -t 2>/dev/null | head -1 || true)"
  if [ -n "${zombie_pid}" ]; then
    echo "stop.sh: zombie ${label} (pid ${zombie_pid}) still listening on :${port} — killing"
    kill "${zombie_pid}" 2>/dev/null || true
    sleep 0.3
    kill -0 "${zombie_pid}" 2>/dev/null && kill -9 "${zombie_pid}" 2>/dev/null || true
  fi
done

# ------------------------------------------------------------------ #
# 3. Chrome — only if the user explicitly asks for it.
# ------------------------------------------------------------------ #
if [ "${CHROME_STOP:-0}" = "1" ]; then
  stop_by_pidfile "chrome-cdp" "${TMP}/nova-chrome-cdp.pid"
  echo "stopped Chrome CDP instance (set CHROME_STOP=0 to leave it running)"
else
  echo "Chrome left running. Set CHROME_STOP=1 to kill the CDP instance too."
fi

echo "stop.sh: done."
