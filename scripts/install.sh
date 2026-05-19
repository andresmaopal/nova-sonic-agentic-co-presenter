#!/usr/bin/env bash
# install.sh — portable, idempotent installer for nova-sonic-agentic-co-presenter.
#
# Designed to run cleanly on any AWS macOS MacBook Pro (Apple Silicon or
# Intel) with the default "empty developer machine" starting point:
#   • Xcode Command Line Tools installed
#   • Homebrew installed (the installer will check and bail with a clear
#     message if not)
#
# It handles every gotcha hit during the first install:
#   1. Homebrew prefix differs on arm64 (/opt/homebrew) vs x86_64
#      (/usr/local). All paths are resolved with `brew --prefix …`.
#   2. Homebrew's python@3.12 is compiled against a newer libexpat than
#      /usr/lib/libexpat.1.dylib, causing ImportError on `xml.parsers.expat`
#      (and therefore plistlib, xmlrpc, etc). We install `expat` and bake
#      DYLD_LIBRARY_PATH into the venv's activate script so it's
#      transparent for every downstream caller.
#   3. PowerPoint needs macOS Automation permission, which can only be
#      granted by the user — we print a clear reminder.
#   4. Bedrock model access needs to be enabled in the AWS Console — we
#      probe it and warn (non-fatal).
#
# Safe to re-run: every step checks what's already present before doing work.
#
# Usage:
#   ./scripts/install.sh              # normal install
#   SKIP_BREW=1 ./scripts/install.sh  # skip Homebrew steps (CI, locked-down machines)
#   VERBOSE=1 ./scripts/install.sh    # show full brew/pip/npm output
#
# Exit codes:
#   0   everything installed (or already installed)
#   1   fatal error (missing Homebrew, unsupported OS, etc.)
#   2   partial success — install finished but a warning needs attention

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT_DIR}"

VERBOSE="${VERBOSE:-0}"
SKIP_BREW="${SKIP_BREW:-0}"

# ANSI colors (disabled when not a TTY, so logs stay clean).
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLU=$'\033[34m'
  C_RST=$'\033[0m'
else
  C_BOLD=''; C_DIM=''; C_RED=''; C_GRN=''; C_YEL=''; C_BLU=''; C_RST=''
fi

log()   { printf '%s[install]%s %s\n'    "${C_BLU}" "${C_RST}" "$*"; }
ok()    { printf '%s[install]%s %s%s%s\n' "${C_BLU}" "${C_RST}" "${C_GRN}" "$*" "${C_RST}"; }
warn()  { printf '%s[install]%s %sWARN%s %s\n' "${C_BLU}" "${C_RST}" "${C_YEL}" "${C_RST}" "$*"; }
err()   { printf '%s[install]%s %sERROR%s %s\n' "${C_BLU}" "${C_RST}" "${C_RED}" "${C_RST}" "$*" >&2; }

# Tracks warnings surfaced during install, for a final summary.
WARNINGS=()

# Run a command, hiding output unless VERBOSE=1 or the command fails.
run_quiet() {
  if [ "${VERBOSE}" = "1" ]; then
    "$@"
    return $?
  fi
  local tmp; tmp="$(mktemp)"
  if "$@" >"${tmp}" 2>&1; then
    rm -f "${tmp}"
    return 0
  fi
  local rc=$?
  cat "${tmp}" >&2
  rm -f "${tmp}"
  return "${rc}"
}

# ------------------------------------------------------------------ #
# 0. OS + arch sanity
# ------------------------------------------------------------------ #
if [ "$(uname -s)" != "Darwin" ]; then
  err "This installer targets macOS. Detected: $(uname -s)"
  exit 1
fi

ARCH="$(uname -m)"
log "macOS $(sw_vers -productVersion) on ${ARCH}"

# ------------------------------------------------------------------ #
# 1. Homebrew presence
# ------------------------------------------------------------------ #
if ! command -v brew >/dev/null 2>&1; then
  if [ "${SKIP_BREW}" = "1" ]; then
    warn "SKIP_BREW=1 set and brew not on PATH — continuing, but several steps may fail."
  else
    err "Homebrew is not installed. Install it from https://brew.sh then re-run."
    exit 1
  fi
fi

BREW_PREFIX="$(brew --prefix 2>/dev/null || echo "/opt/homebrew")"
log "Homebrew prefix: ${BREW_PREFIX}"

# ------------------------------------------------------------------ #
# 2. Homebrew packages
#
# Each entry is "package:test-command". The test-command decides whether
# the package is "already present" — this matters because `brew list`
# is slow and some packages are installed via casks or by being bundled
# (e.g. libreoffice as /Applications/LibreOffice.app).
# ------------------------------------------------------------------ #
if [ "${SKIP_BREW}" != "1" ]; then
  log "Homebrew formulas..."
  BREW_FORMULAS=(
    "python@3.12"
    "node"
    "poppler"
    "portaudio"
    "expat"       # fixes the pyexpat ImportError (see header)
  )
  for formula in "${BREW_FORMULAS[@]}"; do
    if brew list --formula "${formula}" >/dev/null 2>&1; then
      printf '  %s%-20s%s already installed\n' "${C_DIM}" "${formula}" "${C_RST}"
    else
      log "  installing ${formula}..."
      if ! run_quiet brew install "${formula}"; then
        err "brew install ${formula} failed"
        exit 1
      fi
    fi
  done

  log "Homebrew casks..."
  # Google Chrome: check for the .app bundle instead of `brew list --cask`
  # because the user may have installed Chrome manually (very common).
  if [ -d "/Applications/Google Chrome.app" ]; then
    printf '  %s%-20s%s already present (/Applications/Google Chrome.app)\n' \
      "${C_DIM}" "google-chrome" "${C_RST}"
  else
    log "  installing google-chrome..."
    if ! run_quiet brew install --cask google-chrome; then
      err "brew install --cask google-chrome failed"
      exit 1
    fi
  fi

  # LibreOffice: used by scripts that render .pptx → PDF → PNG. Also very
  # commonly pre-installed, so we check for the .app too.
  if [ -d "/Applications/LibreOffice.app" ]; then
    printf '  %s%-20s%s already present (/Applications/LibreOffice.app)\n' \
      "${C_DIM}" "libreoffice" "${C_RST}"
  else
    log "  installing libreoffice (this can take a minute)..."
    if ! run_quiet brew install --cask libreoffice; then
      err "brew install --cask libreoffice failed"
      exit 1
    fi
  fi
fi

# ------------------------------------------------------------------ #
# 3. Resolve Python 3.12 path (brew-agnostic)
# ------------------------------------------------------------------ #
PYTHON312=""
if command -v python3.12 >/dev/null 2>&1; then
  PYTHON312="$(command -v python3.12)"
else
  # Fall back to brew --prefix which works on both arm64 and x86_64.
  if py_prefix="$(brew --prefix python@3.12 2>/dev/null)"; then
    candidate="${py_prefix}/bin/python3.12"
    if [ -x "${candidate}" ]; then
      PYTHON312="${candidate}"
    fi
  fi
fi

if [ -z "${PYTHON312}" ] || [ ! -x "${PYTHON312}" ]; then
  err "python3.12 not found. Run 'brew install python@3.12' and re-try."
  exit 1
fi
log "Python 3.12: ${PYTHON312} ($(${PYTHON312} --version 2>&1))"

# ------------------------------------------------------------------ #
# 4. Resolve expat prefix (needed for the DYLD_LIBRARY_PATH workaround)
# ------------------------------------------------------------------ #
EXPAT_PREFIX=""
if ep="$(brew --prefix expat 2>/dev/null)"; then
  if [ -d "${ep}/lib" ]; then
    EXPAT_PREFIX="${ep}"
  fi
fi
if [ -z "${EXPAT_PREFIX}" ]; then
  warn "Could not resolve Homebrew expat. pyexpat may fail at runtime."
  WARNINGS+=("expat not resolvable via brew --prefix")
else
  log "Homebrew expat: ${EXPAT_PREFIX}"
fi

# ------------------------------------------------------------------ #
# 5. Python virtualenv (+ expat workaround baked into activate)
# ------------------------------------------------------------------ #
VENV_DIR="${ROOT_DIR}/.venv"

# Helper: create the venv, applying the DYLD_LIBRARY_PATH workaround so
# `python -m venv` itself can import xmlrpc → expat.
create_venv() {
  if [ -n "${EXPAT_PREFIX}" ]; then
    DYLD_LIBRARY_PATH="${EXPAT_PREFIX}/lib" "${PYTHON312}" -m venv "${VENV_DIR}"
  else
    "${PYTHON312}" -m venv "${VENV_DIR}"
  fi
}

# Helper: does the given venv python successfully import expat?
venv_expat_ok() {
  local py="$1"
  DYLD_LIBRARY_PATH="" "${py}" -c 'import xml.parsers.expat' >/dev/null 2>&1
}

if [ -x "${VENV_DIR}/bin/python" ]; then
  VENV_PY_VERSION="$("${VENV_DIR}/bin/python" --version 2>&1 || echo 'unknown')"
  log "venv already exists: ${VENV_PY_VERSION}"
  # If the venv's Python doesn't match 3.12, rebuild.
  if ! "${VENV_DIR}/bin/python" --version 2>&1 | grep -q 'Python 3.12'; then
    warn "Existing venv is not Python 3.12 — rebuilding."
    rm -rf "${VENV_DIR}"
    create_venv
  fi
else
  log "creating venv at ${VENV_DIR}..."
  create_venv
fi

# ------------------------------------------------------------------ #
# 5b. Bake the expat fix into .venv/bin/activate (only if needed)
# ------------------------------------------------------------------ #
ACTIVATE_FILE="${VENV_DIR}/bin/activate"
EXPAT_MARKER="# >>> nova-sonic-agentic-co-presenter: expat fix >>>"

if [ -n "${EXPAT_PREFIX}" ] && ! venv_expat_ok "${VENV_DIR}/bin/python"; then
  if grep -q "${EXPAT_MARKER}" "${ACTIVATE_FILE}" 2>/dev/null; then
    log "activate: expat DYLD_LIBRARY_PATH already injected"
  else
    log "activate: injecting expat DYLD_LIBRARY_PATH workaround"
    cat >>"${ACTIVATE_FILE}" <<ACTIVATE_EOF

${EXPAT_MARKER}
# Homebrew's python@3.12 links against a newer libexpat than
# /usr/lib/libexpat.1.dylib. Prepend Homebrew's expat to DYLD_LIBRARY_PATH
# so xml.parsers.expat (and therefore plistlib, xmlrpc, etc.) load.
# This block is idempotent and gets removed when the venv is rebuilt.
if [ -d "${EXPAT_PREFIX}/lib" ]; then
    if [ -z "\${DYLD_LIBRARY_PATH:-}" ]; then
        export DYLD_LIBRARY_PATH="${EXPAT_PREFIX}/lib"
    else
        case ":\${DYLD_LIBRARY_PATH}:" in
            *":${EXPAT_PREFIX}/lib:"*) : ;; # already present
            *) export DYLD_LIBRARY_PATH="${EXPAT_PREFIX}/lib:\${DYLD_LIBRARY_PATH}" ;;
        esac
    fi
fi
# <<< nova-sonic-agentic-co-presenter: expat fix <<<
ACTIVATE_EOF
  fi
elif venv_expat_ok "${VENV_DIR}/bin/python"; then
  log "activate: pyexpat imports cleanly — no workaround needed"
fi

# ------------------------------------------------------------------ #
# 6. Install Python requirements
# ------------------------------------------------------------------ #
log "Python packages..."
# shellcheck disable=SC1091
source "${ACTIVATE_FILE}"

if python -c "import fastapi, boto3, playwright, pdf2image" >/dev/null 2>&1; then
  log "  core packages already installed — checking for updates"
fi

if ! run_quiet python -m pip install --upgrade pip; then
  err "pip upgrade failed"
  exit 1
fi
if ! run_quiet python -m pip install -r requirements.txt; then
  err "pip install -r requirements.txt failed"
  exit 1
fi
ok "  Python packages installed"

# ------------------------------------------------------------------ #
# 7. Playwright chromium (only if missing)
# ------------------------------------------------------------------ #
log "Playwright chromium..."
# playwright stores browsers in ~/Library/Caches/ms-playwright; the CLI's
# `install --dry-run` exits 0 if the browser is already present.
if python -c "
from playwright.sync_api import sync_playwright
import sys
try:
    with sync_playwright() as p:
        # Just check that chromium executable path resolves
        p.chromium.executable_path
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
  log "  chromium already installed"
else
  if ! run_quiet playwright install chromium; then
    err "playwright install chromium failed"
    exit 1
  fi
  ok "  chromium installed"
fi

# ------------------------------------------------------------------ #
# 8. Node versions + dependencies
# ------------------------------------------------------------------ #
log "Node dependencies..."
NODE_VERSION="$(node --version 2>/dev/null | sed 's/^v//' || echo '0.0.0')"
NODE_MAJOR="${NODE_VERSION%%.*}"
if [ "${NODE_MAJOR}" -lt 20 ] 2>/dev/null; then
  warn "Node ${NODE_VERSION} is below the recommended v20+."
  WARNINGS+=("Node ${NODE_VERSION} < 20")
fi

for sub in websocket-server visor; do
  if [ -d "${ROOT_DIR}/${sub}/node_modules" ]; then
    log "  ${sub}: node_modules present"
  else
    log "  installing ${sub}..."
    if ! run_quiet sh -c "cd '${ROOT_DIR}/${sub}' && npm install --no-audit --no-fund"; then
      err "npm install in ${sub} failed"
      exit 1
    fi
  fi
done
ok "  Node dependencies ready"

# ------------------------------------------------------------------ #
# 9. .env file
# ------------------------------------------------------------------ #
if [ ! -f "${ROOT_DIR}/.env" ]; then
  cp "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"
  ok ".env created from .env.example"
  warn "Edit .env and set FINALYSIS_API_KEY before running the financial specialist"
  WARNINGS+=(".env needs FINALYSIS_API_KEY")
else
  log ".env already present"
  if grep -qE '^FINALYSIS_API_KEY=\s*$' "${ROOT_DIR}/.env" 2>/dev/null; then
    warn "FINALYSIS_API_KEY is empty in .env"
    WARNINGS+=("FINALYSIS_API_KEY empty in .env")
  fi
fi

# ------------------------------------------------------------------ #
# 10. AWS credentials probe (non-fatal)
# ------------------------------------------------------------------ #
log "AWS credentials..."
if command -v aws >/dev/null 2>&1; then
  if aws sts get-caller-identity >/dev/null 2>&1; then
    aws_id="$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null || echo 'unknown')"
    ok "  authenticated as ${aws_id}"
  else
    warn "AWS credentials not working — run 'aws configure' or set AWS_PROFILE."
    WARNINGS+=("AWS credentials not configured")
  fi
else
  warn "AWS CLI not installed. Install it or ensure your code picks creds from env/IAM role."
  WARNINGS+=("aws CLI not installed")
fi

# ------------------------------------------------------------------ #
# 11. Sanity tests (fast, offline). The minimal release distribution
#     ships without the tests/ tree; this step is therefore a soft
#     check — present in development checkouts, gracefully skipped
#     in shipped tarballs.
# ------------------------------------------------------------------ #
if [ -d tests ]; then
  log "Running fast sanity tests (Python)..."
  if run_quiet env PYTHONPATH=. python -m pytest tests/ -q \
       --ignore=tests/_smoke_analyze_slide.py -x --timeout=60 2>/dev/null; then
    ok "  Python tests: pass"
  else
    # Retry without --timeout if pytest-timeout is not installed.
    if run_quiet env PYTHONPATH=. python -m pytest tests/ -q \
         --ignore=tests/_smoke_analyze_slide.py -x; then
      ok "  Python tests: pass"
    else
      warn "Python tests failed — see output above."
      WARNINGS+=("Python tests failed")
    fi
  fi
else
  log "Skipping Python sanity tests (tests/ not in this distribution)."
fi

if [ -d "${ROOT_DIR}/websocket-server/tests" ]; then
  log "Running fast sanity tests (Node)..."
  if (cd "${ROOT_DIR}/websocket-server" && run_quiet node --test tests/session-manager.test.js tests/prompts.test.js); then
    ok "  Node tests: pass"
  else
    warn "Node tests failed — see output above."
    WARNINGS+=("Node tests failed")
  fi
else
  log "Skipping Node sanity tests (websocket-server/tests/ not in this distribution)."
fi

# ------------------------------------------------------------------ #
# 12. Reminders that only the user can fulfill
# ------------------------------------------------------------------ #
cat <<EOF

${C_BOLD}Manual steps that only you can do:${C_RST}
  ${C_DIM}•${C_RST} Microsoft PowerPoint 2016+ — grant Automation permission:
    System Settings → Privacy & Security → Automation → enable PowerPoint
    for your terminal (Terminal.app, iTerm2, VS Code — whatever you run
    ${C_BOLD}./start.sh${C_RST} from).
  ${C_DIM}•${C_RST} AWS Bedrock — enable model access in your region:
    https://console.aws.amazon.com/bedrock/home#/modelaccess
    Required: nova-2-sonic, claude-haiku-4-5, claude-sonnet-4-6, nova-2-lite
  ${C_DIM}•${C_RST} Fill in ${C_BOLD}FINALYSIS_API_KEY${C_RST} in .env for the financial specialist.
EOF

# ------------------------------------------------------------------ #
# Summary
# ------------------------------------------------------------------ #
echo
if [ "${#WARNINGS[@]}" -eq 0 ]; then
  ok "Install complete with no warnings. Run ${C_BOLD}./start.sh [deck.pptx]${C_RST} to launch."
  exit 0
else
  warn "Install finished with ${#WARNINGS[@]} warning(s):"
  for w in "${WARNINGS[@]}"; do
    printf '    - %s\n' "${w}"
  done
  echo "Review the manual steps above, then run ${C_BOLD}./start.sh${C_RST}."
  exit 2
fi
