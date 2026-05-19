"""WindowManager — unified two-way window switcher for the co-presenter.

The voice layer's ``switch_window`` tool and the ``handoff_to_specialist``
tool both need to flip the foreground between PowerPoint (slides) and
the Chrome visor tab. This module is the single place where that flip
is coordinated.

Key behaviors:

- **PPT fullscreen is remembered.** When the presenter asks for a
  financial analysis while in fullscreen slideshow, we exit slideshow
  (otherwise Chrome can't come on top on macOS Spaces) BUT record that
  we did. On the return trip, ``switch_to_slides(resume_fullscreen=True)``
  re-enters slideshow automatically.
- **Slide checkpoint is persisted.** Before exiting slideshow, the
  current 1-based slide number is captured via
  ``get_current_slide_number()`` (works in either mode, ~10 ms
  AppleScript) and written to ``.slide_checkpoint.json`` by
  :mod:`src.state.slide_checkpoint`. On the return trip,
  ``switch_to_slides`` reads the checkpoint and either:
  - passes ``from_slide=N`` to ``start_slideshow`` (fullscreen path),
    so the slideshow opens on slide N instead of PowerPoint's default
    slide 1, or
  - calls ``goto(N)`` (normal-view path), so the presenter's cursor
    lands on the same slide they were looking at before the handoff.
  The keyboard hook writes to the same checkpoint continuously during
  slideshow playback, so even if ``switch_to_visor`` is never called
  the most recent slide is still remembered across server restarts.
- **Visor window is maximized.** After ``bring_tab_to_front`` the
  adapter asks Chrome via CDP to ``maximize`` the containing window
  so the report fills the available screen. Best-effort; any CDP
  failure is logged and never fails the switch.
- **Visor tab is focused, not just the Chrome window.** ``ChromeAdapter``
  does the URL-prefix-match + ``page.bring_to_front()`` + activate dance.
- **Idempotent.** Switching to the already-active window is a cheap no-op.
- **Never raises.** Every method returns a ``WindowResult``-shaped
  payload safe for JSON serialization in tool results.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.platform import powerpoint as ppt
from src.platform import spaces
from src.platform.chrome import ChromeAdapter, ChromeResult
from src.state import slide_checkpoint


logger = logging.getLogger(__name__)

# Canonical URL prefix for the visor tab.
VISOR_URL_PREFIX = "http://localhost:3333"


# ─────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowResult:
    """Uniform return type for :class:`WindowManager` operations."""

    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message,
                **self.data}


# ─────────────────────────────────────────────────────────────
# WindowManager
# ─────────────────────────────────────────────────────────────


class WindowManager:
    """Two-way switcher between PowerPoint and Chrome's visor tab."""

    def __init__(
        self,
        *,
        chrome: ChromeAdapter,
        powerpoint_module: Any = ppt,
        visor_url_prefix: str = VISOR_URL_PREFIX,
        checkpoint_module: Any = slide_checkpoint,
        use_spaces_swipe: bool = False,
        spaces_module: Any = spaces,
    ) -> None:
        self._chrome = chrome
        self._ppt = powerpoint_module
        self._visor_prefix = visor_url_prefix
        # Injected so tests can swap in a temp-file-backed implementation
        # without monkey-patching. Defaults to the real module.
        self._checkpoint = checkpoint_module

        # When True, switch_to_visor/switch_to_slides short-circuit to
        # a Spaces swipe and skip ALL PPT/Chrome coordination. This is
        # the dual-fullscreen architecture (see
        # ``(internal postmortem 2026-05-09)``
        # follow-up): slideshow runs once per session on its own Space,
        # visor tab runs fullscreen on its own Space, and we move
        # between them with Ctrl+←/→ instead of exiting+restarting
        # slideshow on every handoff. Flag-gated behind
        # ``NOVA_USE_SPACES_SWIPE=1`` in the env so the default path
        # stays untouched until we've validated the new one on real
        # hardware.
        self._use_spaces_swipe = bool(use_spaces_swipe)
        self._spaces = spaces_module

        # True if PowerPoint was in fullscreen slideshow when we last
        # switched to the visor. Used by switch_to_slides to decide
        # whether to auto-re-enter fullscreen. Unused in spaces-swipe
        # mode (slideshow never exits, so there's nothing to restore).
        self._was_fullscreen_before_visor: bool = False

        # Monotonic timestamp of the last *user-initiated* switch to
        # slides (i.e., the user explicitly said "switch to PPT" /
        # "back to slides" and Nova called ``switch_window(target=
        # 'slides')``). Used by ``handoff_to_specialist`` to detect the
        # "Nova fires a stale SPECULATIVE handoff right after the user
        # explicitly asked for slides" failure mode — see the
        # 2026-05-12 postmortem for the incident chain. ``None`` until
        # the first user switch fires in this process.
        self._last_user_switch_to_slides_at: float | None = None

    @property
    def was_fullscreen_before_visor(self) -> bool:
        """Read-only view of the remembered fullscreen state — useful for tests
        and for :meth:`diagnose`."""
        return self._was_fullscreen_before_visor

    # ─── user-initiated slides-swipe recency (2026-05-12) ─────

    def recently_swiped_to_slides(self, *, within_s: float) -> bool:
        """True iff a *user-initiated* ``switch_to_slides`` call happened
        within the last ``within_s`` seconds of monotonic wall time.

        Intended consumer: ``src/tools/handoff_to_specialist.py`` uses
        this to decide whether the automatic ``switch_to_visor`` at
        the start of a handoff should be suppressed — if the presenter
        just said "switch to PPT" and Nova's LLM fires a
        (possibly stale / SPECULATIVE / mis-heard) handoff ~100-300 ms
        later, yanking them back to the visor feels "unprompted".

        Safe to call at any time; returns ``False`` before the first
        user switch in this process.
        """
        if self._last_user_switch_to_slides_at is None:
            return False
        return (
            time.monotonic() - self._last_user_switch_to_slides_at
        ) <= within_s

    # ─── visor ────────────────────────────────────────────────

    async def switch_to_visor(self) -> WindowResult:
        """Raise the Chrome window and activate the visor tab.

        In spaces-swipe mode (``use_spaces_swipe=True`` at construction),
        this short-circuits to a single ``Ctrl+→`` keystroke and skips
        the PPT/Chrome coordination entirely. Assumes the caller has
        pre-arranged Spaces so the visor sits one Space to the right
        of the currently-visible Space.

        In the default mode, the full choreography runs:

        1. Capture the current 1-based slide number (works in either
           normal or slideshow mode via ``get_current_slide_number``)
           and persist it to ``.slide_checkpoint.json``. This runs
           BEFORE exiting slideshow so the reading reflects what the
           audience was actually looking at.
        2. If PowerPoint is in fullscreen slideshow, exit it
           (remembered so ``switch_to_slides`` can restore).
        3. Bring the visor tab to the front via CDP/AppleScript.
        4. Best-effort ask Chrome to maximize the containing window so
           the report fills the screen (distinct from native
           fullscreen — no separate macOS Space, no return-trip race).
        """
        # Spaces-swipe mode — swipe right, then explicitly sync focus.
        #
        # Why focus-sync is necessary even though we "just swiped":
        # macOS moves the visible Space immediately but does NOT always
        # transfer keyboard focus to the frontmost window of that
        # Space. Observed bugs in PR2a manual testing (2026-05-10):
        #   (1) Chrome window on the visor Space showed the voice-UI
        #       tab instead of the visor (because both tabs live in
        #       the same Chrome window until PR2b.2 splits them).
        #   (2) After the swipe, keyboard arrows didn't navigate the
        #       visor's two-slide report — the window needed a click
        #       first.
        # ``chrome.bring_tab_to_front(visor_prefix)`` fixes both at
        # once: it raises the Chrome app (transferring focus) AND
        # activates the tab whose URL matches the visor prefix (so
        # the right tab is on screen). Best-effort — any CDP/
        # AppleScript failure degrades silently; the user is still on
        # the right Space, which is most of the win.
        if self._use_spaces_swipe:
            swipe_result = self._spaces.swipe("right")
            focus_ok = False
            focus_code: str | None = None
            # Only attempt focus sync if the swipe succeeded. If the
            # swipe failed (Accessibility denied, osascript timeout,
            # etc.) we're not on the target Space — raising Chrome
            # could auto-trigger macOS's "follow the app to its Space"
            # behavior, making the confusion worse. The user needs
            # to resolve the swipe failure first.
            if swipe_result.ok:
                try:
                    focus_result = await self._chrome.bring_tab_to_front(
                        self._visor_prefix,
                    )
                    focus_ok = bool(focus_result.ok)
                    focus_code = focus_result.code
                    if not focus_result.ok:
                        logger.info(
                            "switch_to_visor: spaces-mode focus sync non-ok "
                            "(%s: %s) — continuing",
                            focus_result.code, focus_result.message,
                        )
                except Exception as exc:   # noqa: BLE001
                    logger.info(
                        "switch_to_visor: spaces-mode focus sync raised "
                        "(non-fatal): %s", exc,
                    )
                    focus_code = "EXCEPTION"
            logger.info(
                "switch_to_visor: spaces-swipe mode target=visor "
                "swipe_ok=%s focus_ok=%s code=%s",
                swipe_result.ok, focus_ok, swipe_result.code,
            )
            return WindowResult(
                ok=swipe_result.ok,
                code=swipe_result.code,
                message=swipe_result.message,
                data={
                    **swipe_result.data,
                    "target": "visor",
                    "via": "spaces_swipe",
                    "focus_ok": focus_ok,
                    "focus_code": focus_code,
                },
            )

        # 1. Checkpoint the slide number BEFORE any state change.
        captured_slide: int | None = None
        try:
            slide_res = self._ppt.get_current_slide_number()
            if slide_res.ok:
                num = slide_res.data.get("slide_number")
                if isinstance(num, int) and num >= 1:
                    captured_slide = num
                    saved = self._checkpoint.save(num)
                    if not saved:
                        logger.info(
                            "switch_to_visor: checkpoint save returned False "
                            "(slide=%d) — degrading to in-memory only", num,
                        )
        except Exception as exc:   # noqa: BLE001
            logger.debug(
                "switch_to_visor: get_current_slide_number failed "
                "(non-fatal, no checkpoint saved): %s", exc,
            )

        # 2. Exit slideshow if active.
        exited_fullscreen = False
        try:
            if self._ppt.is_slideshow_active():
                self._was_fullscreen_before_visor = True
                exit_result = self._ppt.exit_slideshow()
                exited_fullscreen = bool(exit_result.ok)
                if not exit_result.ok:
                    # Don't fail the whole switch — log and continue.
                    logger.info(
                        "switch_to_visor: exit_slideshow failed (%s: %s)",
                        exit_result.code, exit_result.message,
                    )
        except Exception as exc:   # noqa: BLE001
            logger.debug("switch_to_visor: PPT probe failed: %s", exc)

        # 3. Bring the visor tab to the front.
        chrome_result = await self._chrome.bring_tab_to_front(self._visor_prefix)

        # 4. Ask Chrome to maximize the containing window. Any failure
        # degrades silently — tab is already front, which is the
        # majority of the user-visible effect.
        maximized = False
        try:
            maximize_result = await self._chrome.maximize_window_for_tab(
                self._visor_prefix,
            )
            maximized = bool(maximize_result.ok)
            if not maximize_result.ok:
                logger.info(
                    "switch_to_visor: maximize_window_for_tab non-ok "
                    "(%s: %s) — continuing",
                    maximize_result.code, maximize_result.message,
                )
        except AttributeError:
            # Adapter doesn't implement maximize (e.g. an older test
            # fixture) — skip silently.
            logger.debug("switch_to_visor: chrome adapter has no maximize method")
        except Exception as exc:   # noqa: BLE001
            logger.info(
                "switch_to_visor: maximize_window_for_tab raised (non-fatal): %s",
                exc,
            )

        logger.info(
            "switch_to_visor: done target=visor checkpoint=%s "
            "exited_fullscreen=%s maximized=%s chrome_ok=%s",
            captured_slide, exited_fullscreen, maximized, chrome_result.ok,
        )

        return WindowResult(
            ok=chrome_result.ok,
            code=chrome_result.code,
            message=chrome_result.message,
            data={
                **chrome_result.data,
                "exited_fullscreen": exited_fullscreen,
                "slide_checkpoint": captured_slide,
                "maximized": maximized,
                "target": "visor",
            },
        )

    # ─── slides ───────────────────────────────────────────────

    async def switch_to_slides(
        self, *, resume_fullscreen: bool = True, user_initiated: bool = False,
    ) -> WindowResult:
        """Raise PowerPoint to the foreground and restore the slide the
        presenter was on before the handoff.

        Args:
            resume_fullscreen: When True and the caller exited a
                slideshow on the way to the visor, re-enter fullscreen
                slideshow after the checkpoint load.
            user_initiated: True when the call comes from the voice
                ``switch_window(target='slides')`` tool (i.e., the
                presenter explicitly asked). False (default) for
                internal callers such as ``handoff_to_specialist``
                rollback paths. Only user-initiated calls update
                ``_last_user_switch_to_slides_at`` — that timestamp is
                read by the handoff tool's "unprompted swipe guard"
                (see ``recently_swiped_to_slides``).

        In spaces-swipe mode (``use_spaces_swipe=True`` at construction),
        this short-circuits to a single ``Ctrl+←`` keystroke and skips
        the PPT activation + slideshow restart choreography entirely.
        Slideshow never exited, so there's nothing to restart; the
        presenter lands on the same slide they left.

        In the default mode, the full choreography runs:

        1. Activate PowerPoint (AppleScript).
        2. Load the slide-number checkpoint.
        3. If ``resume_fullscreen`` AND we exited a slideshow on the
           way to the visor: call ``start_slideshow(from_slide=N)``.
           Using ``from_slide`` is required — ``start_slideshow()``
           without it opens on PowerPoint's default starting slide
           (usually slide 1), silently losing the checkpoint.
        4. If we're NOT resuming fullscreen but the checkpoint exists:
           call ``goto(N)`` so normal view lands on the right slide.
        5. Clear the ``_was_fullscreen_before_visor`` flag — the next
           switch_to_visor will capture fresh state. We intentionally
           leave the persisted checkpoint alone so it remains valid
           for the next round-trip.
        """
        # Stamp the user-initiated timestamp FIRST so even an early
        # exception path doesn't leave the guard blind to an explicit
        # request. Idempotent / monotonic — repeated calls just push
        # the stamp forward.
        if user_initiated:
            self._last_user_switch_to_slides_at = time.monotonic()
        # Spaces-swipe mode — swipe left, then explicitly sync focus to
        # PowerPoint. The ``resume_fullscreen`` kwarg is irrelevant
        # here (slideshow never exited) but we accept it for API
        # compatibility with the default path.
        #
        # Why focus-sync (same reason as switch_to_visor): macOS moves
        # the Space but not always keyboard focus. Without this, the
        # presenter lands on the PPT Space but can't advance slides
        # with the arrow keys until they click the window. We use
        # ``activate_app(force=True)`` which does both the AppleScript
        # `activate` (window raise) AND the System Events `set
        # frontmost to true` (guarantees PPT is the active process).
        # Short timeout because PPT is already fullscreen on this
        # Space — focus should flip within 50-150 ms.
        if self._use_spaces_swipe:
            swipe_result = self._spaces.swipe("left")
            focus_ok = False
            focus_code: str | None = None
            # Only attempt focus sync if the swipe succeeded; see
            # ``switch_to_visor`` for the same rationale.
            if swipe_result.ok:
                try:
                    focus_result = self._ppt.activate_app(
                        force=True, timeout_s=0.5,
                    )
                    focus_ok = bool(focus_result.ok)
                    focus_code = focus_result.code
                    if not focus_result.ok:
                        logger.info(
                            "switch_to_slides: spaces-mode focus sync non-ok "
                            "(%s: %s) — continuing",
                            focus_result.code, focus_result.message,
                        )
                except Exception as exc:   # noqa: BLE001
                    logger.info(
                        "switch_to_slides: spaces-mode focus sync raised "
                        "(non-fatal): %s", exc,
                    )
                    focus_code = "EXCEPTION"
            logger.info(
                "switch_to_slides: spaces-swipe mode target=slides "
                "swipe_ok=%s focus_ok=%s code=%s",
                swipe_result.ok, focus_ok, swipe_result.code,
            )
            return WindowResult(
                ok=swipe_result.ok,
                code=swipe_result.code,
                message=swipe_result.message,
                data={
                    **swipe_result.data,
                    "target": "slides",
                    "via": "spaces_swipe",
                    "focus_ok": focus_ok,
                    "focus_code": focus_code,
                },
            )

        # 1. Activate PowerPoint — STRONG activation via System Events.
        # The bare `tell app to activate` is a WEAK hint; when the
        # calling process is a background FastAPI worker, macOS often
        # ignores it and PowerPoint is not truly frontmost. That in
        # turn causes `run slide show` to create a window that macOS
        # dismisses one frame later (the slideshow Space can't stabilise
        # because the requester isn't foreground), which then corrupts
        # PowerPoint's internal "is presenting" state for the rest of
        # the process lifetime. See
        # ``(internal postmortem 2026-05-09)`` §2.3.
        activate_result = self._ppt.activate_app(force=True)
        if not activate_result.ok:
            return WindowResult(
                ok=False,
                code=activate_result.code,
                message=activate_result.message,
                data={
                    "target": "slides",
                    "resumed_fullscreen": False,
                    "navigated_to_slide": None,
                },
            )
        activation_frontmost = bool(
            activate_result.data.get("frontmost")
        ) if activate_result.data else False

        # 2. Load the persisted checkpoint.
        checkpoint: int | None = None
        try:
            checkpoint = self._checkpoint.load()
        except Exception as exc:   # noqa: BLE001
            logger.debug(
                "switch_to_slides: checkpoint.load raised (non-fatal): %s", exc,
            )

        was_fullscreen = self._was_fullscreen_before_visor
        navigated_to: int | None = None
        resumed = False
        # Recovery telemetry (populated when we enter the fullscreen-
        # resume branch and hit the double-dismissal escalation).
        start_attempts = 0
        relaunch_attempted = False
        relaunch_ok: bool | None = None
        relaunch_data: dict[str, Any] = {}

        if resume_fullscreen and was_fullscreen:
            # Atomic: set starting slide + run slideshow in one
            # AppleScript call. When checkpoint is None (rare — no
            # hook updates yet and switch_to_visor's capture failed),
            # fall through to plain start_slideshow so we at least
            # resume slideshow mode, even if on PowerPoint's default
            # starting slide.
            #
            # start_slideshow(verify=True) polls is_slideshow_active()
            # for ~1 s after `run slide show` returns. If the window
            # vanished (macOS Spaces race) we get code=START_DISMISSED
            # and can retry once after re-asserting PPT frontmost.
            # A second dismissal means PowerPoint has entered the
            # corrupted "is presenting" state that only a quit/
            # relaunch fixes — we surface that clearly.
            import time as _time

            start_result = self._ppt.start_slideshow(
                from_slide=checkpoint, verify=True,
            )
            start_attempts = 1

            # Attempt 2: stronger activation + retry once.
            if (not start_result.ok
                    and start_result.code == "START_DISMISSED"):
                logger.info(
                    "switch_to_slides: start_slideshow dismissed — "
                    "re-activating PPT and retrying once (frontmost=%s)",
                    activation_frontmost,
                )
                try:
                    self._ppt.activate_app(force=True, timeout_s=0.8)
                except Exception as exc:   # noqa: BLE001
                    logger.debug(
                        "switch_to_slides: retry activate raised: %s", exc,
                    )
                _time.sleep(0.25)
                start_result = self._ppt.start_slideshow(
                    from_slide=checkpoint, verify=True,
                )
                start_attempts = 2

            # Attempt 3 (NUCLEAR): quit PowerPoint and relaunch it with
            # the same presentation file, then try one more time. This
            # is the ONLY reliable recovery when PPT has entered the
            # corrupted "is presenting" process state — empirically,
            # stronger activation alone is not enough once the state
            # is corrupted. See
            # ``(internal postmortem 2026-05-09)`` §2.3.
            # Cost: ~3-5 s of silence mid-demo. We only pay it when the
            # alternative is definitely staying stuck.
            if (not start_result.ok
                    and start_result.code == "START_DISMISSED"):
                logger.warning(
                    "switch_to_slides: start_slideshow dismissed TWICE — "
                    "PowerPoint is in the stuck 'is-presenting' state. "
                    "Attempting quit+relaunch recovery (will cost ~3-5 s)."
                )
                relaunch_attempted = True
                try:
                    relaunch_res = self._ppt.quit_and_relaunch()
                except AttributeError:
                    # Module without the helper (e.g. older test fixture).
                    logger.info(
                        "switch_to_slides: ppt module has no "
                        "quit_and_relaunch — skipping nuclear recovery"
                    )
                    relaunch_res = None
                except Exception as exc:   # noqa: BLE001
                    logger.warning(
                        "switch_to_slides: quit_and_relaunch raised: %s",
                        exc,
                    )
                    relaunch_res = None

                if relaunch_res is not None and relaunch_res.ok:
                    relaunch_ok = True
                    relaunch_data = dict(relaunch_res.data or {})
                    logger.info(
                        "switch_to_slides: quit_and_relaunch succeeded "
                        "(quit_ms=%s reopen_ms=%s frontmost=%s) — "
                        "retrying start_slideshow",
                        relaunch_data.get("quit_ms"),
                        relaunch_data.get("reopen_ms"),
                        relaunch_data.get("frontmost"),
                    )
                    # Tiny settle before we press 'play'. open -a +
                    # activate_app inside relaunch already did the heavy
                    # lifting; this is a cheap paranoia-sleep to let
                    # PPT finish painting its main window.
                    _time.sleep(0.25)
                    start_result = self._ppt.start_slideshow(
                        from_slide=checkpoint, verify=True,
                    )
                    start_attempts = 3
                else:
                    relaunch_ok = False
                    if relaunch_res is not None:
                        relaunch_data = dict(relaunch_res.data or {})
                        relaunch_data["code"] = relaunch_res.code
                        relaunch_data["message"] = relaunch_res.message
                    logger.warning(
                        "switch_to_slides: quit_and_relaunch failed (%s) "
                        "— giving up on fullscreen resume for this trip",
                        (relaunch_res.code if relaunch_res is not None
                         else "NO_HELPER"),
                    )

            resumed = bool(start_result.ok)
            if start_result.ok and checkpoint is not None:
                navigated_to = checkpoint
            elif not start_result.ok:
                # Only escalate log level if nuclear recovery was tried.
                log_fn = logger.error if relaunch_attempted else logger.warning
                log_fn(
                    "switch_to_slides: start_slideshow(from_slide=%r) "
                    "failed after %d attempt(s) (%s: %s). "
                    "relaunch_attempted=%s relaunch_ok=%s",
                    checkpoint, start_attempts,
                    start_result.code, start_result.message,
                    relaunch_attempted, relaunch_ok,
                )
        elif checkpoint is not None:
            # Not resuming fullscreen but still want the normal-view
            # cursor on the right slide.
            try:
                goto_result = self._ppt.goto(checkpoint)
                if goto_result.ok:
                    navigated_to = checkpoint
                else:
                    logger.info(
                        "switch_to_slides: goto(%d) failed (%s: %s)",
                        checkpoint, goto_result.code, goto_result.message,
                    )
            except Exception as exc:   # noqa: BLE001
                logger.info(
                    "switch_to_slides: goto raised (non-fatal): %s", exc,
                )

        # Reset the in-memory fullscreen flag. The persisted checkpoint
        # is intentionally LEFT IN PLACE — the keyboard hook will
        # overwrite it on the next slide change, and leaving a
        # last-known-good value protects against a next-handoff race
        # where switch_to_visor runs before the hook has refreshed
        # state.
        self._was_fullscreen_before_visor = False

        msg_bits = ["PowerPoint in front"]
        if navigated_to is not None:
            msg_bits.append(f"slide {navigated_to}")
        if resumed:
            msg_bits.append("fullscreen resumed")

        logger.info(
            "switch_to_slides: done target=slides resumed_fullscreen=%s "
            "navigated_to_slide=%s checkpoint_was=%s was_fullscreen=%s",
            resumed, navigated_to, checkpoint, was_fullscreen,
        )

        return WindowResult(
            ok=True,
            code="OK",
            message=", ".join(msg_bits),
            data={
                "target": "slides",
                "resumed_fullscreen": resumed,
                "navigated_to_slide": navigated_to,
                "start_attempts": start_attempts,
                "quit_and_relaunch_attempted": relaunch_attempted,
                "quit_and_relaunch_ok": relaunch_ok,
                "quit_and_relaunch": relaunch_data or None,
            },
        )

    # ─── diagnose ─────────────────────────────────────────────

    async def diagnose(self) -> dict[str, Any]:
        """Snapshot for the ``/diagnose`` endpoint — current PPT + Chrome
        state plus the remembered fullscreen flag and the persisted
        slide checkpoint.

        In spaces-swipe mode, also reports ``spaces`` subsection with
        OS support + System Events reachability so the presenter can
        tell at a glance whether keystroke injection will work before
        the first handoff.
        """
        ppt_state: dict[str, Any]
        try:
            ppt_state = self._ppt.diagnose()
        except Exception as exc:   # noqa: BLE001
            ppt_state = {"ok": False, "error": str(exc)}
        chrome_state = await self._chrome.health_check()
        try:
            persisted_checkpoint = self._checkpoint.load()
        except Exception as exc:   # noqa: BLE001
            logger.debug("diagnose: checkpoint.load raised: %s", exc)
            persisted_checkpoint = None

        out: dict[str, Any] = {
            "powerpoint": ppt_state,
            "chrome": chrome_state,
            "was_fullscreen_before_visor": self._was_fullscreen_before_visor,
            "slide_checkpoint": persisted_checkpoint,
            "visor_url_prefix": self._visor_prefix,
            "use_spaces_swipe": self._use_spaces_swipe,
        }
        if self._use_spaces_swipe:
            try:
                out["spaces"] = self._spaces.diagnose()
            except Exception as exc:   # noqa: BLE001
                logger.debug(
                    "diagnose: spaces.diagnose() raised: %s", exc,
                )
                out["spaces"] = {"ok": False, "error": str(exc)}
        return out
