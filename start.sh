#!/usr/bin/env bash
# start.sh — unified launcher for the nova-sonic-agentic-co-presenter stack.
#
# Brings up six processes in dependency order:
#   1. Python FastAPI backend on 127.0.0.1:8000
#   2. Node WebSocket server on 127.0.0.1:3000
#   3. Visor SSE server on 127.0.0.1:3333
#   4. AntV chart MCP on 127.0.0.1:1122
#   5. Google Chrome with CDP on 127.0.0.1:9222 (voice-UI + visor tabs)
#   6. Microsoft PowerPoint (optional — if a .pptx path is provided)
#
# Each service is polled for readiness before moving on so the Node
# server doesn't race the Python backend etc. Safe to re-run: services
# 3 and 4 are already idempotent, the others detect "already running"
# via PID files in logs/.
#
# Usage:
#   ./start.sh [path/to/deck.pptx]
#
# Environment variables (all optional, all have sensible defaults):
#   AWS_REGION                      default us-east-1
#   AWS_PROFILE                     default unset. If set, this script will
#                                   (a) unset any static AWS_ACCESS_KEY_ID /
#                                   AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
#                                   env vars before spawning uvicorn so the
#                                   profile (which may use credential_process
#                                   for auto-refresh) is the effective source,
#                                   and (b) run a credential freshness probe
#                                   via scripts/refresh-credentials.sh.
#   NOVA_VOICE_A                     default tiffany
#   NOVA_VOICE_B                     default carlos
#   CHROME_CDP_PORT                 default 9222
#   PORT / VISOR_PORT / CHART_PORT  default 3000 / 3333 / 1122

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${ROOT_DIR}"
# 2026-05-19 — LOG_DIR was previously implicit ("logs/" relative to
# cwd). Phase 7's mute helper references ``${LOG_DIR}/mute_helper.*``
# explicitly, mirroring stop.sh's pattern. Without this binding the
# expansion was empty and the helper tried to write to "/mute_helper.log"
# (filesystem root, permission denied), so the process never started.
LOG_DIR="${ROOT_DIR}/logs"

REGION="${AWS_REGION:-us-east-1}"
PYTHON_PORT="${PYTHON_PORT:-8000}"
NODE_PORT="${PORT:-3000}"
VISOR_PORT="${VISOR_PORT:-3333}"
CHART_PORT="${CHART_PORT:-1122}"
CHROME_CDP_PORT="${CHROME_CDP_PORT:-9222}"

mkdir -p logs

# ------------------------------------------------------------------ #
# 0. Load .env (FINALYSIS_API_KEY, voice overrides, model IDs).
# ------------------------------------------------------------------ #
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT_DIR}/.env"
  set +a
fi

# ------------------------------------------------------------------ #
# 0a. AWS credentials pre-flight.
#
# Two problems we are solving at once:
#
#   1. Credential AUTO-REFRESH. Long-running Python backend + Node WS
#      server cache credentials at startup. If those came from static
#      env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
#      AWS_SESSION_TOKEN), they NEVER refresh and the stack dies with
#      ExpiredTokenException ~1 hour into a demo. If they came from a
#      profile whose credential_process is a refresher (e.g. ada), the
#      SDKs re-run that command automatically before expiration.
#
#   2. PRECEDENCE. boto3 and AWS SDK v3 both check env vars FIRST,
#      then profile. So even if the user sets AWS_PROFILE=palacan10
#      with a healthy credential_process, stale AWS_* env vars (e.g.
#      injected by Kiro / Amazon Q CLI) silently win. The fix: when
#      AWS_PROFILE is set, we unset the three static env vars so the
#      profile is the effective source. If AWS_PROFILE is not set,
#      nothing changes — we keep the env-var path untouched.
#
# scripts/refresh-credentials.sh handles the freshness probe + optional
# `ada credentials update --once` refresh. It exits non-zero only when
# the user has to intervene (e.g. run `mwinit`). We promote that into
# a hard failure so start.sh does not silently spawn services that are
# going to fail on first invocation.
# ------------------------------------------------------------------ #
if [ -n "${AWS_PROFILE:-}" ]; then
  echo "[0a/6] AWS credentials pre-flight (profile=${AWS_PROFILE})..."
  # refresh-credentials.sh handles its own messaging / colouring.
  if ! bash "${ROOT_DIR}/scripts/refresh-credentials.sh"; then
    echo "start.sh: ERROR — AWS credentials not valid for profile '${AWS_PROFILE}'." >&2
    echo "start.sh: See guidance above; fix, then re-run ./start.sh." >&2
    exit 1
  fi
  # Ensure the profile — not stale env-var creds — is what the
  # subprocess credential chain uses. `unset` here is safe because
  # anything we care about (AWS_REGION, AWS_PROFILE) stays exported.
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  export AWS_PROFILE
fi

# ------------------------------------------------------------------ #
# 0b. macOS expat workaround (portable across arm64 / x86_64 brews).
#
# Homebrew's python@3.12 is linked against a newer libexpat than the
# system's /usr/lib/libexpat.1.dylib, so xml.parsers.expat (and plistlib,
# xmlrpc …) fail with "Symbol not found: _XML_SetAllocTrackerActivation…".
# Prepend Homebrew's expat/lib to DYLD_LIBRARY_PATH here so every Python
# subprocess inherits it. scripts/install.sh also bakes this into the
# venv's activate script; this is a belt-and-suspenders copy for callers
# that run start.sh without sourcing activate.
# ------------------------------------------------------------------ #
if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  _expat_prefix="$(brew --prefix expat 2>/dev/null || true)"
  if [ -n "${_expat_prefix}" ] && [ -d "${_expat_prefix}/lib" ]; then
    case ":${DYLD_LIBRARY_PATH:-}:" in
      *":${_expat_prefix}/lib:"*) : ;;
      *) export DYLD_LIBRARY_PATH="${_expat_prefix}/lib${DYLD_LIBRARY_PATH:+:${DYLD_LIBRARY_PATH}}" ;;
    esac
  fi
  unset _expat_prefix
fi

# Helper: wait for an HTTP endpoint to respond. $1=URL $2=timeout_s $3=label.
wait_for_http() {
  local url="$1" timeout="$2" label="${3:-$1}"
  local tries=$(( timeout * 4 ))
  for _ in $(seq 1 "$tries"); do
    if curl -sfo /dev/null --max-time 1 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  echo "start.sh: ERROR — ${label} did not become ready on ${url} within ${timeout}s" >&2
  return 1
}

# Helper: is something listening on a given TCP port?
port_busy() {
  local port="$1"
  lsof -iTCP:"$port" -sTCP:LISTEN -P -n >/dev/null 2>&1
}

# Helper: PID of the process listening on a given TCP port (empty if none).
# The `|| true` handles lsof exiting 1 when there are no matches — that
# non-zero exit would otherwise abort the script under `set -euo pipefail`.
pid_on_port() {
  local port="$1"
  (lsof -iTCP:"$port" -sTCP:LISTEN -P -n -t 2>/dev/null | head -1) || true
}

# Helper: verify a pidfile points to the SAME process that is actually
# listening on the given port. Returns 0 if matched, 1 otherwise.
#
# This defends against the "zombie masks a crash" class of bug: when a
# managed service (python, node) crashes at startup and an older
# instance is still bound to the port, the pidfile would say the new
# process is alive while the port actually belongs to the old one.
pidfile_matches_port() {
  local pidfile="$1" port="$2"
  [ -f "${pidfile}" ] || return 1
  local want actual
  want="$(cat "${pidfile}" 2>/dev/null || true)"
  [ -n "${want}" ] || return 1
  kill -0 "${want}" 2>/dev/null || return 1
  actual="$(pid_on_port "${port}")"
  [ -n "${actual}" ] || return 1
  [ "${want}" = "${actual}" ]
}

# Helper: if any non-managed process is bound to a port we need, kill
# it. Non-managed = not the one listed in our pidfile.
reclaim_port() {
  local pidfile="$1" port="$2" label="$3"
  local listener; listener="$(pid_on_port "${port}")"
  [ -n "${listener}" ] || return 0            # port already free
  local ours=""; [ -f "${pidfile}" ] && ours="$(cat "${pidfile}" 2>/dev/null || true)"
  if [ "${listener}" = "${ours}" ]; then
    return 0                                   # our process, keep it
  fi
  echo "      reclaiming port ${port} from orphan pid ${listener} (${label})"
  kill "${listener}" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6 7 8; do
    sleep 0.25
    port_busy "${port}" || return 0
  done
  kill -9 "${listener}" 2>/dev/null || true
  sleep 0.25
}

# ------------------------------------------------------------------ #
# 1. Python FastAPI backend
# ------------------------------------------------------------------ #
echo "[1/6] Python FastAPI backend on 127.0.0.1:${PYTHON_PORT}..."

if pidfile_matches_port logs/python.pid "${PYTHON_PORT}"; then
  echo "      already running (pid $(cat logs/python.pid))"
else
  reclaim_port logs/python.pid "${PYTHON_PORT}" "python"
  # Pick the venv's python if present; else fall back to system python3.
  if [ -x .venv/bin/python ]; then
    PY="$(pwd)/.venv/bin/python"
  else
    PY="python3"
  fi
  AWS_REGION="${REGION}" nohup "${PY}" -m uvicorn src.api_server:app \
    --host 127.0.0.1 --port "${PYTHON_PORT}" \
    > logs/python.log 2>&1 &
  echo $! > logs/python.pid
  wait_for_http "http://127.0.0.1:${PYTHON_PORT}/compat" 15 "Python backend"
  # Final sanity check: the process we started must own the port.
  if ! pidfile_matches_port logs/python.pid "${PYTHON_PORT}"; then
    echo "start.sh: ERROR — Python started but pid $(cat logs/python.pid) does not own :${PYTHON_PORT}. Check logs/python.log." >&2
    exit 1
  fi
  echo "      ready (pid $(cat logs/python.pid))"
fi

# ------------------------------------------------------------------ #
# 1b. Bedrock pre-flight (surfaces AWS auth failures early)
# ------------------------------------------------------------------ #
echo "[1b/6] Bedrock pre-flight (/diagnose → bedrock.checks.*.ok)..."
# /diagnose exposes bedrock as { region, checks: { nova_lite: {ok, ...},
# haiku: {ok, ...}, sonnet: {ok, ...} } }. We consider the stack green
# iff every configured model check is ok. The previous implementation
# looked for a top-level `bedrock.ok` that never existed, so the
# warning fired on a perfectly healthy system — see internal
# postmortem 2026-05-08 § N4.
bedrock_report="$(
  curl -sf --max-time 15 "http://127.0.0.1:${PYTHON_PORT}/diagnose" 2>/dev/null \
    | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    checks = (d.get("bedrock") or {}).get("checks") or {}
    if not checks:
        print("no|no bedrock.checks present")
    else:
        bad = [k for k, v in checks.items() if not (isinstance(v, dict) and v.get("ok"))]
        if bad:
            print("no|failed: " + ", ".join(bad))
        else:
            print("yes|" + ", ".join(sorted(checks.keys())))
except Exception as exc:
    print("no|" + str(exc))
' || echo "no|/diagnose unreachable"
)"
bedrock_ok="${bedrock_report%%|*}"
bedrock_detail="${bedrock_report#*|}"
if [ "${bedrock_ok}" = "yes" ]; then
  echo "      Bedrock ok (${bedrock_detail})"
else
  echo "      WARN — Bedrock pre-flight not green (${bedrock_detail})."
  echo "             Check AWS creds + model access. The stack will still start;"
  echo "             failures will surface on first voice call."
fi

# ------------------------------------------------------------------ #
# 2. Node WebSocket server
# ------------------------------------------------------------------ #
echo "[2/6] Node WebSocket server on 127.0.0.1:${NODE_PORT}..."

if pidfile_matches_port logs/node.pid "${NODE_PORT}"; then
  echo "      already running (pid $(cat logs/node.pid))"
else
  reclaim_port logs/node.pid "${NODE_PORT}" "node"
  if [ ! -d websocket-server/node_modules ]; then
    echo "      installing Node deps..."
    (cd websocket-server && npm install --silent --no-audit --no-fund)
  fi
  nohup node websocket-server/server.js \
    --port "${NODE_PORT}" \
    --python-url "http://127.0.0.1:${PYTHON_PORT}" \
    > logs/node.log 2>&1 &
  echo $! > logs/node.pid
  wait_for_http "http://127.0.0.1:${NODE_PORT}/healthz" 10 "Node WS server"
  # Final sanity check: the process we started must own the port. This
  # catches the case where server.js crashes at startup and an older
  # Node instance happens to be answering :${NODE_PORT}.
  if ! pidfile_matches_port logs/node.pid "${NODE_PORT}"; then
    echo "start.sh: ERROR — Node started but pid $(cat logs/node.pid) does not own :${NODE_PORT}. Check logs/node.log." >&2
    exit 1
  fi
  echo "      ready (pid $(cat logs/node.pid))"
fi

# ------------------------------------------------------------------ #
# 3. Visor (idempotent via ensure-visor.sh)
# ------------------------------------------------------------------ #
echo "[3/6] Visor on 127.0.0.1:${VISOR_PORT}..."
VISOR_PORT="${VISOR_PORT}" bash "${ROOT_DIR}/scripts/ensure-visor.sh"

# ------------------------------------------------------------------ #
# 4. AntV chart MCP (idempotent via ensure-chart.sh)
# ------------------------------------------------------------------ #
echo "[4/6] AntV chart MCP on 127.0.0.1:${CHART_PORT}..."
CHART_PORT="${CHART_PORT}" bash "${ROOT_DIR}/scripts/ensure-chart.sh"

# ------------------------------------------------------------------ #
# 5. Chrome with CDP (idempotent via ensure-chrome.sh)
# ------------------------------------------------------------------ #
echo "[5/6] Chrome with CDP on 127.0.0.1:${CHROME_CDP_PORT} + tabs..."
# Both tabs in the same CDP-isolated Chrome window. The visor tab is
# the one that goes fullscreen on Space 3 during
# demo-setup-fullscreen.sh; the voice UI tab rides along in the same
# window. Access voice UI by moving focus to the tab — during the demo,
# you'll normally leave the visor tab active. NOTE: do NOT split into
# separate windows via AppleScript `make new window` — that targets the
# frontmost Chrome process, which may be the user's personal Chrome,
# not the CDP instance.
CHROME_CDP_PORT="${CHROME_CDP_PORT}" bash "${ROOT_DIR}/scripts/ensure-chrome.sh" \
  "http://localhost:${NODE_PORT}" \
  "http://localhost:${VISOR_PORT}"

# ------------------------------------------------------------------ #
# 6. PowerPoint (optional — only when a .pptx path was passed)
# ------------------------------------------------------------------ #
if [ $# -ge 1 ]; then
  if [ -f "$1" ]; then
    echo "[6/6] Opening PowerPoint with $1..."
    open -a "Microsoft PowerPoint" "$1"
  else
    echo "[6/6] WARN — .pptx file not found: $1 (skipping)"
  fi
else
  echo "[6/6] (no .pptx path argument — skipping PowerPoint)"
fi

# ------------------------------------------------------------------ #
# 7. Mute helper (macOS-only) — global spacebar hotkey + cross-Space
#    floating "🎤 Live" / "🔇 Muted" indicator. Lives in its own process
#    so a CGEventTap or NSWindow failure can't take down the rest of
#    the stack. Idempotent: refuses to start a second instance if its
#    pidfile already points at a live process.
# ------------------------------------------------------------------ #
if [ "$(uname -s)" = "Darwin" ]; then
  echo "[7/7] Mute helper (spacebar hotkey + cross-Space indicator)..."
  MUTE_PID_FILE="${LOG_DIR}/mute_helper.pid"
  if [ -f "${MUTE_PID_FILE}" ] && kill -0 "$(cat "${MUTE_PID_FILE}")" 2>/dev/null; then
    echo "      already running (pid $(cat "${MUTE_PID_FILE}"))"
  else
    if [ -x .venv/bin/python ]; then
      MUTE_PY="$(pwd)/.venv/bin/python"
    else
      MUTE_PY="python3"
    fi
    NOVA_NODE_BASE_URL="http://127.0.0.1:${NODE_PORT}" \
      nohup "${MUTE_PY}" -m src.platform.mute_helper \
      > "${LOG_DIR}/mute_helper.log" 2>&1 &
    echo $! > "${MUTE_PID_FILE}"
    # No HTTP probe — the helper has no server surface, just a Cocoa
    # event loop. Verify the process is alive after a brief settle.
    sleep 0.4
    if kill -0 "$(cat "${MUTE_PID_FILE}")" 2>/dev/null; then
      echo "      ready (pid $(cat "${MUTE_PID_FILE}"))"
      echo "      tip: spacebar toggles Nova mute everywhere — including"
      echo "           during PowerPoint slideshow. Use → / N / Page Down"
      echo "           to advance slides while a session is active."
    else
      echo "      WARN — mute helper exited immediately. See ${LOG_DIR}/mute_helper.log"
      echo "             (typical cause: Accessibility permission missing"
      echo "              for your terminal — System Settings → Privacy &"
      echo "              Security → Accessibility)."
      rm -f "${MUTE_PID_FILE}"
    fi
  fi
else
  echo "[7/7] Mute helper: skipped (macOS-only feature, uname=$(uname -s))"
fi

# ------------------------------------------------------------------ #
# Summary
# ------------------------------------------------------------------ #
VOICE_A="${NOVA_VOICE_A:-tiffany}"
VOICE_B="${NOVA_VOICE_B:-carlos}"

cat <<EOF

──────────────────────────────────────────────────────────
  nova-sonic-agentic-co-presenter is up.

    Voice UI    http://localhost:${NODE_PORT}
    Visor       http://localhost:${VISOR_PORT}
    Diagnose    curl http://localhost:${PYTHON_PORT}/diagnose | jq

    Session A voice: ${VOICE_A}
    Session B voice: ${VOICE_B}

  Logs: logs/python.log   logs/node.log
  Stop: ./stop.sh
──────────────────────────────────────────────────────────
EOF
