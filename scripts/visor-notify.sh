#!/usr/bin/env bash
# visor-notify.sh — push real-time progress events to the Finalysis visor.
#
# The visor's overlay used to be a cosmetic 8s timer that only ran AFTER
# the HTML report was written. This helper lets the agent drive the
# overlay during the real work: it appears when generation starts,
# advances as each phase completes, and dismisses as soon as the report
# is written (chokidar handles the final swap).
#
# Usage:
#   visor-notify.sh start [PHASES_JSON]
#     PHASES_JSON is an array of strings or objects {label, substeps?}.
#     Omit it to let the visor use its default phase list.
#     Examples:
#       visor-notify.sh start
#       visor-notify.sh start '["Fetch","Transform","Chart","Summary","Write"]'
#       visor-notify.sh start '[{"label":"Fetch","substeps":["TSLA","NVDA"]}]'
#
#   visor-notify.sh phase INDEX [SUBSTEP]
#     Mark phase INDEX (0-based) as active. SUBSTEP is free-form text
#     shown under the active phase; typically a short sentence like
#     "Ticker=TSLA window=50" or "Chart URL validated".
#
#   visor-notify.sh done
#     Snap all phases to complete. Optional — writing the HTML file will
#     also collapse the overlay via the file watcher.
#
# All calls are best-effort: the script exits 0 even when the visor is
# unreachable so the agent's main work is never blocked by visor issues.
# Set VISOR_DEBUG=1 to surface curl errors on stderr.

set -uo pipefail

PORT="${VISOR_PORT:-3333}"
URL="http://localhost:${PORT}"

# Best-effort POST — swallows network/HTTP errors unless VISOR_DEBUG=1.
post() {
  local path="$1" body="$2"
  if [ "${VISOR_DEBUG:-0}" = "1" ]; then
    curl -sS --max-time 2 -X POST "${URL}${path}" \
      -H 'Content-Type: application/json' -d "$body" >/dev/null
  else
    curl -s --max-time 2 -o /dev/null -X POST "${URL}${path}" \
      -H 'Content-Type: application/json' -d "$body" 2>/dev/null || true
  fi
}

# Escape an arbitrary string as a JSON-safe string literal (including quotes).
# Uses Python for correctness with Unicode / control chars; falls back to a
# conservative sed-based escape if Python isn't available.
json_str() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1"
  else
    # Basic fallback: escape backslash and double-quote, wrap in quotes.
    # Good enough for short substep strings from the agent.
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  fi
}

cmd="${1:-}"
case "$cmd" in
  start)
    phases_json="${2:-}"
    if [ -n "$phases_json" ]; then
      body="{\"phases\":${phases_json}}"
    else
      body="{}"
    fi
    post "/api/start" "$body"
    ;;

  phase)
    idx="${2:-}"
    if [ -z "$idx" ] || ! [[ "$idx" =~ ^[0-9]+$ ]]; then
      echo "visor-notify: phase requires a numeric INDEX (0-based)" >&2
      exit 2
    fi
    sub="${3:-}"
    if [ -n "$sub" ]; then
      body="{\"index\":${idx},\"substep\":$(json_str "$sub")}"
    else
      body="{\"index\":${idx}}"
    fi
    post "/api/phase" "$body"
    ;;

  done)
    post "/api/done" "{}"
    ;;

  ""|-h|--help|help)
    sed -n '2,36p' "$0"
    [ -z "$cmd" ] && exit 2 || exit 0
    ;;

  *)
    echo "visor-notify: unknown command '$cmd' (expected: start | phase | done)" >&2
    exit 2
    ;;
esac
