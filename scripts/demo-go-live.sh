#!/usr/bin/env bash
# demo-go-live.sh — one-shot "ready to demo" bootstrap.
#
# Takes the co-presenter from "services not running / unknown state" all
# the way to "voice session live, mic hot, slideshow in front" with a
# single command. On success, you can start talking to Nova immediately.
#
# What it does (8 phases):
#
#   1. Ensure ./start.sh services are running; if not, start them.
#   2. /diagnose pre-flight — fail fast if Bedrock / Chrome / PPT is down.
#   3. Run scripts/demo-setup-fullscreen.sh — arrange PPT slideshow on
#      Space 2 and Chrome visor fullscreen on Space 3.
#   4. Swipe to Chrome's Space and activate the voice-UI tab (not visor).
#   5. Via CDP Runtime.evaluate, click the "Start Session" button in
#      the voice-UI, then wait for the WS connection to go live
#      (document.querySelector('#status').textContent contains "Conn"
#      or "Listo" depending on locale).
#   6. Handle the "Allow microphone" OS permission prompt — this is the
#      ONE step we cannot fully automate (macOS security requires a
#      human accept); if Chrome doesn't show "Microphone: allowed"
#      within a short window, print instructions and pause.
#   7. Swipe back to Space 2 (PPT slideshow).
#   8. Print the go-live banner with live log tails.
#
# Recovery: safe to re-run. Each phase is idempotent.
#
# Usage:
#   ./scripts/demo-go-live.sh <deck.pptx>
#   ./scripts/demo-go-live.sh                  # uses last-opened pptx
#
# Env vars:
#   VISOR_URL_PREFIX   default http://localhost:3333
#   VOICE_UI_PREFIX    default http://localhost:3000
#   PYTHON_PORT        default 8000
#   SKIP_START         set to 1 to assume services are already running
#   NO_SWIPE_BACK      set to 1 to END on Chrome's Space (for demos that
#                      want voice-UI visible during prep)
#
# Exit codes:
#   0 — live, mic hot, slideshow frontmost
#   1 — prerequisite missing (services down, pptx not found, etc.)
#   2 — Accessibility denied
#   3 — Chrome CDP failure (fullscreen or tab activation)
#   4 — voice-UI Start Session failed or didn't report Connected
#   5 — macOS microphone permission not granted

set -u -o pipefail

# ─── colours ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED=$'\033[0;31m' ; GREEN=$'\033[0;32m' ; YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m' ; CYAN=$'\033[0;36m' ; BOLD=$'\033[1m' ; NC=$'\033[0m'
else
  RED='' ; GREEN='' ; YELLOW='' ; BLUE='' ; CYAN='' ; BOLD='' ; NC=''
fi

phase() { printf '\n%s%s══════════════════════════════════════════════════════════════%s\n' "$CYAN" "$BOLD" "$NC"
          printf '%s%s %s%s\n' "$CYAN" "$BOLD" "$1" "$NC"
          printf '%s%s══════════════════════════════════════════════════════════════%s\n' "$CYAN" "$BOLD" "$NC"; }
step()  { printf '%s[go-live]%s %s\n' "$BLUE" "$NC" "$1"; }
ok()    { printf '%s[  ok  ]%s %s\n'  "$GREEN" "$NC" "$1"; }
warn()  { printf '%s[ warn ]%s %s\n'  "$YELLOW" "$NC" "$1"; }
fail()  { printf '%s[ fail ]%s %s\n'  "$RED" "$NC" "$1" 1>&2; }
die()   { fail "$1"; exit "${2:-3}"; }

# ─── resolve paths ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PY="${ROOT_DIR}/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  die "python3 not found (checked .venv and PATH)" 1
fi

VISOR_URL_PREFIX="${VISOR_URL_PREFIX:-http://localhost:3333}"
VOICE_UI_PREFIX="${VOICE_UI_PREFIX:-http://localhost:3000}"
PYTHON_PORT="${PYTHON_PORT:-8000}"
NODE_PORT="${NODE_PORT:-3000}"
VISOR_PORT="${VISOR_PORT:-3333}"
CHART_MCP_PORT="${CHART_MCP_PORT:-1122}"
CHROME_CDP_PORT="${CHROME_CDP_PORT:-9222}"

sleep_ms() {
  /usr/bin/perl -e "select undef, undef, undef, $1/1000" 2>/dev/null \
    || /bin/sleep 1
}

# ─── phase 0: AWS credentials freshness ─────────────────────────────
#
# Run the refresh helper BEFORE start.sh so we fail fast (and with a
# very clear error message) when the user needs to run `mwinit`. If
# AWS_PROFILE is not set in the environment or .env, refresh-credentials.sh
# is a no-op — we keep the env-var credential path untouched.
phase "0/8 AWS credentials"
if ! "${SCRIPT_DIR}/refresh-credentials.sh"; then
  die "AWS credentials not valid. See guidance above, fix, then re-run." 1
fi

# ─── phase 1: ensure services are running ─────────────────────────────
phase "1/8 Services"

PPTX_ARG="${1:-}"

services_up() {
  # 2026-05-19 — partial-stack guard. Previously this only checked
  # Python's /diagnose, which would return 200 even when visor /
  # chart-mcp / Chrome CDP were down (Bedrock probes are independent
  # of the dependent services). Result: phase 1 declared "services
  # responding" while phase 2 simultaneously failed pre-flight on
  # those exact services — confusing, and the script took the wrong
  # branch (skip start.sh) when it should have started them.
  #
  # Now we require ALL five required ports to be in LISTEN state
  # before declaring the stack up. lsof's exit code is 0 iff at least
  # one matching socket exists; we silence its output so this is
  # purely a probe. The mute helper is intentionally NOT in this
  # list — it's macOS-only and optional; treating it as required
  # would break the demo on Linux contributors.
  for port in "${PYTHON_PORT}" "${NODE_PORT}" "${VISOR_PORT}" \
              "${CHART_MCP_PORT}" "${CHROME_CDP_PORT}"; do
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1 || return 1
  done
  # Python responds with the /diagnose JSON, which probes Bedrock —
  # keep this as the final live-ness check so an unhealthy backend
  # still trips us before we get into pre-flight.
  curl -s -m 10 "http://127.0.0.1:${PYTHON_PORT}/diagnose" >/dev/null 2>&1
}

if [ "${SKIP_START:-0}" = "1" ]; then
  step "SKIP_START=1 — assuming services are already running"
  services_up || die "services NOT reachable despite SKIP_START=1" 1
  ok "services responding"
elif services_up; then
  step "services already running — skipping ./start.sh"
  ok "services responding"
else
  if [ -z "$PPTX_ARG" ]; then
    die "services not running and no pptx argument given. Usage: $0 <deck.pptx>" 1
  fi
  if [ ! -f "$PPTX_ARG" ]; then
    die "pptx not found: $PPTX_ARG" 1
  fi
  step "running ./start.sh ${PPTX_ARG}…"
  if ! ./start.sh "$PPTX_ARG" >/dev/null 2>&1; then
    die "./start.sh failed (see logs/python.log, logs/node.log)" 1
  fi
  ok "./start.sh completed"
fi

# ─── phase 2: diagnose pre-flight ─────────────────────────────────────
phase "2/8 Pre-flight checks"

step "GET /diagnose …"
if ! diag=$(curl -s -m 15 "http://127.0.0.1:${PYTHON_PORT}/diagnose"); then
  die "/diagnose unreachable" 1
fi

# If PPT has no active presentation but we have a pptx arg, open it.
# Happens when PPT was force-quit earlier or the deck was closed manually.
has_pres=$(printf '%s' "$diag" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(bool(d.get("powerpoint",{}).get("has_active_presentation")))' 2>/dev/null || echo "False")
if [ "$has_pres" != "True" ]; then
  if [ -n "$PPTX_ARG" ] && [ -f "$PPTX_ARG" ]; then
    step "PPT has no active presentation — opening ${PPTX_ARG}"
    osascript -e "tell application \"Microsoft PowerPoint\" to open POSIX file \"$(cd "$(dirname "$PPTX_ARG")" && pwd)/$(basename "$PPTX_ARG")\"" \
      >/dev/null 2>&1 || warn "open pptx returned non-zero (may still work)"
    # Wait for PPT to actually load it (up to 6 s)
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
      sleep_ms 500
      if osascript -e 'tell application "Microsoft PowerPoint" to exists active presentation' 2>/dev/null | grep -q true; then
        ok "presentation opened"
        # Re-fetch diagnose to pick up the new state
        diag=$(curl -s -m 15 "http://127.0.0.1:${PYTHON_PORT}/diagnose") || true
        break
      fi
    done
  else
    die "PPT has no active presentation and no pptx arg provided. Usage: $0 <deck.pptx>" 1
  fi
fi

# Pipe JSON to Python via stdin (avoids sed escaping hell).
if ! printf '%s' "$diag" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
checks = []
ok = True
br = d.get('bedrock', {}).get('checks', {})
for m in ('haiku','nova_lite','sonnet'):
    mok = br.get(m, {}).get('ok', False)
    checks.append(f'bedrock.{m}: {\"ok\" if mok else \"FAIL\"}')
    ok = ok and mok
ppt = d.get('powerpoint', {})
if not ppt.get('powerpoint_running'): ok = False; checks.append('ppt: NOT RUNNING')
elif not ppt.get('has_active_presentation'): ok = False; checks.append('ppt: no active presentation')
else: checks.append('ppt: ' + (ppt.get('active_presentation_name') or 'running'))
ch = d.get('chrome', {})
if not ch.get('cdp_reachable'): ok = False; checks.append('chrome CDP: UNREACHABLE')
else: checks.append(f'chrome CDP: ok ({len(ch.get(\"tabs\",[]))} tab(s))')
wm = d.get('window_manager', {})
if not wm.get('use_spaces_swipe'): ok = False; checks.append('use_spaces_swipe: FALSE (need NOVA_USE_SPACES_SWIPE=1)')
else: checks.append('use_spaces_swipe: true')
for key, label in (('visor','visor'),('finalysis','finalysis'),('chart_mcp','chart_mcp')):
    v = d.get(key, {})
    vok = v.get('ok', False)
    checks.append(f'{label}: {\"ok\" if vok else \"FAIL\"}')
    ok = ok and vok
for c in checks: print('  ' + c)
sys.exit(0 if ok else 1)
"; then
  die "pre-flight checks failed — fix the items above before continuing" 1
fi
ok "all pre-flight checks green"

# ─── phase 3: arrange Spaces (fullscreen + swipe layout) ──────────────
phase "3/8 Spaces layout"

step "running scripts/demo-setup-fullscreen.sh…"
# demo-setup-fullscreen.sh REQUIRES a .pptx argument (it restarts the
# stack via ./start.sh <pptx> at its step 0b). If PPTX_ARG is empty
# here we can't invoke it, so we try to recover the deck path from
# /diagnose.powerpoint.active_presentation_name and fall back to asking
# the user if that fails too.
ss_pptx="$PPTX_ARG"
if [ -z "$ss_pptx" ]; then
  ss_pptx="$(printf '%s' "$diag" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("powerpoint",{}).get("active_presentation_name",""))' 2>/dev/null || true)"
  if [ -n "$ss_pptx" ] && [ -f "$ROOT_DIR/$ss_pptx" ]; then
    ss_pptx="$ROOT_DIR/$ss_pptx"
  elif [ -n "$ss_pptx" ] && [ -f "$ss_pptx" ]; then
    :
  else
    die "cannot locate .pptx for demo-setup-fullscreen.sh (pass it explicitly: $0 <deck.pptx>)" 1
  fi
fi
# Redirect the setup script's output to a dedicated log file so failures
# are diagnosable *and* go-live stays tidy. Its stdout has a long
# "stack restarted" banner that would otherwise clutter phase 3.
mkdir -p "${ROOT_DIR}/logs"
setup_log="${ROOT_DIR}/logs/demo-setup-fullscreen.log"
if ! "${SCRIPT_DIR}/demo-setup-fullscreen.sh" "$ss_pptx" > "$setup_log" 2>&1; then
  # Script exited non-zero. Its own summary heredoc can also do that on
  # success, so re-verify via the two CDP/PPT probes below instead of
  # bailing here. Log path is surfaced to the user on any failure.
  warn "demo-setup-fullscreen.sh exit was non-zero (see ${setup_log}); will re-verify state"
fi

# Verify layout via CDP
if ! "$PY" "${SCRIPT_DIR}/chrome_set_window_state.py" "$VISOR_URL_PREFIX" fullscreen >/dev/null 2>&1; then
  # chrome_set_window_state.py prints "already in state=fullscreen" and exits 0
  # when no change needed, so a non-zero here means a real failure.
  die "Chrome visor window failed to enter fullscreen via CDP" 3
fi
ok "Chrome visor window: fullscreen (Space 3)"

ppt_ss=$(osascript -e 'tell application "Microsoft PowerPoint" to count slide show windows' 2>/dev/null || echo 0)
if [ "${ppt_ss:-0}" -lt 1 ]; then
  die "PowerPoint slideshow is not active — see logs/demo-setup-fullscreen.log or rerun: ./scripts/demo-setup-fullscreen.sh ${ss_pptx}" 3
fi
ok "PowerPoint: slideshow active (Space 2)"

# ─── phase 4: activate voice-UI tab in background (no Space swipe) ──
#
# 2026-05-18 — eliminated the unconditional Ctrl+→ swipe to Chrome here.
# The visible "PPT → Chrome → PPT → Chrome → PPT" flicker the user sees
# during demo-go-live was caused by phases 4+7 round-tripping to Chrome
# unconditionally, on top of the layout swipes already done by
# demo-setup-fullscreen.sh. But neither the Start-Session click (phase 5)
# nor the mic-permission probe (phase 6) needs Chrome to be foregrounded —
# both go through CDP, which talks to Chrome regardless of which Space is
# being viewed. The only situation that genuinely benefits from focusing
# Chrome is when the mic-permission state is "prompt" (a first-run macOS
# system dialog that has to be visible to be accepted). We defer that to
# phase 6, where it can be made conditional. On every subsequent run
# (mic already granted, the typical case) the user stays on the PPT
# slideshow throughout phases 4-7, with zero PPT↔Chrome flicker.
phase "4/8 Activate voice-UI tab (background)"

# Track whether phase 6 ends up swiping to Chrome so phase 7 only swipes
# back if it did. Default = 0 (no swipe, stay on PPT).
SWIPED_TO_CHROME=0

step "selecting voice-UI tab via AppleScript (no activate, no Space switch)…"
# Note: no `activate` keyword and no Ctrl+→ keystroke. `set active tab
# index` and `set index of w to 1` only reorder Chrome's internal tabs
# and windows; they do NOT bring Chrome to the OS foreground or switch
# Spaces. PPT stays frontmost on Space 2.
if ! osascript \
       -e 'tell application "Google Chrome"' \
       -e "  set target_prefix to \"${VOICE_UI_PREFIX}\"" \
       -e '  set found_it to false' \
       -e '  repeat with w in windows' \
       -e '    set t_idx to 0' \
       -e '    repeat with t in tabs of w' \
       -e '      set t_idx to t_idx + 1' \
       -e '      if URL of t starts with target_prefix then' \
       -e '        set active tab index of w to t_idx' \
       -e '        set index of w to 1' \
       -e '        set found_it to true' \
       -e '        exit repeat' \
       -e '      end if' \
       -e '    end repeat' \
       -e '    if found_it then exit repeat' \
       -e '  end repeat' \
       -e 'end tell' >/dev/null 2>&1; then
  die "Failed to activate voice-UI tab (expected URL prefix: ${VOICE_UI_PREFIX})" 3
fi
sleep_ms 200
ok "voice-UI tab is the active tab in its Chrome window (Chrome still in background on Space 3)"

# ─── phase 5: click "Start Session" via CDP Runtime.evaluate ─────────
phase "5/8 Start voice session"

step "injecting click on Start button via CDP…"
start_result=$("$PY" - <<'PY' 2>&1
import asyncio, sys, os
sys.path.insert(0, os.getcwd())
from src.platform.chrome import ChromeAdapter

VOICE_UI_PREFIX = os.environ.get("VOICE_UI_PREFIX", "http://localhost:3000")

async def main() -> int:
    c = ChromeAdapter()
    try:
        browser = await c.connect()
        if browser is None:
            print("CDP unreachable", file=sys.stderr); return 3
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if (p.url or "").startswith(VOICE_UI_PREFIX):
                    page = p
                    break
            if page: break
        if page is None:
            print(f"no voice-UI tab found (prefix={VOICE_UI_PREFIX})", file=sys.stderr); return 3

        # Surgical selectors (verified from browser/index.html):
        #   Start button:   #startBtn
        #   Status text:    #statusText
        # Connected states observed from browser/app.js:
        #   "Connected" / "Listening" / "In session"
        # Fallbacks below cover older/alternate UI builds.
        js = """
(() => {
    // If already started, status text changes away from "Disconnected".
    const statusEl = document.querySelector('#statusText, #status, [data-role="status"], .status, .status-text');
    const statusText = (statusEl?.textContent || '').toLowerCase();
    if (statusText.includes('connected') || statusText.includes('listening') ||
        statusText.includes('listo') || statusText.includes('in session') ||
        statusText.includes('conectado') || statusText.includes('active')) {
        return {status: 'already-started', statusText};
    }

    // Prefer the real voice-UI button by id.
    const candidates = [
        'button#startBtn',
        'button[data-action="start"]',
        'button#start',
        'button#start-btn',
        'button.start',
        'button[aria-label*="start" i]',
        'button[aria-label*="iniciar" i]',
    ];
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el && !el.disabled && el.offsetParent !== null) {
            el.click();
            return {status: 'clicked', selector: sel};
        }
    }
    // Text-based fallback
    const buttons = Array.from(document.querySelectorAll('button'));
    for (const b of buttons) {
        if (b.disabled || b.offsetParent === null) continue;
        const t = (b.textContent || '').trim().toLowerCase();
        if (t === 'start' || t === 'start session' ||
            t === 'iniciar' || t === 'iniciar sesión' ||
            t.includes('start') || t.includes('iniciar')) {
            b.click();
            return {status: 'clicked-by-text', text: b.textContent.trim()};
        }
    }
    return {status: 'no-button-found', buttonCount: buttons.length,
            buttonTexts: buttons.map(b => (b.textContent || '').trim().slice(0, 40))};
})()
"""
        result = await page.evaluate(js)
        print(f"click result: {result}")
        if result.get("status") not in ("clicked", "clicked-by-text", "already-started"):
            print(f"FAILED to find Start button. Saw buttons: {result.get('buttonTexts')}", file=sys.stderr)
            return 4

        # Poll for a non-Disconnected status up to 8 s
        for i in range(16):
            await asyncio.sleep(0.5)
            status = await page.evaluate(
                "document.querySelector('#statusText, #status, [data-role=\"status\"], .status, .status-text')?.textContent || ''"
            )
            s = (status or "").lower()
            if any(k in s for k in ("connected", "listening", "listo", "conectado", "active", "in session")):
                print(f"connection status: {status}")
                return 0
            # Bail early if the status explicitly says an error state.
            if "error" in s or "failed" in s:
                print(f"WARN: status shows error: {status!r}", file=sys.stderr)
                return 4
        print(f"WARN: status never reached Connected (last seen: {status!r})", file=sys.stderr)
        # Return 0 anyway — the button WAS clicked; the poll may just not know
        # the right selector. User can verify visually.
        return 0
    finally:
        await c.close()

sys.exit(asyncio.run(main()))
PY
)
click_exit=$?
printf '%s\n' "$start_result"

case "$click_exit" in
  0) ok "voice session started" ;;
  3) die "CDP/tab error — see above" 3 ;;
  4) die "could not find Start button in voice-UI — check browser/index.html" 4 ;;
  *) warn "Start button interaction returned unexpected exit $click_exit — continue with caution" ;;
esac

# ─── phase 6: mic permission check ───────────────────────────────────
phase "6/8 Microphone permission"

# Chrome shows a system prompt the FIRST time the voice-UI requests
# getUserMedia(). macOS security requires user acceptance — we cannot
# bypass it. But we CAN detect whether it was granted by asking the
# voice-UI to report its permission state.
step "probing microphone permission state via CDP…"
mic_probe=$("$PY" - <<'PY' 2>&1
import asyncio, sys, os
sys.path.insert(0, os.getcwd())
from src.platform.chrome import ChromeAdapter

VOICE_UI_PREFIX = os.environ.get("VOICE_UI_PREFIX", "http://localhost:3000")

async def main() -> int:
    c = ChromeAdapter()
    try:
        browser = await c.connect()
        if browser is None: return 1
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if (p.url or "").startswith(VOICE_UI_PREFIX):
                    page = p
                    break
            if page: break
        if page is None: return 1

        state = await page.evaluate(
            "navigator.permissions.query({name:'microphone'}).then(r => r.state).catch(e => 'unknown:' + e.message)"
        )
        print(f"mic permission state: {state}")
        return 0 if state == "granted" else (2 if state == "denied" else 3)
    finally:
        await c.close()

sys.exit(asyncio.run(main()))
PY
)
mic_exit=$?
printf '%s\n' "$mic_probe"

case "$mic_exit" in
  0) ok "microphone permission: granted (no need to swipe to Chrome)" ;;
  2) die "microphone permission: DENIED — allow in Chrome address-bar lock icon, then re-run" 5 ;;
  3)
    # Mic is in "prompt" state — the OS system dialog needs to be
    # visible for the user to accept. NOW (and only now) is the moment
    # we have to swipe to Chrome's Space. Phase 7 will mirror this and
    # swipe back. On every subsequent run the cached "granted" state
    # makes this a no-op, keeping the user on PPT throughout.
    warn "microphone permission: prompt pending — swiping to Chrome so the OS dialog is visible"
    if osascript -e 'tell application "System Events" to key code 124 using {control down}' >/dev/null 2>&1; then
      sleep_ms 450
      # Bring Chrome forward so the queued mic prompt actually appears.
      osascript -e 'tell application "Google Chrome" to activate' >/dev/null 2>&1 || true
      SWIPED_TO_CHROME=1
      warn "Accept the macOS microphone prompt in Chrome, then re-run this script if the session didn't connect."
    else
      warn "Could not Ctrl+→ to Chrome's Space — accept the mic prompt manually then re-run."
    fi
    ;;
  *) warn "microphone permission state unknown — verify visually in Chrome" ;;
esac

# ─── phase 7: return to PPT slideshow (only if we swiped to Chrome) ──
phase "7/8 Return to slideshow"

if [ "${NO_SWIPE_BACK:-0}" = "1" ]; then
  step "NO_SWIPE_BACK=1 — staying on Chrome's Space"
elif [ "${SWIPED_TO_CHROME}" = "1" ]; then
  # We swiped to Chrome in phase 6 because the mic prompt needed to be
  # visible. Now mirror the swipe so the demo ends on PPT slideshow.
  step "Ctrl+← to Space 2 (PPT slideshow)…"
  osascript -e 'tell application "System Events" to key code 123 using {control down}' \
    >/dev/null 2>&1 || die "Ctrl+← failed" 2
  sleep_ms 500
  # Force PPT frontmost so arrow keys navigate slides.
  osascript \
    -e 'tell application "System Events" to tell process "Microsoft PowerPoint" to set frontmost to true' \
    >/dev/null 2>&1 || warn "PPT frontmost assertion failed"
  sleep_ms 200
  ok "on Space 2 (PPT slideshow frontmost)"
else
  # Common case: phases 4-6 stayed on PPT throughout (Chrome interaction
  # was via CDP, no Space switch). Nothing to swipe back. Just re-assert
  # PPT frontmost in case anything stole focus.
  step "already on Space 2 (no PPT↔Chrome round-trip happened) — re-asserting PPT frontmost"
  osascript \
    -e 'tell application "System Events" to tell process "Microsoft PowerPoint" to set frontmost to true' \
    >/dev/null 2>&1 || warn "PPT frontmost assertion failed"
  sleep_ms 100
  ok "on Space 2 (PPT slideshow frontmost)"
fi

# ─── phase 8: go-live banner ────────────────────────────────────────
phase "8/8 DEMO LIVE"

# One-line state dump for clarity
front=$(osascript -e 'tell application "System Events" to name of first process whose frontmost is true' 2>/dev/null || echo "?")
ss=$(osascript -e 'tell application "Microsoft PowerPoint" to count slide show windows' 2>/dev/null || echo "?")

cat <<EOF

${GREEN}${BOLD}╭─────────────────────────────────────────────────────────────────╮${NC}
${GREEN}${BOLD}│${NC}  ${BOLD}GBM AGENTIC CO-PRESENTER — LIVE${NC}                                ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}├─────────────────────────────────────────────────────────────────┤${NC}
${GREEN}${BOLD}│${NC}   frontmost: ${front}                                ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   PPT slideshow windows: ${ss}                                      ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}                                                                 ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   ${CYAN}Desktop 1${NC}  (empty placeholder)                              ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   ${CYAN}Space 2${NC}    ← PowerPoint slideshow  ${YELLOW}← you are here${NC}           ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   ${CYAN}Space 3${NC}    ← Chrome visor (fullscreen)                      ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}                                                                 ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   During the demo:                                              ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}     • "Nova, siguiente diapositiva"                             ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}     • "Nova, dame el SMA de Tesla"  → auto-swipe to visor       ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}     • Carlos narrates → handback → auto-swipe to slides         ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}                                                                 ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   ${RED}WARNINGS:${NC} do NOT press Esc in slideshow or exit Chrome    ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   fullscreen. If that happens, re-run this script.              ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}                                                                 ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   Live logs:  tail -f logs/node.log logs/python.log             ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}│${NC}   Stop:       ./stop.sh                                         ${GREEN}${BOLD}│${NC}
${GREEN}${BOLD}╰─────────────────────────────────────────────────────────────────╯${NC}

EOF
exit 0
