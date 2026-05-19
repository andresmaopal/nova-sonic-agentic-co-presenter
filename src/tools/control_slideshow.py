"""control_slideshow tool — start or exit PowerPoint fullscreen slideshow.

Space-transfer problem
----------------------
Starting a slideshow while the user is 1, 2, or N Spaces away from PowerPoint
is invisible without a post-start focus step: macOS fires the slideshow on
PowerPoint's Space but doesn't move the user's view. The previous
implementation swiped LEFT once, which only worked when the user was
exactly one Space to the right of PowerPoint. If the user was on Space 3
(voice UI) and PowerPoint was on Space 1, swiping left took them to
Space 2 (visor), NOT to the slideshow — the user then had to swipe
manually, which made Nova look broken.

Solution (Space-count agnostic): after ``start`` succeeds, call
``ppt.activate_app(force=True)``. That function uses System Events to set
``frontmost = true`` on the PowerPoint process, which triggers macOS's
native "follow-the-app-to-its-Space" behavior. It doesn't matter how
many Spaces apart the user and PowerPoint are, and it doesn't matter
whether they're laid out left/right/up/down: macOS jumps the user's
view to wherever PowerPoint is.

``activate_app`` polls System Events until PowerPoint reports frontmost
(or a short timeout), so we know the Space transfer landed before we
return the tool result. If System Events is denied (e.g. no
Accessibility permission), the soft activation alone is sometimes
enough — and the pre-existing spaces-swipe left is still used as a
last-resort fallback so we don't regress from the previous behavior.

This module's auto-focus is ONLY performed when ``NOVA_USE_SPACES_SWIPE=1``
(the spaces-aware mode the start script enables). In the default
(exit/restart) window-switch mode, the window manager's own flow
handles focus; injecting another activate there would fight it.

Exit doesn't auto-focus — the presenter may want to edit slides after
exiting fullscreen, so moving the view would be intrusive.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from src.platform import powerpoint as ppt
from src.platform import spaces

logger = logging.getLogger(__name__)

TOOL_NAME = "control_slideshow"
TOOL_DESCRIPTION = (
    "Start or exit PowerPoint's fullscreen slideshow mode. "
    "Use when the presenter says 'start slideshow', 'go fullscreen', "
    "'begin presentation', 'exit slideshow', or 'stop presenting'."
)
TOOL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "Either 'start' (enter fullscreen) or 'exit' (leave fullscreen)",
        },
    },
    "required": ["action"],
}


def _spaces_swipe_enabled() -> bool:
    """True when NOVA_USE_SPACES_SWIPE is set to a truthy value.

    Kept as a function (not a module-level constant) so tests can
    monkey-patch ``os.environ`` and the control tool picks it up on the
    next call — mirrors how ``api_server.py`` reads the same var.
    """
    return os.environ.get("NOVA_USE_SPACES_SWIPE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _bring_user_to_powerpoint_space(payload: Dict[str, Any]) -> None:
    """Best-effort: move the user's visible Space to PowerPoint's.

    Tries in order:
      1. ``ppt.activate_app(force=True)`` — uses System Events to set
         PowerPoint as the frontmost process. macOS transfers the user
         to PowerPoint's Space automatically, regardless of Space-layout
         distance. This is the primary, Space-count-agnostic path.
      2. If activate returned ok=False (System Events / Automation
         denied), fall back to ``spaces.swipe("left")``. This only works
         when PowerPoint is exactly one Space to the left, but it's
         better than nothing.

    Results are annotated onto ``payload`` for observability:
      - ``focus_mode``:    "activate" | "swipe_fallback" | "none"
      - ``focus_ok``:      bool
      - ``focus_code``:    str
      - ``focus_waited_ms``: int (only for activate path)
    """
    # Primary path — ppt.activate_app with force=True.
    try:
        result = ppt.activate_app(force=True)
        payload["focus_mode"] = "activate"
        payload["focus_ok"] = bool(result.ok)
        payload["focus_code"] = result.code
        payload["focus_waited_ms"] = result.data.get("waited_ms", 0)
        if result.ok:
            frontmost = result.data.get("frontmost")
            logger.info(
                "control_slideshow start: activate_app ok "
                "(frontmost=%s, waited=%sms) — macOS should have "
                "switched Spaces to PPT",
                frontmost, result.data.get("waited_ms", 0),
            )
            return
        logger.info(
            "control_slideshow start: activate_app FAILED "
            "(%s: %s) — falling back to spaces.swipe",
            result.code, result.message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "control_slideshow start: activate_app raised "
            "(non-fatal): %s — falling back to spaces.swipe",
            exc,
        )
        payload["focus_mode"] = "activate"
        payload["focus_ok"] = False
        payload["focus_code"] = "EXCEPTION"

    # Fallback path — single swipe LEFT. Same limitation as before
    # (only covers the 1-space-away case) but it's a safety net.
    try:
        swipe_result = spaces.swipe("left")
        payload["focus_mode"] = "swipe_fallback"
        payload["focus_ok"] = bool(swipe_result.ok)
        payload["focus_code"] = swipe_result.code
        if swipe_result.ok:
            logger.info(
                "control_slideshow start: swipe_fallback ok "
                "(1-space-left only — may not reach PPT if farther away)",
            )
        else:
            logger.info(
                "control_slideshow start: swipe_fallback FAILED "
                "(%s: %s) — user may need to swipe manually",
                swipe_result.code, swipe_result.message,
            )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "control_slideshow start: swipe_fallback raised "
            "(non-fatal): %s", exc,
        )
        payload["focus_ok"] = False
        payload["focus_code"] = "EXCEPTION"


def _reopen_last_pptx(path: str, timeout_s: float = 6.0) -> bool:
    """Reopen a .pptx path in PowerPoint and wait for it to become active.

    Self-healing for the NO_PRESENTATION scenario: the presenter closed
    the deck between voice sessions (e.g. stray Cmd+W after exiting
    slideshow with Esc) and then asked Nova to start the presentation.
    The browser UI tells /preprocess which file it's about to demo, so
    api_server stashes it on ``app.state.last_pptx_path``. When
    ``start_slideshow`` returns NO_PRESENTATION we try to reopen that
    file and retry once — this restores the demo without the presenter
    having to intervene.

    Returns True if PowerPoint has an active presentation after reopen
    (whether we needed to reopen or it got loaded concurrently), False
    otherwise. Best-effort — no exceptions raised.
    """
    import subprocess
    import time

    if not path or not os.path.exists(path):
        logger.info(
            "control_slideshow reopen: path missing or not on disk (%r) — "
            "cannot self-heal", path,
        )
        return False

    try:
        subprocess.run(
            ["open", "-a", "Microsoft PowerPoint", path],
            check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("control_slideshow reopen: `open -a` failed: %s", exc)
        return False

    # Poll until PowerPoint reports an active presentation again.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if ppt.has_active_presentation():
                logger.info(
                    "control_slideshow reopen: PPT reports active "
                    "presentation after %.1fs — retrying start", 
                    timeout_s - (deadline - time.time()),
                )
                return True
        except Exception:  # noqa: BLE001 - diagnostic only
            pass
        time.sleep(0.25)

    logger.info(
        "control_slideshow reopen: timed out after %.1fs waiting for "
        "active presentation", timeout_s,
    )
    return False


def control_slideshow(
    tool_input: Dict[str, Any],
    app_state: Any = None,
) -> Dict[str, Any]:
    """Handle a control_slideshow tool call.

    On ``action='start'`` success in spaces-swipe mode, also bring the
    user's visible Space to PowerPoint's (via activate_app → System
    Events frontmost). Works regardless of how many Spaces separate
    the user from PowerPoint.

    Self-healing: if ``action='start'`` returns NO_PRESENTATION and
    ``app_state`` has a ``last_pptx_path``, attempt to reopen that file
    and retry once. See :func:`_reopen_last_pptx`.
    """
    action = (tool_input.get("action") or "").lower()

    if action not in ("start", "exit"):
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": f"Unknown action: {action!r}. Use 'start' or 'exit'.",
        }

    result = ppt.start_slideshow() if action == "start" else ppt.exit_slideshow()

    # Self-healing: if start failed because no presentation is open and
    # we have a cached path, reopen it and retry once. Strictly
    # additive — only runs on the NO_PRESENTATION error path.
    if (action == "start"
            and not result.ok
            and result.code == "NO_PRESENTATION"
            and app_state is not None
            and getattr(app_state, "last_pptx_path", None)):
        last_path = app_state.last_pptx_path
        logger.info(
            "control_slideshow start: NO_PRESENTATION — attempting "
            "self-heal by reopening %s", last_path,
        )
        if _reopen_last_pptx(last_path):
            result = ppt.start_slideshow()
            if result.ok:
                logger.info(
                    "control_slideshow start: self-heal SUCCEEDED — "
                    "slideshow started after reopening deck",
                )

    if not result.ok:
        logger.warning("control_slideshow %s: %s — %s",
                       action, result.code, result.message)
    else:
        logger.info("control_slideshow %s: %s", action, result.message)

    payload = result.to_dict()
    payload["action"] = action

    # Best-effort Space transfer to PowerPoint after a successful start.
    # See module docstring for why this is activate-based, not swipe-based.
    if action == "start" and result.ok and _spaces_swipe_enabled():
        _bring_user_to_powerpoint_space(payload)

    payload["speech_hint"] = "Reply with ONE word only: 'ok' or 'vale' or 'listo' or 'perfecto' or 'hecho' or 'claro' — pick one at random, vary each time."
    return payload
