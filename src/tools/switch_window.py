"""``switch_window`` — Session A tool handler.

Takes an argument ``target ∈ {visor, slides}`` and delegates to the
shared :class:`WindowManager` which coordinates PowerPoint (AppleScript)
and Chrome (Playwright-over-CDP, AppleScript fallback).

Per ``requirements.md R5`` and ``design.md § 7``, this is the
single voice-accessible surface for bidirectional window switching.
The tool is idempotent (switching to the already-foreground window is
a cheap no-op from the platform adapters' point of view) and never
raises — any failure is surfaced as ``{"ok": False, "code": ...}``
which Session A speaks briefly to the audience.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


_VALID_TARGETS = {"visor", "slides"}


async def switch_window_handler(
    *,
    tool_input: dict,
    app_state: Any,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Handle a ``switch_window`` tool call from Session A.

    Args:
        tool_input: ``{"target": "visor"|"slides",
                        "resume_fullscreen"?: bool}``
        app_state: The FastAPI ``app.state`` — provides
            ``window_manager`` (shared across all browser sessions).

    Returns:
        A JSON-safe dict suitable for a Nova Sonic ``toolResult``:

            {"ok": True, "target": "...", <WindowResult data>...}
            {"ok": False, "code": "BAD_ARGS", "message": "..."}
    """
    target_raw = tool_input.get("target") if isinstance(tool_input, dict) else None
    target = (target_raw or "").strip().lower()
    if target not in _VALID_TARGETS:
        logger.info("switch_window: bad target %r", target_raw)
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": (
                f"target must be one of {sorted(_VALID_TARGETS)}, "
                f"got {target_raw!r}"
            ),
        }

    resume_fullscreen = True
    if isinstance(tool_input, dict) and "resume_fullscreen" in tool_input:
        raw = tool_input.get("resume_fullscreen")
        # Tolerate both real booleans and the common "true"/"false" string form
        # that sometimes comes off voice agents.
        if isinstance(raw, bool):
            resume_fullscreen = raw
        elif isinstance(raw, str):
            resume_fullscreen = raw.strip().lower() not in ("false", "0", "no")

    wm = app_state.window_manager

    if target == "visor":
        result = await wm.switch_to_visor()
    else:  # "slides"
        # user_initiated=True is the signal the handoff-tool guard
        # reads via ``recently_swiped_to_slides()`` — it fires
        # whenever Nova invokes this tool on behalf of a presenter
        # utterance ("switch to PPT" / "back to slides"). Without
        # this flag the guard can't tell a presenter-driven swipe
        # from an internal rollback path. See the 2026-05-12
        # unprompted-visor-swipe postmortem.
        result = await wm.switch_to_slides(
            resume_fullscreen=resume_fullscreen,
            user_initiated=True,
        )

    logger.info(
        "switch_window target=%s ok=%s code=%s",
        target, result.ok, result.code,
    )

    # Record the target so downstream Session A tools (notably
    # analyze_slide) can distinguish "presenter is on slides" from
    # "presenter is on the visor looking at a fresh report". Only
    # flip the flag on success — a failed swipe didn't actually
    # change what's on screen.
    if result.ok:
        app_state.last_foreground_target = target

    out = result.to_dict()
    # Stable top-level field for the voice model to echo ("ok, slides").
    out["target"] = target
    # Reinforce the "one-word acknowledgement" rule — the model sees this
    # in the tool result right before generating its next utterance.
    out["speech_hint"] = "Reply with ONE word only: 'ok' or 'vale' or 'listo' or 'perfecto' or 'hecho' or 'claro' — pick one at random, vary each time."
    return out
