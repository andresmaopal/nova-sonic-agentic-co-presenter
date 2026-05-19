#!/usr/bin/env bash
# demo-setup-fullscreen.sh — arrange PowerPoint slideshow and Chrome visor
# tab on adjacent macOS Spaces for the co-presenter demo.
#
# Target layout after this script:
#
#   Desktop 1 (leftmost, empty-ish)
#   Space 2    ← PowerPoint slideshow (fullscreen, native)
#   Space 3    ← Chrome window with visor tab visible (fullscreen, native)
#
# Switching during the demo then becomes Ctrl+← / Ctrl+→ between adjacent
# Spaces — NO exit-slideshow, NO re-enter-slideshow per handoff, NO
# Space create/destroy race. This is the NOVA_USE_SPACES_SWIPE=1 path.
#
# Prerequisites (user-verified, not scripted):
#   1. PowerPoint has the demo .pptx already open (start.sh does this).
#   2. Chrome has the visor tab (http://localhost:3333) loaded.
#   3. PowerPoint and Chrome are NOT pinned to any specific Desktop
#      (Dock → right-click → Options → Assign To → None).
#   4. System Settings → Desktop & Dock → Mission Control →
#      "Automatically rearrange Spaces based on most recent use" OFF.
#   5. System Settings → Keyboard → Keyboard Shortcuts → Mission Control →
#      "Move left a space" (Ctrl+←) and "Move right a space" (Ctrl+→) ENABLED.
#   6. Accessibility permission granted to the terminal running this script.
#
# Post-state on success: user is on Space 2 (PPT slideshow). They can
# begin the demo immediately.
#
# Exit codes:
#   0  — success
#   1  — prerequisite missing (service down, PPT not open, etc.)
#   2  — Accessibility denied (keystroke injection failed)
#   3  — unrecoverable error during setup
#
# Idempotent: if both Spaces already exist in the right layout, this
# script ends on Space 2 without creating duplicates. (Best-effort —
# macOS does not expose public APIs for introspecting Space layout.)

set -u -o pipefail
IFS=$'\n\t'

# ─── colours ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED=$'\033[0;31m' ; GREEN=$'\033[0;32m' ; YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m' ; BOLD=$'\033[1m' ; NC=$'\033[0m'
else
  RED='' ; GREEN='' ; YELLOW='' ; BLUE='' ; BOLD='' ; NC=''
fi

step()  { printf '%s[setup]%s %s\n' "$BLUE" "$NC" "$1"; }
ok()    { printf '%s[ ok  ]%s %s\n'  "$GREEN" "$NC" "$1"; }
warn()  { printf '%s[warn ]%s %s\n'  "$YELLOW" "$NC" "$1"; }
fail()  { printf '%s[fail ]%s %s\n'  "$RED" "$NC" "$1" 1>&2; }
die()   { fail "$1"; exit "${2:-3}"; }

# ─── paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ─── config ───────────────────────────────────────────────────────────
VISOR_URL_PREFIX="${VISOR_URL_PREFIX:-http://localhost:3333}"
# Settle timings — empirically derived. macOS Space animation is ~300 ms
# on Apple Silicon; we leave margin so keystrokes don't stack on each
# other and cause reverse-swipe or skip-swipe bugs.
SWIPE_SETTLE_MS="${SWIPE_SETTLE_MS:-450}"
FULLSCREEN_SETTLE_MS="${FULLSCREEN_SETTLE_MS:-900}"
# How many times to press Ctrl+← during the leftmost reset.
#
# 2026-05-18 — reduced from 20 to 4. The original count was a brute-force
# upper bound ("user might be on Space 17") but in practice the script
# is invoked from a terminal which is almost always on Desktop 1, and
# even with a non-leftmost start the typical case is Space 2-4. With 20
# fast-chained keystrokes (LEFTMOST_CHAIN_MS=80 ms) macOS rendered the
# overflow as a visible flicker storm before the layout work even began —
# the dizzying part the user saw on every demo bring-up. 4 keystrokes is
# enough to handle the 95th-percentile starting position (Desktop 1-4)
# while removing the strobe effect; no-ops past leftmost are silent.
# Override with LEFTMOST_SWIPES=20 if you genuinely roam Spaces 5+.
LEFTMOST_SWIPES="${LEFTMOST_SWIPES:-4}"
# Milliseconds between chained leftmost swipes — shorter than post-swipe
# settle because we're just flushing the queue.
LEFTMOST_CHAIN_MS="${LEFTMOST_CHAIN_MS:-80}"

sleep_ms() {
  # macOS `sleep` accepts fractional seconds on newer versions but the
  # safest portable millisecond sleep is via perl (always installed on
  # macOS). Python3 fallback kept as a second line of defense.
  /usr/bin/perl -e "select undef, undef, undef, $1/1000" 2>/dev/null && return
  /usr/bin/python3 -c "import time; time.sleep($1/1000.0)" 2>/dev/null && return
  /bin/sleep 1  # absolute fallback (we'd rather over-wait than race)
}

now_ms() {
  # Millisecond clock. `date +%s%3N` is GNU-only and breaks on BSD
  # (macOS), hence this perl helper.
  /usr/bin/perl -MTime::HiRes -e 'printf "%d\n", Time::HiRes::time()*1000'
}

# ─── argument ─────────────────────────────────────────────────────────
PPTX="${1:-}"
if [ -z "$PPTX" ] || [ ! -f "$PPTX" ]; then
  die "Usage: $0 <path/to/deck.pptx>" 1
fi
# Resolve to absolute path so start.sh finds it regardless of cwd.
PPTX="$(cd "$(dirname "$PPTX")" && pwd)/$(basename "$PPTX")"

# ─── 0. stop all previous services ───────────────────────────────────
step "0/7 stopping all previous services…"

# Exit any running PowerPoint slideshow first (otherwise PPT refuses to
# start a new one and the script fails on step 3).
osascript \
  -e 'tell application "Microsoft PowerPoint"' \
  -e '  if (count slide show windows) > 0 then' \
  -e '    exit slide show (slide show view of slide show window 1)' \
  -e '  end if' \
  -e 'end tell' 2>/dev/null || true

CHROME_STOP=1 bash "${ROOT_DIR}/stop.sh" 2>/dev/null || true
sleep 1
ok "previous services stopped"

# ─── 0b. restart the stack ───────────────────────────────────────────
step "0b/7 restarting stack (start.sh)…"

bash "${ROOT_DIR}/start.sh" "$PPTX"
ok "stack restarted"

# ─── 1. prerequisite checks ──────────────────────────────────────────
step "1/7 checking prerequisites…"

if [ "$(uname -s)" != "Darwin" ]; then
  die "This script is macOS-only (uname=$(uname -s))." 1
fi

# System Events reachable? Necessary for any keystroke injection.
if ! osascript -e 'tell application "System Events" to get version' \
      >/dev/null 2>&1; then
  fail "System Events is not responding to AppleScript."
  fail "Grant Automation to your terminal: System Settings → Privacy"
  fail "& Security → Automation → ${BOLD}your-terminal${NC} → enable"
  fail "System Events."
  exit 2
fi
ok "System Events reachable"

# PowerPoint has an active presentation?
if ! ppt_name=$(osascript \
     -e 'tell application "Microsoft PowerPoint"' \
     -e '  if not (exists active presentation) then error "no-presentation"' \
     -e '  return name of active presentation' \
     -e 'end tell' 2>/dev/null); then
  die "PowerPoint has no active presentation. Run ./start.sh <deck.pptx> first." 1
fi
ok "PowerPoint has presentation: ${ppt_name}"

# Visor tab reachable via curl?
if ! curl -s -o /dev/null -m 2 -w '%{http_code}' "${VISOR_URL_PREFIX}/" \
     | grep -q '^2..$'; then
  die "Visor not reachable at ${VISOR_URL_PREFIX}. Is ./start.sh running?" 1
fi
ok "Visor reachable at ${VISOR_URL_PREFIX}"

# Chrome has a tab matching the visor prefix?
tab_count=$(osascript \
  -e 'tell application "Google Chrome"' \
  -e '  set n to 0' \
  -e '  repeat with w in windows' \
  -e '    repeat with t in tabs of w' \
  -e "      if URL of t starts with \"${VISOR_URL_PREFIX}\" then set n to n + 1" \
  -e '    end repeat' \
  -e '  end repeat' \
  -e '  return n' \
  -e 'end tell' 2>/dev/null || echo "0")
if [ "${tab_count:-0}" -lt 1 ]; then
  die "No Chrome tab matching ${VISOR_URL_PREFIX}. Did start.sh complete step 5/6?" 1
fi
ok "Chrome has ${tab_count} visor tab(s)"

# ─── 2. reset to leftmost Space ──────────────────────────────────────
step "2/7 resetting to leftmost Space (Ctrl+← × ${LEFTMOST_SWIPES})…"

# Even if we're already on Desktop 1, these keystrokes are cheap no-ops.
# This eliminates any ambiguity about where we start. Individual
# swipes are chained fast (LEFTMOST_CHAIN_MS); macOS coalesces extras.
for ((i=1; i<=LEFTMOST_SWIPES; i++)); do
  if ! osascript -e 'tell application "System Events" to key code 123 using {control down}' \
       >/dev/null 2>&1; then
    fail "Ctrl+← keystroke failed. Accessibility permission likely missing."
    fail "Grant it: System Settings → Privacy & Security → Accessibility"
    fail "→ enable ${BOLD}your-terminal${NC}."
    exit 2
  fi
  sleep_ms "$LEFTMOST_CHAIN_MS"
done
sleep_ms "$SWIPE_SETTLE_MS"
ok "at leftmost Space (Desktop 1)"

# ─── 3. start PowerPoint slideshow → creates Space 2 ─────────────────
step "3/7 starting PowerPoint slideshow (creates new Space)…"

# Force PPT frontmost first — `run slide show` from a background
# AppleScript can create a slideshow window that macOS dismisses one
# frame later if PPT isn't actually foreground (the "Spaces race"
# documented in (internal postmortem 2026-05-09) § 2.3).
osascript \
  -e 'tell application "System Events" to tell process "Microsoft PowerPoint" to set frontmost to true' \
  >/dev/null 2>&1 || warn "PPT frontmost assertion failed (non-fatal)"
sleep_ms 300

slide_state=$(osascript \
  -e 'tell application "Microsoft PowerPoint"' \
  -e '  activate' \
  -e '  if (count slide show windows) > 0 then return "already_running"' \
  -e '  run slide show (slide show settings of active presentation)' \
  -e '  return "started"' \
  -e 'end tell' 2>/dev/null || echo "error")

case "$slide_state" in
  started|already_running)
    ok "slideshow state: ${slide_state}"
    ;;
  *)
    die "PowerPoint refused to start slideshow (state=${slide_state})." 3
    ;;
esac

# Wait for PPT to actually paint the slideshow window. If macOS is
# going to dissolve the Space (the Spaces race), it happens within
# ~400 ms. Poll is_slideshow_active up to 1.5 s.
deadline=$(( $(now_ms) + 1500 ))
slideshow_present=0
while [ "$(now_ms)" -lt "$deadline" ]; do
  count=$(osascript -e 'tell application "Microsoft PowerPoint" to count slide show windows' 2>/dev/null || echo 0)
  if [ "${count:-0}" -gt 0 ]; then
    slideshow_present=1
    break
  fi
  sleep_ms 150
done

if [ "$slideshow_present" -ne 1 ]; then
  fail "PowerPoint accepted 'run slide show' but the window did not stay."
  fail "This is the macOS Spaces race. Try: quit PowerPoint entirely,"
  fail "reopen it via ./start.sh, then re-run this script."
  exit 3
fi
ok "slideshow window confirmed — user is now on Space 2 (PPT)"

# ─── 4. return to Desktop 1 so Chrome fullscreen opens on Space 3 ───
step "4/7 swiping left to Desktop 1 (Ctrl+←)…"

osascript -e 'tell application "System Events" to key code 123 using {control down}' \
  >/dev/null 2>&1 || die "Ctrl+← failed" 2
sleep_ms "$SWIPE_SETTLE_MS"
ok "at Desktop 1"

# ─── 5. bring visor tab to front, activate Chrome ───────────────────
step "5/7 activating Chrome with visor tab in front…"

if ! osascript \
     -e 'tell application "Google Chrome"' \
     -e '  activate' \
     -e "  set target_prefix to \"${VISOR_URL_PREFIX}\"" \
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
  die "Failed to activate Chrome visor tab" 3
fi
sleep_ms 400
ok "Chrome focused with visor tab active"

# ─── 6. Chrome native fullscreen via CDP → creates Space 3 ──────────
step "6/7 entering Chrome native fullscreen via CDP…"

# Why CDP and not Cmd+Ctrl+F keystroke injection:
#
# Observed 2026-05-10: the keystroke path silently no-ops if Chrome
# isn't the strictly-frontmost process at the instant the keystroke
# fires. After a Space swipe + tab activation the animation may still
# be completing when our 400 ms settle ends, and the Cmd+Ctrl+F goes
# to whatever WAS frontmost (often the terminal that launched us),
# producing a "[ok]" log line while Chrome stays in maximized mode.
#
# CDP's Browser.setWindowBounds talks to Chrome directly and does not
# depend on OS-level window focus. It's also observable: we verify
# the resulting windowState equals "fullscreen" before proceeding,
# which keystroke injection cannot do.
PY="${PY:-.venv/bin/python3}"
if [ ! -x "$PY" ]; then
  PY=$(command -v python3 || true)
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  die "python3 not available — required for CDP fullscreen helper" 3
fi
"$PY" "${SCRIPT_DIR}/chrome_set_window_state.py" \
    "${VISOR_URL_PREFIX}" fullscreen \
    || die "Chrome CDP fullscreen request failed (see stderr above)" 3

# Brief pause so the macOS fullscreen animation (~600-800 ms) completes
# before step 7's swipe. Going too fast risks swiping while Chrome's
# Space is still being allocated.
sleep_ms "$FULLSCREEN_SETTLE_MS"
ok "Chrome window is fullscreen — user is now on Space 3 (Chrome)"

# ─── 7. swipe left → back to PPT slideshow Space ─────────────────────
step "7/7 swiping left to PPT slideshow Space (Ctrl+←)…"

osascript -e 'tell application "System Events" to key code 123 using {control down}' \
  >/dev/null 2>&1 || die "final Ctrl+← failed" 2
sleep_ms "$SWIPE_SETTLE_MS"

# Force PPT frontmost to make sure arrow keys advance slides.
osascript \
  -e 'tell application "System Events" to tell process "Microsoft PowerPoint" to set frontmost to true' \
  >/dev/null 2>&1 || warn "final PPT frontmost assertion failed"
sleep_ms 150
ok "at Space 2 (PPT slideshow) — DEMO READY"

# ─── Summary ─────────────────────────────────────────────────────────
cat <<EOF

$GREEN╭──────────────────────────────────────────────────────────────╮$NC
$GREEN│$NC ${BOLD}FULLSCREEN SETUP COMPLETE$NC                                    $GREEN│$NC
$GREEN├──────────────────────────────────────────────────────────────┤$NC
$GREEN│$NC   Desktop 1  (empty placeholder)                             $GREEN│$NC
$GREEN│$NC   Space 2    ← PowerPoint slideshow   ${BOLD}← you are here${NC}         $GREEN│$NC
$GREEN│$NC   Space 3    ← Chrome visor (fullscreen)                     $GREEN│$NC
$GREEN│$NC                                                              $GREEN│$NC
$GREEN│$NC   During the demo:                                           $GREEN│$NC
$GREEN│$NC     • Nova handoff → Ctrl+→  (visor)                         $GREEN│$NC
$GREEN│$NC     • handback     → Ctrl+←  (slides)                        $GREEN│$NC
$GREEN│$NC                                                              $GREEN│$NC
$GREEN│$NC   ${YELLOW}WARNING:${NC} do NOT press Esc in slideshow or exit Chrome   $GREEN│$NC
$GREEN│$NC   fullscreen — both would dissolve their Space and break     $GREEN│$NC
$GREEN│$NC   the swipe mapping. If that happens, re-run this script.    $GREEN│$NC
$GREEN╰──────────────────────────────────────────────────────────────╯$NC

EOF
