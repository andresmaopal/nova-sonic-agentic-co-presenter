"""navigate_slide tool — move through slides in PowerPoint.

Supported actions:
  * ``next`` / ``previous`` — step one slide (or ``count`` steps).
  * ``first`` — jump to slide 1.
  * ``last`` — jump to the final slide.

For multi-step or absolute jumps, the tool uses PowerPoint's ``goto(n)``
AppleScript path; single-step next/previous use the native ``go to next
slide`` / ``go to previous slide`` commands so animations and build steps
behave naturally.

After every successful navigation we resync SlideStore with PowerPoint's
actual current slide to prevent drift (e.g. if an animation step consumed
the ``next`` instead of advancing the slide).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.platform import powerpoint as ppt
from src.slide_store import SlideStore
from src.slide_sync import resync_slide_store

logger = logging.getLogger(__name__)

TOOL_NAME = "navigate_slide"
TOOL_DESCRIPTION = (
    "Navigate slides in the presentation. "
    "action='next' or 'previous' (optionally with count for 'advance 3 slides'); "
    "action='first' jumps to slide 1; action='last' jumps to the final slide."
)
TOOL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "One of 'next', 'previous', 'first', 'last'",
        },
        "count": {
            "type": "integer",
            "description": "Number of slides to advance for next/previous. Default 1.",
        },
    },
    "required": ["action"],
}

_VALID_ACTIONS = {"next", "previous", "first", "last"}


def _coerce_count(raw: Any) -> int:
    """Best-effort cast of a tool-provided count to a positive int (default 1)."""
    if raw is None:
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def navigate_slide(
    slide_store: SlideStore,
    tool_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle a ``navigate_slide`` tool call.

    Only updates SlideStore *after* PowerPoint confirms the move, so the
    logical state cannot drift from what the presenter sees on screen.
    """
    action = (tool_input.get("action") or "").lower()
    total = slide_store.total_slides

    if action not in _VALID_ACTIONS:
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": (
                f"Unknown action: {action!r}. "
                "Use 'next', 'previous', 'first', or 'last'."
            ),
        }

    # Sync store with PowerPoint before we plan the move, so count-based
    # navigation is relative to reality (not to a stale pointer).
    resync_slide_store(slide_store)
    current = slide_store.current_index  # 0-based, post-sync

    count = _coerce_count(tool_input.get("count"))

    # --- Compute target (0-based) ---
    if action == "first":
        target = 0
    elif action == "last":
        target = total - 1
    elif action == "next":
        target = min(current + count, total - 1)
    else:  # previous
        target = max(current - count, 0)

    # --- Bounds edge-cases ---
    if action in ("next", "previous") and target == current:
        edge_code = "AT_END" if action == "next" else "AT_START"
        edge_msg = (
            f"Already on the last slide ({total} of {total})."
            if action == "next"
            else "Already on the first slide."
        )
        return {
            "ok": False,
            "code": edge_code,
            "message": edge_msg,
            "slide_index": current + 1,
            "total_slides": total,
        }

    # --- Dispatch to PowerPoint ---
    if action in ("next", "previous") and count == 1:
        # Use native step so animations / build steps work naturally.
        result = ppt.navigate(action)
    else:
        # Multi-step or absolute jump → use goto for reliability.
        result = ppt.goto(target + 1)  # goto is 1-based

    if not result.ok:
        logger.warning("navigate_slide: %s — %s", result.code, result.message)
        return {
            "ok": False,
            "code": result.code,
            "message": result.message,
            "slide_index": current + 1,
            "total_slides": total,
        }

    # --- Update local pointer & resync against PowerPoint ---
    slide_store.set_current_index(target)
    # Best-effort: PowerPoint may have landed somewhere else (animation step
    # consumed the next, etc.) — one more read keeps us honest.
    resync_slide_store(slide_store)

    final_index = slide_store.current_index
    final_slide = final_index + 1
    logger.info(
        "Navigated %s (count=%d) → slide %d/%d (%s)",
        action, count, final_slide, total, result.data.get("mode", "?"),
    )

    return {
        "ok": True,
        "code": "OK",
        "message": f"On slide {final_slide} of {total}.",
        "slide_index": final_slide,
        "total_slides": total,
        "mode": result.data.get("mode"),
        "action": action,
        "count": count,
        "speech_hint": "Reply with ONE word only: 'ok' or 'vale' or 'listo' or 'perfecto' or 'hecho' or 'claro' — pick one at random, vary each time.",
    }
