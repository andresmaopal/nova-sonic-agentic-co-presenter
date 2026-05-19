"""Spaces — macOS Mission Control Space navigation primitives.

Provides keystroke-based Space switching via AppleScript (System Events).
This module is the foundation for the dual-fullscreen architecture where
PowerPoint slideshow lives on one Space and the Chrome visor lives on
another; we navigate between them with Ctrl+←/→ (the macOS-native
"Move one space left/right" shortcut) instead of exiting/re-entering
slideshow mode on every handoff.

Why keystroke injection instead of a direct API:

- macOS does NOT expose a public API to change the active Space.
  ``CGSGetActiveSpace`` / ``CGSManagedDisplayGetCurrentSpace`` are
  private SPI under ``SkyLight.framework`` — using them requires
  reverse-engineered bindings and breaks across macOS versions.
- The ``Ctrl+←/→`` shortcut goes through the window server BEFORE any
  app sees it, so PowerPoint in slideshow (which reads plain arrow keys
  for navigation) does NOT intercept Ctrl+Arrow.
- This path has been stable since Lion (2011) — Apple has had 15 years
  to fix the edge cases in the Space-switch animation.

Caveats the caller must understand:

- A successful return does NOT mean the Space actually changed. It
  means the keystroke was injected successfully. If the shortcut is
  disabled in System Settings → Keyboard → Keyboard Shortcuts →
  Mission Control, osascript succeeds silently and nothing happens.
  Verification requires the private APIs mentioned above or an
  observable side effect (focused-app change).
- Accessibility permission is required for the host process (in
  addition to the Automation permission this project already needs
  for PowerPoint). System Settings → Privacy & Security →
  Accessibility.
- Key codes 123/124 are virtual key codes referring to PHYSICAL key
  positions (stable across US/ISO/JIS layouts). They do not change
  with keyboard locale.

This module is macOS-only. On other platforms every function returns
``SpacesResult(ok=False, code="UNSUPPORTED_OS", ...)``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpacesResult:
    """Uniform return type for every Spaces operation.

    Parallels :class:`src.platform.powerpoint.PptResult` and
    :class:`src.platform.chrome.ChromeResult` so callers can handle all
    three platforms through the same interface.
    """

    ok: bool
    code: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view, safe for tool_result payloads."""
        return {"ok": self.ok, "code": self.code, "message": self.message,
                **self.data}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Virtual key codes (physical positions, locale-independent).
KEY_CODE_LEFT_ARROW = 123
KEY_CODE_RIGHT_ARROW = 124

# Empirically the Spaces transition animation is ~300 ms on Apple
# Silicon. 400 ms is a safe default that leaves a small visual
# "settle" buffer for the next narration line.
DEFAULT_SETTLE_S = 0.4

# When chaining swipes in a loop (e.g. ``swipe_to_leftmost``), we only
# need enough pause for macOS to register each keystroke as distinct
# (without it, multiple keystrokes within one animation frame can be
# coalesced into a single swipe). Empirically 100 ms is enough.
CHAIN_SETTLE_S = 0.1

# Upper bound for ``swipe_to_leftmost`` — macOS supports up to 16
# Spaces per display in practice. Extra swipes past Space 1 are cheap
# no-ops, but we bound the loop anyway as a defensive measure.
MAX_SPACES_LIMIT = 16

_OSASCRIPT_TIMEOUT = 3.0

_VALID_DIRECTIONS = frozenset({"left", "right"})

# Error-code subset relevant to Spaces ops. Keystroke injection mainly
# fails on permission issues (Accessibility denied or System Events
# refused the Apple event).
_ERROR_CODES: Dict[int, tuple[str, str]] = {
    -1743: ("NO_PERMISSION",
            "macOS denied programmatic keystroke injection. Grant "
            "Accessibility to your terminal in System Settings → "
            "Privacy & Security → Accessibility."),
    -1712: ("TIMED_OUT", "osascript didn't respond in time."),
    -600:  ("NOT_RUNNING", "System Events isn't running."),
    -609:  ("CONNECTION_LOST", "Lost connection to System Events."),
}

_ERR_NUM_RE = re.compile(r"\((-?\d+)\)")


# ---------------------------------------------------------------------------
# Low-level runner (private)
# ---------------------------------------------------------------------------


def _parse_error(stderr: str) -> tuple[str, str]:
    """Convert osascript stderr into a (code, friendly_message) pair."""
    stderr = stderr.strip() or "Unknown osascript error"
    m = _ERR_NUM_RE.search(stderr)
    if m:
        try:
            num = int(m.group(1))
        except ValueError:
            num = None
        if num is not None and num in _ERROR_CODES:
            return _ERROR_CODES[num]
    return "OSASCRIPT_ERROR", stderr


def _run(script: str, *, timeout: float = _OSASCRIPT_TIMEOUT) -> SpacesResult:
    """Execute an AppleScript snippet. Never raises."""
    if sys.platform != "darwin":
        return SpacesResult(False, "UNSUPPORTED_OS",
                            "Spaces control requires macOS.")

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SpacesResult(False, "TIMED_OUT",
                            f"osascript didn't respond within {timeout:.0f}s.")
    except FileNotFoundError:
        return SpacesResult(False, "NO_OSASCRIPT",
                            "`osascript` binary missing — not a supported "
                            "macOS environment.")
    except OSError as exc:
        return SpacesResult(False, "OS_ERROR",
                            f"Failed to invoke osascript: {exc}")

    if result.returncode != 0:
        code, msg = _parse_error(result.stderr)
        return SpacesResult(False, code, msg,
                            {"stderr": result.stderr.strip()})

    return SpacesResult(True, "OK", "ok",
                        {"stdout": result.stdout.strip()})


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def swipe(direction: str, *,
          settle_s: float = DEFAULT_SETTLE_S) -> SpacesResult:
    """Swipe one Space in ``direction`` ('left' or 'right').

    Injects Ctrl+← or Ctrl+→ via ``System Events``. The shortcut is
    handled by the macOS window server before any foreground app sees
    it, so PowerPoint in slideshow (which reads plain arrow keys) does
    NOT intercept Ctrl+Arrow.

    Args:
        direction: 'left' or 'right' (case-insensitive, surrounding
            whitespace is tolerated).
        settle_s: Seconds to sleep after the keystroke for the Spaces
            animation to complete. Default is
            :data:`DEFAULT_SETTLE_S` (0.4 s). Pass 0 to skip sleeping
            (useful when chaining multiple swipes — each keystroke
            fires while the previous animation is still in progress).

    Returns:
        :class:`SpacesResult`. Codes:

        - ``OK`` — keystroke injected, settle slept.
        - ``BAD_ARGS`` — invalid direction.
        - ``UNSUPPORTED_OS`` — not macOS.
        - ``NO_PERMISSION`` — Accessibility denied (-1743).
        - ``TIMED_OUT`` — osascript hung.
        - ``NO_OSASCRIPT`` — binary missing.
        - ``OSASCRIPT_ERROR`` — any other osascript failure.

    **Caveat — successful injection ≠ Space changed.** If the shortcut
    is disabled in System Settings (Keyboard → Shortcuts → Mission
    Control → "Move left/right a space"), osascript still returns 0
    and ``ok=True``. True verification requires private APIs we don't
    bind to. Callers that need to confirm the Space change should
    cross-check via an observable side effect (e.g. which app is
    frontmost after the swipe).
    """
    normalized = (direction or "").strip().lower()
    if normalized not in _VALID_DIRECTIONS:
        return SpacesResult(
            False, "BAD_ARGS",
            f"direction must be one of {sorted(_VALID_DIRECTIONS)}, "
            f"got {direction!r}",
            data={"direction": direction},
        )

    key_code = (KEY_CODE_LEFT_ARROW if normalized == "left"
                else KEY_CODE_RIGHT_ARROW)
    script = (
        'tell application "System Events" '
        f'to key code {key_code} using {{control down}}'
    )
    result = _run(script)
    base_data = {"direction": normalized, "key_code": key_code}

    if not result.ok:
        return SpacesResult(
            result.ok, result.code, result.message,
            data={**result.data, **base_data},
        )

    if settle_s > 0:
        time.sleep(settle_s)

    return SpacesResult(
        True, "OK",
        f"swiped {normalized}",
        data={**base_data, "settle_ms": int(settle_s * 1000)},
    )


def swipe_to_leftmost(*,
                      max_swipes: int = MAX_SPACES_LIMIT,
                      settle_s: float = CHAIN_SETTLE_S) -> SpacesResult:
    """Swipe left repeatedly until we're at the leftmost Space.

    Defensive reset for session start: we don't know which Space is
    currently active, so bring the user to a known state before we
    arrange PPT and Chrome on their own Spaces. Extra swipes past
    Space 1 are cheap no-ops (macOS refuses to swipe further left).

    On the first keystroke failure the loop stops early — errors in
    keystroke injection are almost always configuration problems
    (Accessibility denied, shortcut disabled) that will repeat for
    every subsequent attempt, so retrying is wasted effort.

    Args:
        max_swipes: Upper bound on swipe count. Default
            :data:`MAX_SPACES_LIMIT` (16) matches macOS's practical
            per-display Space limit.
        settle_s: Pause between swipes. Default
            :data:`CHAIN_SETTLE_S` (0.1 s) — just enough for macOS to
            register each keystroke as distinct.

    Returns:
        :class:`SpacesResult`. On success, ``data["swipes_attempted"]``
        echoes how many keystrokes were injected. On failure, the
        first error is returned with ``swipes_attempted`` set to the
        count completed before the failure.
    """
    if not isinstance(max_swipes, int) or isinstance(max_swipes, bool):
        return SpacesResult(
            False, "BAD_ARGS",
            f"max_swipes must be int, got {type(max_swipes).__name__}",
        )
    if max_swipes < 1:
        return SpacesResult(
            False, "BAD_ARGS",
            f"max_swipes must be >= 1, got {max_swipes}",
        )

    attempted = 0
    for _ in range(max_swipes):
        res = swipe("left", settle_s=settle_s)
        attempted += 1
        if not res.ok:
            return SpacesResult(
                False, res.code,
                f"{res.message} (swipe {attempted}/{max_swipes})",
                data={
                    **res.data,
                    "swipes_attempted": attempted,
                    "direction": "left",
                },
            )

    return SpacesResult(
        True, "OK",
        f"swiped to leftmost ({attempted} swipes)",
        data={"swipes_attempted": attempted, "direction": "left"},
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def diagnose() -> Dict[str, Any]:
    """Snapshot for the ``/diagnose`` endpoint. Never raises.

    Reports OS support and whether System Events is reachable (a
    necessary-but-not-sufficient condition for keystroke injection).
    We cannot directly probe whether the Mission Control shortcut is
    enabled in System Settings — that requires reading
    ``com.apple.symbolichotkeys.plist`` which has an undocumented
    format. We surface that as a caveat in ``notes`` instead.
    """
    os_ok = sys.platform == "darwin"
    probe: Optional[SpacesResult] = None
    if os_ok:
        # Harmless probe: ask System Events for its version. If this
        # succeeds, Automation permission for System Events is
        # granted. (Accessibility, required separately for keystroke
        # injection, is NOT probed by this call — System Events can
        # respond to info requests without it.)
        probe = _run(
            'tell application "System Events" to get version',
            timeout=1.5,
        )

    return {
        "os_supported": os_ok,
        "os_platform": sys.platform,
        "system_events_reachable": bool(probe and probe.ok),
        "system_events_probe_code": (
            probe.code if probe and not probe.ok else None
        ),
        "system_events_version": (
            probe.data.get("stdout") if probe and probe.ok else None
        ),
        "key_codes": {
            "left_arrow": KEY_CODE_LEFT_ARROW,
            "right_arrow": KEY_CODE_RIGHT_ARROW,
        },
        "notes": [
            "Keystroke injection additionally requires Accessibility "
            "permission (System Settings → Privacy & Security → "
            "Accessibility).",
            "Space shortcuts must be enabled (System Settings → "
            "Keyboard → Keyboard Shortcuts → Mission Control → "
            "'Move left/right a space').",
            "A successful keystroke injection does NOT guarantee the "
            "Space actually changed — macOS has no public API to "
            "verify. Cross-check via the focused-app change if "
            "confirmation matters.",
        ],
    }
