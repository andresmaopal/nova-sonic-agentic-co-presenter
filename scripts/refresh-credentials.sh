#!/usr/bin/env bash
# refresh-credentials.sh — ensure AWS creds for the demo profile are fresh.
#
# The demo's long-running services (Python backend, Node WS server) cache
# their boto3 / AWS-SDK-v3 credentials at startup. If those creds were
# picked from static environment variables, they NEVER refresh and the
# stack dies with `ExpiredTokenException` after ~1 hour.
#
# The safe pattern is: use a *refreshable* profile (AWS_PROFILE=palacan10
# backed by `credential_process = ada credentials print …`). This script
# makes sure that profile is currently valid and, if it isn't, calls
# `ada credentials update --once` to renew it before we hand the baton to
# start.sh.
#
# Safe to run repeatedly. Called automatically by demo-go-live.sh (step 0)
# and start.sh (pre-flight), but also usable standalone:
#
#   ./scripts/refresh-credentials.sh
#   AWS_PROFILE=palacan10 ./scripts/refresh-credentials.sh
#
# Exit codes:
#   0 — profile is valid (either already or after a successful refresh)
#   1 — profile config problem (missing ada, missing profile, bad .env)
#   2 — refresh attempted but still failing (likely mwinit cookies expired)

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── colour helpers (match demo-go-live.sh) ────────────────────────────
if [ -t 1 ]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m'; CYAN=$'\033[0;36m'; NC=$'\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; NC=''
fi
step() { printf '%s[creds]%s %s\n' "$BLUE" "$NC" "$1"; }
ok()   { printf '%s[  ok ]%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '%s[ warn]%s %s\n' "$YELLOW" "$NC" "$1"; }
fail() { printf '%s[fail ]%s %s\n' "$RED" "$NC" "$1" >&2; }

# ─── pick AWS_PROFILE from (in order) arg, env, .env file ──────────────
if [ $# -ge 1 ] && [ -n "${1:-}" ]; then
  AWS_PROFILE="$1"
fi
if [ -z "${AWS_PROFILE:-}" ] && [ -f "${ROOT_DIR}/.env" ]; then
  # Pull AWS_PROFILE=... out of .env without sourcing the whole file
  # (sourcing would pollute our env with API keys etc).
  _env_profile="$(grep -E '^[[:space:]]*AWS_PROFILE[[:space:]]*=' "${ROOT_DIR}/.env" \
                    | tail -1 | sed -E 's/^[[:space:]]*AWS_PROFILE[[:space:]]*=[[:space:]]*"?([^"#]*)"?.*$/\1/' \
                    | tr -d '[:space:]')"
  if [ -n "${_env_profile:-}" ]; then
    AWS_PROFILE="${_env_profile}"
  fi
fi

if [ -z "${AWS_PROFILE:-}" ]; then
  step "no AWS_PROFILE set — skipping profile refresh (using env / default chain)"
  exit 0
fi
export AWS_PROFILE

# When refreshing a profile we must not let static env-var creds shadow
# the profile. This affects this shell only — demo-go-live.sh also
# unsets these in the subprocess env for start.sh.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

step "target profile: ${CYAN}${AWS_PROFILE}${NC}"

# ─── probe current freshness ───────────────────────────────────────────
probe() {
  aws sts get-caller-identity --profile "$AWS_PROFILE" --no-cli-pager \
    --output json 2>&1
}

attempt_probe="$(probe)"
if printf '%s' "$attempt_probe" | grep -q '"Arn"'; then
  arn="$(printf '%s' "$attempt_probe" | sed -nE 's/.*"Arn":[[:space:]]*"([^"]*)".*/\1/p')"
  ok "credentials valid (${arn})"
  exit 0
fi

# ─── credentials expired / broken → try a refresh ─────────────────────
step "credentials not valid — attempting refresh"
if ! command -v ada >/dev/null 2>&1; then
  fail "ada CLI not found in PATH. Install via 'toolbox install ada' and retry."
  fail "Original probe error:"
  printf '%s\n' "$attempt_probe" | sed 's/^/    /' >&2
  exit 1
fi

# Extract account+role from the credential_process line so we refresh
# exactly the identity the profile expects. This keeps the script honest
# when multiple profiles share ada.
cfg_line="$(aws configure get credential_process --profile "$AWS_PROFILE" 2>/dev/null || true)"
account="$(printf '%s' "$cfg_line" | sed -nE 's/.*--account[[:space:]]+([^[:space:]]+).*/\1/p')"
role="$(printf '%s'    "$cfg_line" | sed -nE 's/.*--role[[:space:]]+([^[:space:]]+).*/\1/p')"
provider="$(printf '%s' "$cfg_line" | sed -nE 's/.*--provider[[:space:]]+([^[:space:]]+).*/\1/p')"
provider="${provider:-isengard}"

if [ -z "${account:-}" ] || [ -z "${role:-}" ]; then
  fail "Could not parse --account / --role from credential_process for profile '${AWS_PROFILE}'."
  fail "credential_process = ${cfg_line:-<empty>}"
  fail "Fix ~/.aws/config so the line contains '--account <id> --role <name>' and try again."
  exit 1
fi

step "ada credentials update --once --profile ${AWS_PROFILE} --account ${account} --role ${role} --provider ${provider}"
if ! ada credentials update --once \
        --profile "$AWS_PROFILE" \
        --account "$account" \
        --role "$role" \
        --provider "$provider" 2>&1 | sed 's/^/    /'; then
  fail "'ada credentials update --once' failed."
  fail "If Midway cookies are stale, run:  mwinit -f"
  fail "then re-run this script."
  exit 2
fi

# Re-probe after refresh.
attempt_probe="$(probe)"
if printf '%s' "$attempt_probe" | grep -q '"Arn"'; then
  arn="$(printf '%s' "$attempt_probe" | sed -nE 's/.*"Arn":[[:space:]]*"([^"]*)".*/\1/p')"
  ok "credentials refreshed (${arn})"
  exit 0
fi

fail "credentials still invalid after refresh."
fail "Last sts error:"
printf '%s\n' "$attempt_probe" | sed 's/^/    /' >&2
fail ""
fail "Manual recovery:"
fail "  1. mwinit -f                           # refresh Midway cookies (your preferred flow)"
fail "  2. ada credentials update --once --profile ${AWS_PROFILE} \\"
fail "         --account ${account} --role ${role} --provider ${provider}"
fail "  3. aws sts get-caller-identity --profile ${AWS_PROFILE}"
exit 2
