"""``handoff_to_specialist`` — Session A's gateway to Session B.

Per ``modular-extension.md § 4.2``, this tool:

1. Validates that ``agent_id`` names a registered specialist.
2. Drops duplicate tool_use events from the same turn (SPECULATIVE +
   FINAL emission) via a short-lived payload dedup (Fix 1B).
3. **Atomically** reserves a handoff slot against the rate limiter
   (concurrency + sliding window + per-session cap) via
   :meth:`HandoffRateLimiter.check_and_record` — no ``await`` between
   the check and the counter bump, so two concurrent handoffs can't
   both think the slot is free (Fix 1A).
4. POSTs ``/api/start`` to the visor with the specialist's phase labels
   so the overlay appears **before** Chrome comes to the front.
5. Brings the Chrome visor tab to the foreground via the WindowManager.
6. Returns ``{ok: true, handoff_ready: true, agent_id, query, customer,
   session_b_config}``.

The Node session manager inspects ``handoff_ready`` in the tool
result, waits for Session A's handoff line to finish playing, and then
opens Session B with the config returned here (voice, system prompt,
tool defs, terminator phrases).

Total latency budget for this handler: ≤ 500 ms p95 — the actual work
is three cheap HTTP/AppleScript calls.

Never raises. Surfaces errors as structured ``{"ok": False, "code": ...,
"message": "..."}`` dicts that Session A can briefly apologize with.

Failure rollback: if *any* downstream step after a successful
reservation raises, we :meth:`release` the slot before returning so
the rate limiter's ``active`` counter can't get stuck above zero.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from src.platform.window_manager import WindowResult
from src.state.handoff_rate import (
    CODE_CONCURRENCY,
    CODE_OK,
    CODE_RATE_LIMITED,
    CODE_SESSION_LIMIT,
)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Unprompted-visor-swipe guard (2026-05-12)
# ─────────────────────────────────────────────────────────────
#
# How long (seconds) after a user-initiated ``switch_window(target=
# 'slides')`` call the handoff's automatic ``switch_to_visor`` stays
# suppressed. Picked to cover Nova Sonic's typical SPECULATIVE→FINAL
# gap (~100–300 ms) plus a healthy margin for any queued ASR tails,
# while still being short enough that an intentional follow-up like
# "Nova, now show me the RSI chart" within a normal conversational
# cadence goes through unaffected. Tune via env var.
_HANDOFF_SLIDES_GUARD_S = max(
    0.0,
    float(os.environ.get("NOVA_HANDOFF_SLIDES_GUARD_MS", "3000")) / 1000.0,
)


# ─────────────────────────────────────────────────────────────
# Payload-level dedup for Nova Sonic's SPECULATIVE + FINAL emission
# ─────────────────────────────────────────────────────────────
#
# Nova Sonic occasionally emits the same tool_use twice in one turn —
# once during its SPECULATIVE generation stage, once during FINAL,
# ~100–300 ms apart. Bedrock usually assigns them different
# ``tool_use_id``s, so the Node-side dedup keyed on id does not catch
# this case. Without a second layer of dedup both requests reach this
# handler concurrently and pay the full ~2.5 s of visor+window work,
# *and* the race window between the rate-limiter check and record
# (closed by Fix 1A) previously let both requests pass the
# concurrency gate at ``active=0``.
#
# The dedup store is attached to ``app_state`` lazily (one dict per
# running FastAPI instance) rather than being a module-level global —
# so tests get a fresh cache per fixture without special reset hooks.
# Keys: (browser_session_id, agent_id, normalized query prefix).
# TTL: 5 s. Entries are GC'd lazily on every call so the dict stays
# bounded.
#
# See: (internal postmortem 2026-05-09) § 4
# for the incident that motivated this layer.
_HANDOFF_PAYLOAD_DEDUP_TTL_S = 5.0
_HANDOFF_PAYLOAD_DEDUP_QUERY_PREFIX = 80

CODE_DUPLICATE = "HANDOFF_DUPLICATE"


def _dedup_key(
    browser_session_id: str | None, agent_id: str, query: str,
) -> tuple[str, str, str]:
    return (
        browser_session_id or "-",
        agent_id,
        query[:_HANDOFF_PAYLOAD_DEDUP_QUERY_PREFIX],
    )


def _get_dedup_store(app_state: Any) -> dict[tuple[str, str, str], float]:
    """Return (and lazily create) the per-app-state dedup dict.

    Storing it on ``app_state`` rather than as a module global keeps
    each FastAPI instance isolated and, crucially, gives every pytest
    fixture a fresh cache without needing a reset hook.
    """
    store = getattr(app_state, "_handoff_payload_dedup", None)
    if store is None:
        store = {}
        try:
            app_state._handoff_payload_dedup = store
        except AttributeError:
            # Frozen dataclass or similar — fall back to a one-shot
            # dict; dedup simply won't work for this call, which is
            # only a performance concern, not a correctness one
            # (Fix 1A's atomic reservation still prevents double-count).
            return {}
    return store


def _is_recent_duplicate(
    store: dict[tuple[str, str, str], float],
    key: tuple[str, str, str],
    now: float,
) -> bool:
    """Returns True iff the same (session, agent, query) was dispatched
    within ``_HANDOFF_PAYLOAD_DEDUP_TTL_S``. GCs expired entries as a
    side-effect so the dict stays bounded without a background sweeper.
    """
    # GC first — cheap since the dict is O(tens) in practice.
    for k, t in list(store.items()):
        if now - t > _HANDOFF_PAYLOAD_DEDUP_TTL_S:
            del store[k]
    seen_at = store.get(key)
    if seen_at is not None and now - seen_at < _HANDOFF_PAYLOAD_DEDUP_TTL_S:
        return True
    store[key] = now
    return False


async def handoff_to_specialist_handler(
    *,
    tool_input: dict,
    app_state: Any,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Handle a ``handoff_to_specialist`` tool call from Session A.

    Tool input schema::

        {
          "agent_id": "financial" | "legal" | ...,
          "query":    str,
          "customer": str (optional),
        }
    """
    agent_id = _clean_str(tool_input, "agent_id")
    query = _clean_str(tool_input, "query")
    customer = tool_input.get("customer") if isinstance(tool_input, dict) else None

    if not agent_id:
        return _bad_args("agent_id is required")
    if not query:
        return _bad_args("query is required")

    # 1. Validate agent exists.
    try:
        agent = app_state.registry.agent(agent_id)
    except KeyError:
        available = app_state.registry.ids()
        logger.info("handoff_to_specialist: unknown agent_id=%r", agent_id)
        return {
            "ok": False,
            "code": "UNKNOWN_SPECIALIST",
            "message": (
                f"No specialist registered with id={agent_id!r}. "
                f"Available: {available}"
            ),
            "available": available,
        }

    # 2. Payload-level dedup (Fix 1B). Must come BEFORE the rate-limiter
    # reservation so duplicate tool_use events from the same turn don't
    # consume a slot. Non-atomic with respect to parallel requests —
    # on a true concurrent race one of the two will get DUPLICATE and
    # the other will proceed to the atomic reservation below, which is
    # the correct outcome.
    now = time.monotonic()
    key = _dedup_key(browser_session_id, agent_id, query)
    dedup_store = _get_dedup_store(app_state)
    if _is_recent_duplicate(dedup_store, key, now):
        logger.info(
            "handoff_to_specialist: duplicate payload ignored agent=%s "
            "query_prefix=%r tool_use_id=%s",
            agent_id, query[:40], tool_use_id,
        )
        return {
            "ok": False,
            "code": CODE_DUPLICATE,
            "message": "Duplicate handoff request ignored.",
        }

    # 3. Atomic rate-limiter reservation (Fix 1A). From here until
    # return (or rollback on failure) the slot is counted as in-flight.
    allowed, reason = app_state.handoff_rate.check_and_record(agent_id=agent_id)
    if not allowed:
        logger.info(
            "handoff_to_specialist: rate-limited agent=%s reason=%s",
            agent_id, reason,
        )
        return {
            "ok": False,
            "code": reason,
            "message": _rate_limit_message(reason),
        }

    # From this point on, any failure path MUST call release() before
    # returning so the concurrency counter doesn't leak.
    reservation_owner = agent_id

    try:
        # 3b. Reset the per-handoff pipeline progress tracker. From this
        # point until the next handoff, ``/cancel_session_tools`` uses
        # this flag to tell "pipeline completed" apart from "pipeline
        # aborted early" — see internal postmortem 2026-05-09
        # § 7 P0-#1 for the context and the failure mode it
        # prevents.
        tracker = getattr(app_state, "b_pipeline_reached_render", None)
        if tracker is not None:
            tracker[agent_id] = False
        # Also reset the render-result idempotency cache. Keeping a
        # stale result from a prior handoff around would cause a
        # render_report in the NEW handoff to be short-circuited with
        # last session's report, which would be the worst kind of
        # silent correctness bug. See ``b_last_render_result`` in
        # ``src/api_server.py`` for details.
        render_cache = getattr(app_state, "b_last_render_result", None)
        if render_cache is not None:
            render_cache.pop(agent_id, None)

        # Reset the per-handoff pipeline capture slot. Same reasoning
        # as the idempotency cache above — stale slices from the
        # previous handoff (e.g. IPC Mexicano's fetch_data) must never
        # bleed into this handoff's promoted current_report (e.g.
        # Tesla). ``_dispatch_session_b`` lazily re-creates the slot
        # on the first successful tool result.
        capture_store = getattr(app_state, "b_pipeline_capture", None)
        if capture_store is not None:
            capture_store.pop(agent_id, None)

        # 4. Arm the visor overlay with this specialist's phase labels.
        # Best-effort — visor outage never fails the handoff.
        try:
            await app_state.visor.start(phases=list(agent.visor_phases))
        except Exception as exc:   # noqa: BLE001
            logger.warning("handoff_to_specialist: visor.start failed: %s", exc)

        # 5. Bring Chrome's visor tab to the front — UNLESS the user
        #    just explicitly asked for slides within the last few
        #    seconds.
        #
        # 2026-05-12 postmortem (unprompted-visor-swipe): Nova Sonic
        # occasionally fires a ``handoff_to_specialist`` tool call
        # ~100–300 ms AFTER the presenter has explicitly said
        # "switch to PPT" / "back to slides", because:
        #   (a) Nova's SPECULATIVE generation stage can emit a stale
        #       handoff that its FINAL stage contradicts but the
        #       tool_use has already been dispatched, OR
        #   (b) ASR mishears a follow-up utterance ("switch" → "show").
        # Without a guard, the handoff unconditionally swipes to the
        # visor Space, yanking the presenter off the slides they
        # JUST asked for. From their seat it looks "unprompted".
        #
        # Guard: if the ``switch_window(target='slides')`` tool was
        # called within the last ``_HANDOFF_SLIDES_GUARD_S`` seconds,
        # SUPPRESS the automatic visor swipe but still let Carlos's
        # pipeline run. The report renders normally; the presenter
        # stays on slides. If they want to see it they can swipe
        # right (Ctrl+→) manually — one cheap keypress — and the
        # handback's Nova Sonic audio narration still plays.
        slides_guard_s = _HANDOFF_SLIDES_GUARD_S
        wm = app_state.window_manager
        suppressed_by_guard = (
            hasattr(wm, "recently_swiped_to_slides")
            and wm.recently_swiped_to_slides(within_s=slides_guard_s)
        )
        if suppressed_by_guard:
            logger.info(
                "handoff_to_specialist: user swiped to slides <%ss ago — "
                "suppressing automatic switch_to_visor agent=%s "
                "(report will still render; presenter can swipe manually)",
                slides_guard_s, agent_id,
            )
            window_result = WindowResult(
                ok=False,
                code="SUPPRESSED_RECENT_USER_SLIDES",
                message=(
                    "Suppressed automatic visor swipe — the presenter "
                    f"asked for slides within the last {slides_guard_s}s."
                ),
                data={
                    "target": "visor",
                    "via": "guard_recent_user_slides",
                    "cooldown_s": slides_guard_s,
                },
            )
            visor_active = False
        else:
            window_result = await app_state.window_manager.switch_to_visor()
            visor_active = bool(window_result.ok)
            if not visor_active:
                # Non-fatal — the report will still land; the audience just
                # sees the PowerPoint view until they switch manually.
                logger.info(
                    "handoff_to_specialist: switch_to_visor failed (%s): %s",
                    window_result.code, window_result.message,
                )
            else:
                # Record the foreground state so analyze_slide's guard
                # (see api_server._dispatch_session_a) can refuse to
                # describe a PowerPoint slide while the presenter is
                # actually looking at the fresh specialist report on
                # the visor. Paired with ``current_report`` which is
                # populated when render_report succeeds.
                try:
                    app_state.last_foreground_target = "visor"
                except AttributeError:
                    # Older app_state (tests, manual construction) —
                    # skip quietly. The analyze_slide guard falls
                    # open when the attribute is absent.
                    pass

        # 6. Return the signal the Node session manager uses to open
        # Session B. Reservation stays counted until the session
        # manager fires handback → POST /internal/handoff_released.
        result = {
            "ok": True,
            "handoff_ready": True,
            "agent_id": agent_id,
            "query": query,
            "customer": customer,
            "visor_active": visor_active,
            "window_switch": window_result.to_dict(),
            # The Node side has the system prompt + tool defs
            # in-memory (loaded at startup). These two fields let it
            # pick the right voice and terminators per-specialist
            # without another HTTP call.
            "session_b_config": {
                "voice_id": agent.voice_id,
                "terminators": list(agent.terminator_phrases),
                "locale": agent.locale,
                "display_name": agent.display_name,
            },
            "message": f"Ready for {agent.display_name}.",
        }
        # Reservation now owned by the caller (released on handback).
        reservation_owner = None
        return result
    finally:
        if reservation_owner is not None:
            # An exception escaped the try-block AND we still hold the
            # reservation — roll it back so the concurrency counter
            # doesn't leak and block every future handoff.
            logger.warning(
                "handoff_to_specialist: releasing reservation after "
                "downstream failure agent=%s",
                reservation_owner,
            )
            try:
                app_state.handoff_rate.release(agent_id=reservation_owner)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "handoff_to_specialist: release() also failed"
                )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _clean_str(tool_input: dict, key: str) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    v = tool_input.get(key)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _bad_args(message: str) -> dict[str, Any]:
    return {"ok": False, "code": "BAD_ARGS", "message": message}


def _rate_limit_message(code: str) -> str:
    """Short human-readable explanation that Session A can echo."""
    if code == CODE_CONCURRENCY:
        return "A specialist is already running."
    if code == CODE_RATE_LIMITED:
        return "Handoff rate limit hit — try again in a moment."
    if code == CODE_SESSION_LIMIT:
        return "Handoff session limit reached."
    if code == CODE_DUPLICATE:
        return "Duplicate handoff request ignored."
    return f"Handoff rejected ({code})."
