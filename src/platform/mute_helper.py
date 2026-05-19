"""mute_helper — global spacebar mute hotkey + floating cross-Space mute
indicator for the GBM agentic co-presenter.

Background
==========

The voice UI at ``http://localhost:3000`` has a Mute button that
silences Nova by setting an ``isMuted`` flag the audio worklet reads on
every frame (see ``browser/app.js``). The button only works while
Chrome is the foregrounded app on the user's current Space — but in a
live demo the user spends most of their time on the PowerPoint
slideshow Space (Space 2 in the ``demo-setup-fullscreen.sh`` layout),
not on Chrome (Space 3). Reaching for the mouse to mute mid-sentence
is awkward.

This helper plugs that gap with two coordinated affordances:

1. **Global spacebar hotkey.** A ``CGEventTap`` listens for spacebar
   keydown events system-wide. When the user presses space:

   - If no session is active → pass through (no-op).
   - If PowerPoint is in slideshow mode AND PowerPoint is the
     foregrounded app → pass through, so spacebar continues to
     advance slides during a presentation.
   - Otherwise → ``POST /toggle_mute`` to the Node WS server (which
     broadcasts a ``toggle_mute`` message back to every connected
     browser, where the existing ``applyMuteState()`` does the
     actual mic-frame gate). The keystroke is suppressed so it
     doesn't ALSO scroll a webpage or insert a space in a text
     field at the moment of muting.

2. **Floating cross-Space indicator.** A small ``NSWindow`` configured
   with ``NSWindowCollectionBehaviorCanJoinAllSpaces |
   NSWindowCollectionBehaviorFullScreenAuxiliary`` sits in the
   top-right corner of the main display. It's visible above
   PowerPoint's slideshow fullscreen and Chrome's fullscreen visor,
   shows ``Live`` (green) or ``Muted`` (red) — no emoji, plain
   horizontally-and-vertically-centered text — and hides entirely
   when no session is active. Polls ``GET /mute_state`` at 250 ms
   cadence so the indicator updates within ~half a press of the
   spacebar.

3. **Spacebar override DURING PowerPoint slideshow.** The CGEventTap
   sits at ``kCGSessionEventTap`` priority, which intercepts keystrokes
   BEFORE the frontmost app sees them. A spacebar press during PPT
   slideshow now toggles mute (instead of advancing the slide). PPT
   on macOS has no first-party way to remap or disable spacebar=advance
   selectively — KioskMode disables ALL advance methods, which isn't
   what we want. The CGEventTap approach is clean (nothing modified in
   PPT itself) and reversible (when the helper exits, PPT immediately
   regains its default spacebar behaviour). Trade-off: presenters
   advance slides via → / N / Page Down while a Nova session is
   active — most professional presenters prefer those keys anyway since
   spacebar also triggers animation steps and is therefore ambiguous.

Dependencies
============

PyObjC frameworks. ``pyobjc-framework-Quartz`` is already in
``requirements.txt`` (used by the keyboard-hook PowerPoint poller);
the AppKit / Foundation / Cocoa frameworks are pulled in transitively
on the project's venv but should be made explicit in
``requirements.txt`` for resilience.

Run as a separate process from the main stack — ``start.sh`` spawns
this in the background, ``stop.sh`` kills it.

Permissions
===========

macOS Accessibility permission is required to install a
``CGEventTap``. The same permission is already required by the
project for the AppleScript keystroke injection
(``Ctrl+←/→`` Space swipes in ``demo-setup-fullscreen.sh``), so the
demo workflow already requests / receives it.

If the tap can't be created, the indicator-only path still runs and
the helper logs ``mute_helper: CGEventTap creation failed — Accessibility
permission missing?`` so the user knows what to fix.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import httpx

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────

NODE_BASE_URL = os.environ.get("NOVA_NODE_BASE_URL", "http://127.0.0.1:3000")
"""Where the Node WS server lives. Same default as the rest of the stack."""

POLL_INTERVAL_MS = int(os.environ.get("NOVA_MUTE_POLL_INTERVAL_MS", "250"))
"""How fast the indicator picks up mute-state changes from the server."""

PPT_POLL_INTERVAL_MS = int(os.environ.get("NOVA_MUTE_PPT_POLL_MS", "500"))
"""How fast we re-check PowerPoint slideshow state. The keyboard_hook
PowerPoint poller already runs at the same cadence; we don't share its
state because the two processes are independent and a missed press is
much worse here than there."""

POWERPOINT_BUNDLE_ID = "com.microsoft.Powerpoint"
"""macOS bundle identifier for Microsoft PowerPoint (verified across
2016/2019/365 builds)."""

SPACE_KEYCODE = 49
"""macOS virtual keycode for spacebar. Stable across keyboard layouts."""


# ── Process-global state ───────────────────────────────────────

class _State:
    """Cached state read by the CGEventTap callback (no I/O on the
    keystroke path) and written by the background pollers."""

    __slots__ = ("muted", "session_active", "ppt_in_slideshow")

    def __init__(self) -> None:
        self.muted: bool = False
        self.session_active: bool = False
        self.ppt_in_slideshow: bool = False


_state = _State()


# ── 1. CGEventTap callback — global spacebar listener ─────────

def _toggle_mute_remote() -> None:
    """Fire-and-forget POST to the Node WS server. Synchronous so it
    completes within the CGEventTap callback's window, but with a
    tight timeout so a stuck server doesn't block keystroke handling.
    """
    try:
        httpx.post(f"{NODE_BASE_URL}/toggle_mute", timeout=1.0)
    except httpx.HTTPError as exc:
        logger.info("mute_helper: toggle_mute POST failed: %s", exc)


def _ppt_is_frontmost() -> bool:
    """Return True iff PowerPoint is the foregrounded app right now.
    Lazy-imports NSWorkspace so a failed AppKit import doesn't crash
    the whole helper at startup (the indicator would still work)."""
    try:
        from AppKit import NSWorkspace  # noqa: WPS433 — intentional lazy import
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return False
        return front.bundleIdentifier() == POWERPOINT_BUNDLE_ID
    except Exception:   # noqa: BLE001 — keystroke path must never raise
        return False


def _event_tap_callback(proxy, event_type, event, refcon):  # noqa: ARG001
    """Called by macOS for every keydown event while the tap is enabled.
    Returns the original event to pass it through, or ``None`` to
    suppress it. Must be FAST — anything more than ~10 ms here causes
    the user's keypress to feel laggy."""
    from Quartz import (
        CGEventGetIntegerValueField, kCGKeyboardEventKeycode,
    )

    keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
    if keycode != SPACE_KEYCODE:
        return event

    # Pass-through 1: no session → don't intercept anything.
    if not _state.session_active:
        return event

    # Pass-through 1: no session → don't intercept anything.
    # PowerPoint, Chrome, every other app gets the press as normal.
    if not _state.session_active:
        return event

    # 2026-05-19 — design change: spacebar is now ALWAYS a mute toggle
    # while a Nova session is active, INCLUDING during PowerPoint
    # slideshow. The previous "pass through to PPT slideshow" gate was
    # removed at user request. PowerPoint for Mac has no API to
    # customise the slide-advance shortcut (Microsoft confirmed there's
    # no equivalent of Word's "Customise Keyboard" UI, and the only
    # first-party way to disable spacebar=advance is KioskMode which
    # disables ALL advance methods). The CGEventTap at
    # kCGSessionEventTap priority intercepts keystrokes BEFORE they
    # reach the frontmost app, so suppressing the event here means PPT
    # never sees the press — a clean, reversible override that costs
    # nothing when the helper is stopped.
    #
    # Trade-off: presenters must use → / N / Page Down to advance
    # slides while a session is active. Most pro presenters already
    # prefer → because spacebar also triggers animation steps, which
    # makes its behaviour ambiguous mid-bullet.
    _toggle_mute_remote()
    return None


def _start_event_tap() -> bool:
    """Install the CGEventTap on the current run loop. Returns True
    on success, False if the tap couldn't be created (typically:
    Accessibility permission missing)."""
    from Quartz import (
        CGEventTapCreate, kCGSessionEventTap, kCGHeadInsertEventTap,
        kCGEventTapOptionDefault, kCGEventKeyDown,
        CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
        CFRunLoopGetCurrent, kCFRunLoopCommonModes, CGEventTapEnable,
    )

    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        1 << kCGEventKeyDown,
        _event_tap_callback,
        None,
    )
    if tap is None:
        logger.warning(
            "mute_helper: CGEventTap creation failed — Accessibility "
            "permission missing? Indicator still runs; spacebar global "
            "hotkey is disabled until you grant: System Settings → "
            "Privacy & Security → Accessibility → enable your terminal."
        )
        return False

    runloop_source = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), runloop_source, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    logger.info("mute_helper: CGEventTap installed (spacebar global hotkey)")
    return True


# ── 2. Background pollers ─────────────────────────────────────

def _poll_node_state() -> None:
    """Keep ``_state.muted`` and ``_state.session_active`` synced with
    the Node WS server's snapshot, and refresh the indicator on every
    change. Runs forever in a daemon thread."""
    last: tuple[bool, bool] | None = None
    interval_s = POLL_INTERVAL_MS / 1000.0
    backoff_s = interval_s
    while True:
        try:
            r = httpx.get(f"{NODE_BASE_URL}/mute_state", timeout=2.0)
            d = r.json()
            _state.muted = bool(d.get("muted", False))
            _state.session_active = bool(d.get("session_active", False))
            current = (_state.muted, _state.session_active)
            if current != last:
                _refresh_indicator_async()
                # Notification banner — bulletproof second signal
                # above fullscreen apps. The floating overlay is the
                # primary affordance (continuously visible), but
                # macOS NSWindow rendering above fullscreen is
                # famously fragile across OS / GPU / app
                # combinations. The notification banner uses the
                # system Notification Center, which has been visible
                # above EVERY fullscreen surface since macOS 10.15;
                # if for any reason the floating overlay doesn't
                # appear on the user's setup, the banner still tells
                # them their press registered.
                if last is not None:
                    # Skip the very first poll (transitions FROM
                    # session-not-active aren't user actions).
                    _fire_notification_async(
                        muted=_state.muted,
                        session_active=_state.session_active,
                    )
                last = current
            backoff_s = interval_s
        except Exception:  # noqa: BLE001 — poller must never die
            # Server might be down briefly during ./stop.sh / ./start.sh
            # transitions. Back off so we don't spam the network and
            # logs, but don't give up.
            backoff_s = min(backoff_s * 2, 5.0)
        time.sleep(backoff_s)


def _fire_notification_async(*, muted: bool, session_active: bool) -> None:
    """Fire a brief macOS notification banner via osascript. Runs in
    a background thread because osascript can take 50-150 ms and we
    don't want that on the poller's hot loop. Safe to call from any
    thread; failures are swallowed (the indicator is still authoritative).

    Why osascript display notification:
    - Works above fullscreen apps WITHOUT requiring window-level
      tricks (uses system Notification Center).
    - Auto-dismisses after a few seconds — perfect for a transient
      "you just pressed mute" signal.
    - Already trusted by the project (used everywhere for AppleScript).
    - First-call asks for Notification permission, which is per-user
      and persistent.
    """
    if not session_active:
        # On session-end the floating overlay disappears; firing a
        # banner would just be noise.
        return

    if muted:
        title = "🔇 Muted"
        body = "Nova won't hear you. Press Space again to unmute."
    else:
        title = "🎤 Live"
        body = "Nova is listening. Press Space again to mute."

    def _shell_out():
        # Quote-escape via Python's repr-style escape so user-facing
        # strings can never break out of the AppleScript literal.
        try:
            import subprocess
            esc_title = title.replace('"', '\\"')
            esc_body = body.replace('"', '\\"')
            script = (
                f'display notification "{esc_body}" '
                f'with title "{esc_title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                timeout=2.0, check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_shell_out, daemon=True, name="mute-notify").start()


def _poll_ppt_state() -> None:
    """Keep ``_state.ppt_in_slideshow`` fresh so the CGEventTap
    callback can answer without doing AppleScript IO on the keystroke
    path. Runs forever in a daemon thread."""
    interval_s = PPT_POLL_INTERVAL_MS / 1000.0
    # Lazy import so the helper can start even if the powerpoint
    # module fails to import (e.g., osascript not installed).
    try:
        from src.platform import powerpoint as ppt
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "mute_helper: src.platform.powerpoint import failed (%s) — "
            "PPT slideshow detection disabled, spacebar will always toggle mute",
            exc,
        )
        return
    while True:
        try:
            _state.ppt_in_slideshow = ppt.is_running() and ppt.is_slideshow_active()
        except Exception:  # noqa: BLE001
            _state.ppt_in_slideshow = False
        time.sleep(interval_s)


# ── 3. Floating cross-Space indicator window ──────────────────

# These are populated on the main thread once NSApplication has booted.
_indicator_window = None
_indicator_label = None
_indicator_pill = None


def _build_indicator_window():
    """Construct the floating overlay. Must run on the main thread.
    Returns ``(window, label, pill)`` so the refresher can update both
    the text (label) and the colored pill background (pill view).

    Layout (post 2026-05-19):
       ┌─────────────────────────┐  ← NSWindow (transparent, click-through)
       │  ╭───────────────────╮  │
       │  │       Live        │  │  ← NSView pill (colored, rounded)
       │  ╰───────────────────╯  │     contains a single NSTextField
       └─────────────────────────┘     sized to the font's line height
                                       and y-centered inside the pill.

    Why two views instead of one self-styled NSTextField:
    NSTextField with a non-zero ``setBackgroundColor_`` paints its
    pill background AT THE FRAME. So if you size the field to match
    the text height (for vertical centering), the colored pill becomes
    a thin strip — visibly wrong. Splitting into a layer-backed pill
    view + a transparent label decouples "where the colored pill is"
    from "where the text is", letting each be sized for its own
    purpose. The label fills the pill horizontally (so
    ``NSTextAlignmentCenter`` works) but its frame height matches the
    font's line height and is y-centered inside the pill — giving true
    vertical centering without a custom NSTextFieldCell subclass.
    """
    from AppKit import (
        NSWindow, NSScreen, NSColor, NSTextField, NSView,
        NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
        NSPopUpMenuWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSFont, NSFontWeightSemibold,
    )
    from Foundation import NSMakeRect

    # Narrower pill since the icon emoji was removed at user request
    # 2026-05-19 — "Live AI" / "Muted" don't need the extra width that
    # the 🎤/🔇 prefix used to occupy. Width tuned for the longest
    # label ("Live AI" at 15pt semibold ≈ 60 px text width + ~20 px
    # padding on each side).
    width, height = 100, 36
    screen_frame = NSScreen.mainScreen().frame()
    # Top-right of the main display. ``frame()`` is in Cocoa
    # coordinates (origin at bottom-left), so "top" is the largest y.
    margin = 16
    x = screen_frame.origin.x + screen_frame.size.width - width - margin
    y = screen_frame.origin.y + screen_frame.size.height - height - margin
    rect = NSMakeRect(x, y, width, height)

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )

    # 2026-05-19 — bumped window level from NSStatusWindowLevel (25) to
    # NSPopUpMenuWindowLevel (101). Both can sit above fullscreen Spaces
    # in principle, but on macOS Sonoma+ the lower status level
    # occasionally rendered BEHIND the fullscreen content under heavy
    # GPU load (PowerPoint slideshow with transitions or Chrome
    # fullscreen with active tab). PopUpMenu level is what context
    # menus and tooltips use — it's reliably above app content but
    # below the screen-saver level (1000), which we don't want to
    # share lest macOS treat our overlay as a screensaver.
    # FullScreenAuxiliary is what actually grants the cross-fullscreen
    # privilege; the level just decides z-order within that privilege.
    win.setLevel_(NSPopUpMenuWindowLevel)
    win.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary,
    )

    # Click-through so the indicator can never accidentally steal a
    # click from PPT or anything else underneath.
    win.setIgnoresMouseEvents_(True)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setHasShadow_(True)
    # Don't show in window menus or Mission Control's app switcher.
    win.setHidesOnDeactivate_(False)

    # ────────── Pill background (layer-backed NSView) ──────────
    pill = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    pill.setWantsLayer_(True)
    pill_layer = pill.layer()
    pill_layer.setCornerRadius_(height / 2.0)
    pill_layer.setMasksToBounds_(True)
    # Initial background color set by the first _refresh_indicator call.

    # ────────── Text label (transparent, y-centered) ──────────
    # 15-pt semibold reads cleaner on the colored pill than the
    # default regular weight without making the pill feel shouty.
    font = NSFont.systemFontOfSize_weight_(15.0, NSFontWeightSemibold)
    # ascender - descender ≈ visual height of a line of glyphs;
    # the +2 padding stops descenders from kissing the pill edge if
    # the user ever localises the strings (e.g., "Encendido").
    glyph_height = float(font.ascender() - font.descender()) + 2.0
    label_y = (height - glyph_height) / 2.0
    label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(0, label_y, width, glyph_height),
    )
    label.setBezeled_(False)
    label.setDrawsBackground_(False)  # transparent so the pill shows
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(1)  # NSTextAlignmentCenter
    label.setFont_(font)
    label.setTextColor_(NSColor.whiteColor())

    pill.addSubview_(label)
    win.contentView().addSubview_(pill)
    return win, label, pill


def _refresh_indicator_async() -> None:
    """Hop to the AppKit main thread to update the overlay. Safe to
    call from any thread (the pollers, the event tap, …)."""
    from PyObjCTools import AppHelper
    AppHelper.callAfter(_refresh_indicator)


def _refresh_indicator() -> None:
    """Read ``_state`` and update the overlay. Must run on the main
    thread."""
    global _indicator_window, _indicator_label, _indicator_pill

    if _indicator_window is None:
        _indicator_window, _indicator_label, _indicator_pill = (
            _build_indicator_window()
        )

    if not _state.session_active:
        _indicator_window.orderOut_(None)
        return

    # Live = green pill, Muted = warm red pill. Pure-text labels (no
    # emoji) at user request 2026-05-19 — clearer on the colored pill
    # and avoids any font-fallback weirdness with emoji glyphs in
    # AppKit text fields.
    from AppKit import NSColor
    if _state.muted:
        _indicator_label.setStringValue_("Muted")
        bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.80, 0.20, 0.18, 0.92,  # warm red
        )
    else:
        _indicator_label.setStringValue_("Live AI")
        bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.18, 0.55, 0.34, 0.92,  # green
        )
    # CALayer.backgroundColor takes a CGColor, so convert via NSColor's
    # CGColor accessor (available since macOS 10.8). PyObjC bridges
    # this as a method call.
    _indicator_pill.layer().setBackgroundColor_(bg.CGColor())

    # orderFront keeps the overlay above newly-created fullscreen
    # Spaces — without this, swiping to Chrome fullscreen could let
    # Chrome's window cover the overlay.
    _indicator_window.orderFront_(None)


# ── 4. Main ──────────────────────────────────────────────────

def main() -> int:
    """Entry point. Runs the AppKit event loop forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] mute_helper: %(message)s",
    )

    # Boot NSApplication BEFORE installing the event tap so the run
    # loop the tap binds to is the AppKit one.
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    nsapp = NSApplication.sharedApplication()
    # 2026-05-19 — set Accessory policy explicitly. Without this, a
    # Python-launched NSApplication can be ambiguous about its
    # activation state, which causes borderless transparent windows to
    # sometimes not draw at all (the system thinks the app isn't
    # foreground enough to grant rendering). Accessory = "no Dock
    # icon, no app menu, but can show floating windows reliably" —
    # the same policy macOS menu-bar utilities use.
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    tap_ok = _start_event_tap()
    if not tap_ok:
        logger.warning(
            "mute_helper: running indicator-only (no spacebar hotkey). "
            "Re-grant Accessibility and restart this helper to enable.",
        )

    # Background pollers.
    threading.Thread(target=_poll_node_state, daemon=True, name="mute-node-poll").start()
    threading.Thread(target=_poll_ppt_state, daemon=True, name="mute-ppt-poll").start()

    # Build the indicator on the main thread before runEventLoop blocks
    # so the user sees it appear immediately on the next session_started.
    _refresh_indicator()

    logger.info(
        "mute_helper: ready (Node=%s, poll=%dms, ppt_poll=%dms, hotkey=%s)",
        NODE_BASE_URL, POLL_INTERVAL_MS, PPT_POLL_INTERVAL_MS,
        "on" if tap_ok else "off",
    )
    from PyObjCTools import AppHelper
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        logger.info("mute_helper: SIGINT — shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
