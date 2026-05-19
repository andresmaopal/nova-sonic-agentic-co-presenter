"""PowerPoint platform adapter — single entry point for all AppleScript calls.

Centralizes every interaction with Microsoft PowerPoint on macOS so we have
one place to:

* detect the install + runtime state (version, running, has presentation),
* map raw AppleScript errors to user-friendly messages,
* support both regular slideshow and Presenter View,
* gracefully degrade when PowerPoint is unavailable.

All public functions return a :class:`PptResult` object with a boolean
``ok`` flag, a human-readable ``message``, an optional ``data`` payload,
and a machine-readable ``code`` for callers that want to branch.

This module is macOS-only.  On other platforms every function returns
``PptResult(ok=False, code="UNSUPPORTED_OS", ...)``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PptResult:
    """Uniform return type for every PowerPoint operation."""

    ok: bool
    code: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view, safe for tool_result payloads."""
        return {"ok": self.ok, "code": self.code, "message": self.message, **self.data}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUNDLE_ID = "com.microsoft.Powerpoint"
_APP_NAME = "Microsoft PowerPoint"
_APP_PATHS = (
    Path("/Applications/Microsoft PowerPoint.app"),
    Path.home() / "Applications/Microsoft PowerPoint.app",
)
_OSASCRIPT_TIMEOUT = 5.0  # seconds
_POLL_TIMEOUT = 2.0       # shorter for hot-path polling

# Map well-known AppleScript / osascript error codes to actionable messages.
_ERROR_CODES = {
    -1728: ("NO_OBJECT", "The requested PowerPoint object wasn't found — is a presentation open?"),
    -1712: ("TIMED_OUT", "PowerPoint didn't respond in time — is it busy or unresponsive?"),
    -1743: ("NO_PERMISSION", "macOS denied access to PowerPoint. Grant Automation permission in System Settings → Privacy & Security → Automation."),
    -600:  ("NOT_RUNNING", "PowerPoint isn't running. Open your presentation and try again."),
    -609:  ("CONNECTION_LOST", "Lost connection to PowerPoint — did it quit?"),
    -10004: ("PRIVILEGE_VIOLATION", "PowerPoint refused the request — it may be in a modal state (e.g. a dialog is open)."),
    -10006: ("CANNOT_SET", "PowerPoint refused the property update — the view may not support it right now."),
}

_ERR_NUM_RE = re.compile(r"\((-?\d+)\)")


# ---------------------------------------------------------------------------
# Low-level runner
# ---------------------------------------------------------------------------

def _parse_error(stderr: str) -> tuple[str, str]:
    """Convert osascript stderr into a (code, friendly_message) pair."""
    stderr = stderr.strip() or "Unknown osascript error"
    match = _ERR_NUM_RE.search(stderr)
    if match:
        try:
            num = int(match.group(1))
        except ValueError:
            num = None
        if num is not None and num in _ERROR_CODES:
            code, friendly = _ERROR_CODES[num]
            return code, friendly
    return "OSASCRIPT_ERROR", stderr


def _run(
    script: str,
    *,
    timeout: float = _OSASCRIPT_TIMEOUT,
) -> PptResult:
    """Execute an AppleScript snippet and wrap the result.

    Returns:
        PptResult with ``ok=True`` and ``data={"stdout": ...}`` on success,
        or ``ok=False`` with a friendly ``message`` on failure.
    """
    if sys.platform != "darwin":
        return PptResult(False, "UNSUPPORTED_OS", "PowerPoint control requires macOS.")

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PptResult(False, "TIMED_OUT",
                         f"PowerPoint didn't respond within {timeout:.0f}s.")
    except FileNotFoundError:
        return PptResult(False, "NO_OSASCRIPT",
                         "`osascript` binary missing — not a supported macOS environment.")
    except OSError as exc:
        return PptResult(False, "OS_ERROR", f"Failed to invoke osascript: {exc}")

    if result.returncode != 0:
        code, friendly = _parse_error(result.stderr)
        return PptResult(False, code, friendly, {"stderr": result.stderr.strip()})

    return PptResult(True, "OK", "ok", {"stdout": result.stdout.strip()})


# ---------------------------------------------------------------------------
# Installation / runtime checks (cheap, cached where safe)
# ---------------------------------------------------------------------------

_cached_install_path: Optional[Path] = None


def find_install() -> Optional[Path]:
    """Return the filesystem path of the PowerPoint app bundle, or None."""
    global _cached_install_path
    if _cached_install_path is not None:
        return _cached_install_path
    for candidate in _APP_PATHS:
        if candidate.is_dir():
            _cached_install_path = candidate
            return candidate
    # Fall back to `mdfind` which catches custom install locations.
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["mdfind", f"kMDItemCFBundleIdentifier == '{_BUNDLE_ID}'"],
                capture_output=True, text=True, timeout=2,
            )
            for line in out.stdout.splitlines():
                path = Path(line.strip())
                if path.is_dir():
                    _cached_install_path = path
                    return path
        except (subprocess.SubprocessError, OSError):
            pass
    return None


def is_running() -> bool:
    """True if a PowerPoint process is currently running (any presentation)."""
    if sys.platform != "darwin":
        return False
    # Fast path — `pgrep` avoids launching AppleEvent subsystem.
    try:
        result = subprocess.run(
            ["pgrep", "-xi", "Microsoft PowerPoint"],
            capture_output=True, timeout=1,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def get_version() -> Optional[str]:
    """Return PowerPoint's `CFBundleShortVersionString` if installed."""
    path = find_install()
    if path is None:
        return None
    plist = path / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        result = subprocess.run(
            ["plutil", "-extract", "CFBundleShortVersionString", "raw",
             str(plist)],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def has_active_presentation() -> bool:
    """Best-effort: does PowerPoint have a presentation loaded right now?"""
    if not is_running():
        return False
    res = _run(
        'tell application "' + _APP_NAME + '" to count presentations',
        timeout=_POLL_TIMEOUT,
    )
    if not res.ok:
        return False
    try:
        return int(res.data["stdout"]) > 0
    except (KeyError, ValueError):
        return False


def is_slideshow_active() -> bool:
    """True if a regular slide-show window is open."""
    if not is_running():
        return False
    res = _run(
        'tell application "' + _APP_NAME + '" to count slide show windows',
        timeout=_POLL_TIMEOUT,
    )
    if not res.ok:
        return False
    try:
        return int(res.data["stdout"]) > 0
    except (KeyError, ValueError):
        return False


def _precondition() -> Optional[PptResult]:
    """Return a PptResult describing the blocker, or None if ready."""
    if sys.platform != "darwin":
        return PptResult(False, "UNSUPPORTED_OS",
                         "PowerPoint control requires macOS.")
    if find_install() is None:
        return PptResult(False, "NOT_INSTALLED",
                         "Microsoft PowerPoint is not installed on this machine.")
    if not is_running():
        return PptResult(False, "NOT_RUNNING",
                         "PowerPoint isn't running. Open your presentation and try again.")
    if not has_active_presentation():
        return PptResult(False, "NO_PRESENTATION",
                         "No presentation is open in PowerPoint.")
    return None


# ---------------------------------------------------------------------------
# Health check / diagnostics
# ---------------------------------------------------------------------------

def diagnose() -> Dict[str, Any]:
    """Return a structured diagnostic report.  Never raises."""
    os_ok = sys.platform == "darwin"

    def _safe(fn, default=None):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("diagnose helper raised: %s", exc)
            return default

    install_path = _safe(find_install) if os_ok else None
    running = _safe(is_running, False) if os_ok else False
    version = _safe(get_version) if install_path else None

    active_pres = False
    ssw_active = False
    active_name: Optional[str] = None
    if running:
        active_pres = _safe(has_active_presentation, False)
        ssw_active = _safe(is_slideshow_active, False)
        if active_pres:
            def _get_name():
                res = _run(
                    'tell application "' + _APP_NAME
                    + '" to get name of active presentation',
                    timeout=_POLL_TIMEOUT,
                )
                return res.data.get("stdout") if res.ok else None
            active_name = _safe(_get_name)

    # Assemble a single line summary for quick triage.
    if not os_ok:
        summary = "Unsupported OS (macOS required)."
    elif install_path is None:
        summary = "PowerPoint not installed."
    elif not running:
        summary = "PowerPoint not running."
    elif not active_pres:
        summary = "PowerPoint running, no presentation open."
    elif ssw_active:
        summary = "PowerPoint running a slideshow."
    else:
        summary = "PowerPoint running with a presentation (normal view)."

    return {
        "os_supported": os_ok,
        "os_platform": sys.platform,
        "powerpoint_installed": install_path is not None,
        "powerpoint_path": str(install_path) if install_path else None,
        "powerpoint_version": version,
        "powerpoint_running": running,
        "has_active_presentation": active_pres,
        "active_presentation_name": active_name,
        "slideshow_active": ssw_active,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

def get_current_slide_number() -> PptResult:
    """Return current 1-based slide number.  Works in slideshow or normal view."""
    blocker = _precondition()
    if blocker is not None:
        return blocker

    # Prefer slideshow window (presenter is actually *showing* that slide).
    if is_slideshow_active():
        res = _run(
            'tell application "' + _APP_NAME + '" to get slide number of '
            'slide of slide show view of slide show window 1',
            timeout=_POLL_TIMEOUT,
        )
        if res.ok:
            try:
                return PptResult(True, "OK", "ok",
                                 {"slide_number": int(res.data["stdout"]),
                                  "mode": "slideshow"})
            except (KeyError, ValueError):
                pass  # fall through

    # Normal view fallback.
    res = _run(
        'tell application "' + _APP_NAME + '" to get slide index of '
        'slide of view of active window',
        timeout=_POLL_TIMEOUT,
    )
    if res.ok:
        try:
            return PptResult(True, "OK", "ok",
                             {"slide_number": int(res.data["stdout"]),
                              "mode": "normal"})
        except (KeyError, ValueError):
            pass

    return PptResult(False, res.code if not res.ok else "UNKNOWN",
                     res.message if not res.ok else "Could not read current slide.")


def navigate(direction: str) -> PptResult:
    """Advance one slide forward or backward in the active view."""
    if direction not in ("next", "previous"):
        return PptResult(False, "BAD_ARGS",
                         f"direction must be 'next' or 'previous', got {direction!r}.")

    blocker = _precondition()
    if blocker is not None:
        return blocker

    ss_cmd = "go to next slide" if direction == "next" else "go to previous slide"
    script = (
        f'tell application "{_APP_NAME}"\n'
        '  if (count slide show windows) > 0 then\n'
        f'    {ss_cmd} slide show view of slide show window 1\n'
        '    return "slideshow"\n'
        '  else\n'
        '    set curIdx to slide index of slide of view of active window\n'
        '    set total to count of slides of active presentation\n'
    )
    if direction == "next":
        script += (
            '    if curIdx < total then\n'
            '      go to slide (view of active window) number (curIdx + 1)\n'
            '    end if\n'
        )
    else:
        script += (
            '    if curIdx > 1 then\n'
            '      go to slide (view of active window) number (curIdx - 1)\n'
            '    end if\n'
        )
    script += (
        '    return "normal"\n'
        '  end if\n'
        'end tell'
    )

    res = _run(script)
    if not res.ok:
        return res
    mode = res.data.get("stdout", "")
    return PptResult(True, "OK",
                     f"Moved {direction} in {mode} mode.",
                     {"mode": mode})


def goto(slide_number: int) -> PptResult:
    """Jump to a specific 1-based slide number."""
    if not isinstance(slide_number, int) or isinstance(slide_number, bool):
        return PptResult(False, "BAD_ARGS",
                         f"slide_number must be int, got {type(slide_number).__name__}.")
    if slide_number < 1:
        return PptResult(False, "BAD_ARGS",
                         f"slide_number must be ≥1, got {slide_number}.")

    blocker = _precondition()
    if blocker is not None:
        return blocker

    script = (
        f'tell application "{_APP_NAME}"\n'
        '  set total to count of slides of active presentation\n'
        f'  if {slide_number} > total then\n'
        f'    error "slide {slide_number} exceeds total (" & total & ")" number -1719\n'
        '  end if\n'
        '  if (count slide show windows) > 0 then\n'
        f'    go to slide slide {slide_number} of active presentation of '
        'slide show view of slide show window 1\n'
        '    return "slideshow"\n'
        '  else\n'
        f'    go to slide (view of active window) number {slide_number}\n'
        '    return "normal"\n'
        '  end if\n'
        'end tell'
    )
    res = _run(script)
    if not res.ok:
        return res
    return PptResult(True, "OK",
                     f"Jumped to slide {slide_number}.",
                     {"mode": res.data.get("stdout", ""),
                      "slide_number": slide_number})


def start_slideshow(
    from_slide: Optional[int] = None,
    *,
    verify: bool = True,
    verify_timeout_s: float = 1.0,
) -> PptResult:
    """Enter fullscreen slideshow mode from the active presentation.

    Args:
        from_slide: Optional 1-based slide number to start from. When
            provided, sets ``starting slide of slide show settings``
            BEFORE running the slideshow so PowerPoint opens on that
            slide instead of the default slide 1. This is the correct
            way to resume a slideshow on a specific slide — calling
            ``goto(N)`` before ``start_slideshow()`` does NOT work
            because ``run slide show (slide show settings …)`` uses
            whatever ``starting slide`` is configured (default 1),
            silently resetting the cursor.
        verify: When True (default), after ``run slide show`` returns ok
            we poll :func:`is_slideshow_active` until it returns True or
            ``verify_timeout_s`` elapses. If it never goes true, we treat
            the start as "the AppleScript lied" (macOS Spaces race — the
            show window was created and dismissed in the same frame)
            and return ``code="START_DISMISSED"``. See
            ``(internal postmortem 2026-05-09)`` §2.3
            for the incident that motivated this verification layer.
        verify_timeout_s: How long to wait for ``is_slideshow_active`` to
            flip to True before declaring a dismissal.

    Returns:
        PptResult. On success ``data["state"]`` is ``"started"``,
        ``"already_running"``, or ``"started_unverified"`` (caller passed
        ``verify=False``). On the macOS-Spaces-race dismissal path the
        result is ``ok=False, code="START_DISMISSED"`` so the caller can
        retry with a stronger activation; callers that don't care about
        the race can just pass ``verify=False``.
        When ``from_slide`` was honored, ``data["from_slide"]`` echoes
        the value for observability.
    """
    import time as _time
    blocker = _precondition()
    if blocker is not None:
        return blocker

    if from_slide is not None:
        if not isinstance(from_slide, int) or isinstance(from_slide, bool):
            return PptResult(
                False, "BAD_ARGS",
                f"from_slide must be int, got {type(from_slide).__name__}.",
            )
        if from_slide < 1:
            return PptResult(
                False, "BAD_ARGS",
                f"from_slide must be >= 1, got {from_slide}.",
            )

    # Build the "set starting slide" line only when the caller asked
    # for a specific slide. When from_slide is None we preserve the
    # original behaviour (PowerPoint uses whatever was last configured,
    # typically slide 1 on a fresh presentation).
    set_starting_line = ""
    if from_slide is not None:
        set_starting_line = (
            f'  set starting slide of slide show settings '
            f'of active presentation to {from_slide}\n'
        )

    script = (
        f'tell application "{_APP_NAME}"\n'
        '  activate\n'
        '  if (count slide show windows) > 0 then\n'
        '    return "already_running"\n'
        '  end if\n'
        f'{set_starting_line}'
        '  run slide show (slide show settings of active presentation)\n'
        '  return "started"\n'
        'end tell'
    )
    res = _run(script)
    if not res.ok:
        return res
    state = res.data.get("stdout", "")
    extra: Dict[str, Any] = {"state": state}
    if from_slide is not None:
        extra["from_slide"] = from_slide
    if state == "already_running":
        return PptResult(True, "ALREADY_RUNNING",
                         "Slideshow is already active.",
                         extra)

    # Verify-after-start: AppleScript's "started" is a promise, not an
    # observation. On macOS the slideshow window is created on its own
    # Space and can be dismissed by the window server in the same frame
    # if the caller wasn't truly frontmost. Poll until we see the window
    # or time out.
    if verify:
        deadline = _time.monotonic() + max(0.05, verify_timeout_s)
        observed = False
        attempts = 0
        while _time.monotonic() < deadline:
            attempts += 1
            if is_slideshow_active():
                observed = True
                break
            _time.sleep(0.1)
        extra["verify_attempts"] = attempts
        if not observed:
            extra["state"] = "dismissed"
            return PptResult(
                False, "START_DISMISSED",
                "PowerPoint accepted 'run slide show' but the slideshow "
                "window vanished (macOS Spaces race). Retry after a "
                "stronger app activation, or quit+relaunch PowerPoint.",
                extra,
            )
        return PptResult(True, "OK", "Slideshow started.", extra)

    extra["state"] = "started_unverified"
    return PptResult(True, "OK", "Slideshow started (unverified).", extra)


def exit_slideshow(
    *,
    wait_for_quiesce: bool = True,
    quiesce_timeout_s: float = 1.2,
) -> PptResult:
    """Leave fullscreen slideshow mode (returns to normal view).

    Args:
        wait_for_quiesce: When True (default), after issuing the exit
            AppleScript we poll :func:`is_slideshow_active` until it
            returns False or ``quiesce_timeout_s`` elapses. This closes
            a class of bugs where ``exit slide show`` returns instantly
            on macOS (it's async — the Space collapse animation is
            still running) and a follow-up operation (e.g. Chrome
            ``bring_to_front``) races against the window server mid-
            transition, which in turn corrupts PowerPoint's internal
            "is presenting" state for the rest of the process lifetime.
            See ``(internal postmortem 2026-05-09)``
            §2.3 for the original incident.
        quiesce_timeout_s: Max wall time to wait for full quiesce.
            Observed settled times on M-series Macs are 300–700 ms.

    If more than one slideshow window is open (the Presenter-View case,
    where PowerPoint creates both a presenter window AND a display
    window), we loop the exit up to 3 times so every slideshow window
    is closed — the original script only closed window 1.
    """
    # Note: we don't require has_active_presentation here — exit is a no-op if
    # not in slideshow mode, and the user may be quickly recovering from a bad
    # state.
    import time as _time

    if sys.platform != "darwin":
        return PptResult(False, "UNSUPPORTED_OS",
                         "PowerPoint control requires macOS.")
    if not is_running():
        return PptResult(False, "NOT_RUNNING",
                         "PowerPoint isn't running.")

    # One-shot exit of "slide show window 1" — if more windows exist
    # (Presenter View), the loop below will catch them.
    script = (
        f'tell application "{_APP_NAME}"\n'
        '  if (count slide show windows) = 0 then\n'
        '    return "not_running"\n'
        '  end if\n'
        '  exit slide show slide show view of slide show window 1\n'
        '  return "exited"\n'
        'end tell'
    )
    res = _run(script)
    if not res.ok:
        return res
    state = res.data.get("stdout", "")
    if state == "not_running":
        return PptResult(True, "NOT_IN_SLIDESHOW",
                         "Not currently in slideshow mode.",
                         {"state": state})

    # Close any remaining slideshow windows (Presenter View case).
    extra_closed = 0
    for _ in range(3):
        if not is_slideshow_active():
            break
        res2 = _run(script, timeout=_POLL_TIMEOUT)
        if not res2.ok:
            break
        if res2.data.get("stdout", "") == "not_running":
            break
        extra_closed += 1

    quiesced = True
    quiesce_ms: Optional[int] = None
    if wait_for_quiesce:
        t0 = _time.monotonic()
        deadline = t0 + max(0.05, quiesce_timeout_s)
        quiesced = False
        while _time.monotonic() < deadline:
            if not is_slideshow_active():
                quiesced = True
                break
            _time.sleep(0.08)
        quiesce_ms = int((_time.monotonic() - t0) * 1000)

    return PptResult(
        True, "OK", "Exited slideshow.",
        {
            "state": state,
            "extra_closed": extra_closed,
            "quiesced": quiesced,
            "quiesce_ms": quiesce_ms,
        },
    )


def activate_app(*, force: bool = True, timeout_s: float = 0.8) -> PptResult:
    """Bring PowerPoint to the foreground for real.

    The bare ``tell application "Microsoft PowerPoint" to activate`` is a
    WEAK activation on macOS — it hints to the window server but does
    not guarantee PowerPoint becomes the frontmost process when the
    caller is itself a background/GUIless process (e.g. a FastAPI
    worker). This matters because ``run slide show`` creates its window
    on a new Space and macOS will dismiss that Space within one frame
    if the requester isn't truly frontmost — which then corrupts
    PowerPoint's internal "is presenting" state for the rest of the
    process lifetime.

    When ``force=True`` (default) we additionally ask ``System Events``
    to set ``frontmost`` to True on the PowerPoint process and poll
    until it reports frontmost or ``timeout_s`` elapses. Requires macOS
    Automation permission for the terminal/host process — the same
    permission the rest of this module already needs.

    Returns PptResult; ``data`` includes ``frontmost`` (bool) and
    ``waited_ms``.
    """
    import time as _time

    if sys.platform != "darwin":
        return PptResult(False, "UNSUPPORTED_OS",
                         "PowerPoint control requires macOS.")
    if not is_running():
        return PptResult(False, "NOT_RUNNING",
                         "PowerPoint isn't running.")

    # Soft activate first — this is fast and always safe.
    soft = _run(
        'tell application "Microsoft PowerPoint" to activate',
        timeout=_POLL_TIMEOUT,
    )
    if not soft.ok:
        return soft

    if not force:
        return PptResult(True, "OK", "activated (soft)",
                         {"frontmost": None, "waited_ms": 0})

    # Hard activate via System Events. We poll rather than sleeping a
    # fixed time because observed frontmost latencies vary 50–400 ms
    # depending on what else is competing for focus.
    set_front = _run(
        'tell application "System Events" to tell process '
        '"Microsoft PowerPoint" to set frontmost to true',
        timeout=_POLL_TIMEOUT,
    )
    if not set_front.ok:
        # System Events can fail (e.g. missing Accessibility permission).
        # Fall back to the soft activate we already did.
        return PptResult(
            True, "OK", "activated (soft; System Events rejected)",
            {"frontmost": None, "waited_ms": 0,
             "system_events_error": set_front.code},
        )

    t0 = _time.monotonic()
    deadline = t0 + max(0.05, timeout_s)
    frontmost = False
    while _time.monotonic() < deadline:
        probe = _run(
            'tell application "System Events" to return name of '
            '(first application process whose frontmost is true)',
            timeout=_POLL_TIMEOUT,
        )
        if probe.ok and probe.data.get("stdout", "") == "Microsoft PowerPoint":
            frontmost = True
            break
        _time.sleep(0.06)
    waited_ms = int((_time.monotonic() - t0) * 1000)
    return PptResult(
        True, "OK",
        "activated (frontmost)" if frontmost
        else "activated (frontmost not confirmed)",
        {"frontmost": frontmost, "waited_ms": waited_ms},
    )



def quit_and_relaunch(
    *,
    quit_timeout_s: float = 5.0,
    reopen_timeout_s: float = 10.0,
) -> PptResult:
    """Nuclear option: quit PowerPoint and reopen the current presentation.

    Used by :func:`src.platform.window_manager.WindowManager.switch_to_slides`
    as a last-ditch recovery when :func:`start_slideshow` has returned
    ``START_DISMISSED`` twice in a row. At that point PowerPoint has
    entered the corrupted "is presenting" state where every future
    ``run slide show`` call creates a window that macOS dismisses within
    one frame, regardless of how aggressively the caller re-asserts
    frontmost. Empirically (see
    ``(internal postmortem 2026-05-09)`` §2.3), the
    only reliable fix is to terminate the PowerPoint process entirely
    and relaunch it with the original presentation file.

    Steps:
      1. Capture the active presentation's POSIX path via ``full name of
         active presentation``.
      2. Issue ``quit saving no`` via AppleScript; poll :func:`is_running`
         until False, falling back to ``pkill -TERM`` then ``pkill -KILL``
         if PowerPoint won't exit within ``quit_timeout_s``.
      3. Relaunch via ``open -a "Microsoft PowerPoint" <path>`` — this
         is the legitimate macOS activation primitive that launchd
         executes with foreground privileges, so the resulting PPT
         process actually becomes frontmost (something a bare
         ``tell app to activate`` from a background FastAPI worker
         cannot guarantee).
      4. Poll until :func:`is_running` AND :func:`has_active_presentation`
         are both True, or ``reopen_timeout_s`` elapses.
      5. Call :func:`activate_app` ``(force=True)`` so the follow-up
         ``start_slideshow`` has frontmost confirmed.

    Returns:
        On success, ``PptResult(ok=True, code="OK", …)`` with ``data``
        containing ``pres_path``, ``quit_ms``, ``reopen_ms``, ``frontmost``,
        and ``activate_waited_ms``.

        On failure, ``ok=False`` with ``code`` one of:

        - ``UNSUPPORTED_OS``     — not macOS.
        - ``NOT_RUNNING``        — PPT wasn't running; nothing to relaunch.
        - ``NO_PRESENTATION``    — couldn't read a presentation path to
          reopen, so we refuse to quit (no way to restore state).
        - ``QUIT_FAILED``        — AppleScript quit errored and ``pkill
          -KILL`` also failed to terminate the process.
        - ``REOPEN_FAILED``      — ``open -a`` exited non-zero.
        - ``REOPEN_TIMEOUT``     — PPT did not finish loading within
          ``reopen_timeout_s``.

    **Cost:** ~3-5 seconds of total silence (quit ≈ 1-2 s, launch ≈
    1-3 s, presentation load ≈ 1 s). This is visibly disruptive
    mid-demo — only invoke after a START_DISMISSED has already
    happened twice with stronger activation in between.
    """
    import time as _time

    if sys.platform != "darwin":
        return PptResult(False, "UNSUPPORTED_OS",
                         "PowerPoint control requires macOS.")
    if not is_running():
        return PptResult(False, "NOT_RUNNING",
                         "PowerPoint isn't running — nothing to relaunch.")

    # 1. Capture the active presentation's path BEFORE we quit. If the
    # capture fails we refuse to quit — there'd be no way to restore
    # the presenter to their demo afterwards.
    path_res = _run(
        f'tell application "{_APP_NAME}" to return full name of '
        f'active presentation',
        timeout=_POLL_TIMEOUT,
    )
    if not path_res.ok:
        return PptResult(
            False, "NO_PRESENTATION",
            "Couldn't read active presentation path — refusing to quit "
            "PowerPoint (would lose the demo with no way to reopen).",
            {"probe_error": path_res.code},
        )
    pres_path = path_res.data.get("stdout", "").strip()
    if not pres_path:
        return PptResult(
            False, "NO_PRESENTATION",
            "Active presentation reported an empty path — refusing to "
            "quit.",
        )

    # 2. Quit PowerPoint and wait for the process to actually exit.
    # AppleScript's `quit` returns before the process is gone, so poll.
    t_quit = _time.monotonic()
    quit_res = _run(
        f'tell application "{_APP_NAME}" to quit saving no',
        timeout=_OSASCRIPT_TIMEOUT,
    )
    if not quit_res.ok:
        logger.info(
            "quit_and_relaunch: `quit saving no` returned non-ok "
            "(%s: %s) — falling through to pkill",
            quit_res.code, quit_res.message,
        )

    deadline = t_quit + max(0.2, quit_timeout_s)
    while _time.monotonic() < deadline:
        if not is_running():
            break
        _time.sleep(0.15)
    quit_ms = int((_time.monotonic() - t_quit) * 1000)

    if is_running():
        # Last-ditch: SIGTERM then SIGKILL. `pkill -xi` matches the
        # full process name case-insensitively.
        logger.warning(
            "quit_and_relaunch: PPT still running after %d ms — escalating "
            "to pkill", quit_ms,
        )
        try:
            subprocess.run(
                ["pkill", "-TERM", "-xi", "Microsoft PowerPoint"],
                capture_output=True, timeout=2,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("quit_and_relaunch: pkill -TERM raised: %s", exc)
        _time.sleep(0.8)
        if is_running():
            try:
                subprocess.run(
                    ["pkill", "-KILL", "-xi", "Microsoft PowerPoint"],
                    capture_output=True, timeout=2,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                logger.debug(
                    "quit_and_relaunch: pkill -KILL raised: %s", exc,
                )
            _time.sleep(0.4)
        quit_ms = int((_time.monotonic() - t_quit) * 1000)

    if is_running():
        return PptResult(
            False, "QUIT_FAILED",
            "PowerPoint did not exit even after pkill -KILL.",
            {"pres_path": pres_path, "quit_ms": quit_ms,
             "quit_error": quit_res.code if not quit_res.ok else None},
        )

    # 3. Reopen the presentation via the legitimate macOS launch path.
    # launchd launches PowerPoint with foreground privileges, so the
    # resulting process actually becomes frontmost — something a bare
    # `tell app to activate` from a background worker can't guarantee.
    t_reopen = _time.monotonic()
    try:
        open_res = subprocess.run(
            ["open", "-a", _APP_NAME, pres_path],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return PptResult(
            False, "REOPEN_FAILED",
            "`open -a` timed out while relaunching PowerPoint.",
            {"pres_path": pres_path, "quit_ms": quit_ms},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return PptResult(
            False, "REOPEN_FAILED",
            f"`open -a` raised: {exc}",
            {"pres_path": pres_path, "quit_ms": quit_ms},
        )
    if open_res.returncode != 0:
        return PptResult(
            False, "REOPEN_FAILED",
            f"`open -a` exited {open_res.returncode}: "
            f"{(open_res.stderr or '').strip()}",
            {"pres_path": pres_path, "quit_ms": quit_ms},
        )

    # 4. Wait for PowerPoint to finish booting and load the presentation.
    deadline = t_reopen + max(0.5, reopen_timeout_s)
    loaded = False
    while _time.monotonic() < deadline:
        if is_running() and has_active_presentation():
            loaded = True
            break
        _time.sleep(0.2)
    reopen_ms = int((_time.monotonic() - t_reopen) * 1000)

    if not loaded:
        return PptResult(
            False, "REOPEN_TIMEOUT",
            f"PowerPoint did not reload {pres_path!r} within "
            f"{reopen_timeout_s:.1f}s.",
            {
                "pres_path": pres_path,
                "quit_ms": quit_ms,
                "reopen_ms": reopen_ms,
                "running": is_running(),
                "has_presentation": has_active_presentation(),
            },
        )

    # 5. Force frontmost so the caller's follow-up start_slideshow has
    # the best chance of sticking. `open -a` already did most of the
    # work here, but we belt-and-suspenders it with System Events.
    act = activate_app(force=True, timeout_s=1.0)

    return PptResult(
        True, "OK",
        f"PowerPoint relaunched with {Path(pres_path).name} "
        f"(quit {quit_ms} ms, reopen {reopen_ms} ms).",
        {
            "pres_path": pres_path,
            "quit_ms": quit_ms,
            "reopen_ms": reopen_ms,
            "frontmost": (bool(act.data.get("frontmost"))
                          if act.data else None),
            "activate_waited_ms": (act.data.get("waited_ms")
                                   if act.data else None),
        },
    )
