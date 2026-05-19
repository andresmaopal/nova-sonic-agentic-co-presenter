"""Python REST API backend for nova-sonic-agentic-co-presenter.

Endpoints exposed to the Node.js WebSocket server (session manager):

Slide control (presenter assistant, carried over from presenterassistant):
    POST /preprocess        — preprocess a PPTX file and load slides
    GET  /slide_info        — current slide index + total
    POST /slide_update      — update the current slide index
    GET  /compat            — PowerPoint/macOS diagnostic

Unified tool dispatcher (v1 extension):
    POST /tool_call         — handles both Session A tools (analyze_slide,
                              navigate_slide, control_slideshow,
                              switch_window, handoff_to_specialist) and
                              Session B tools (fetch_data, transform_data,
                              generate_chart, compose_summary,
                              render_report, end_session)

Registry / handoff helpers (added with the specialist registry):
    GET  /registry/ids
    GET  /registry/{agent_id}
    GET  /registry/catalog?locale=en
    POST /internal/handoff_released
    POST /cancel_session_tools?session_id=B

Aggregate health:
    GET  /diagnose          — PowerPoint + Chrome + visor + chart MCP +
                              Bedrock + Finalysis + handles + rate limiter

Backwards compatible: the existing presenterassistant tests that POST
to ``/tool_call`` without a ``session_id`` still pass because
``session_id`` defaults to ``"A"``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import boto3
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.clients.antv_chart import AntvChartClient
from src.clients.bedrock_router import BedrockRouterClient
from src.clients.finalysis import FinalysisClient
from src.clients.visor import VisorClient
from src.hooks.keyboard_hook import run_hook_inprocess
from src.platform.chrome import ChromeAdapter
from src.platform.window_manager import WindowManager
from src.pptx_preprocessor import convert_pptx, load_from_images
from src.render.report import ReportRenderer
from src.slide_cache import load_cached, save_cache
from src.slide_store import SlideStore
from src.specialists.registry import AgentRegistry
from src.state.data_handles import DataHandleStore
from src.state.handoff_rate import HandoffRateLimiter
from src.tools.analyze_slide import analyze_slide
from src.tools.control_slideshow import control_slideshow
from src.tools.get_premarket import get_premarket_handler
from src.tools.get_quote import get_quote_handler
from src.tools.handoff_to_specialist import handoff_to_specialist_handler
from src.tools.navigate_slide import navigate_slide
from src.tools.session_b import SESSION_B_HANDLERS
from src.tools.switch_window import switch_window_handler


# ─────────────────────────────────────────────────────────────
# Logging configuration (module scope — critical)
# ─────────────────────────────────────────────────────────────
#
# start.sh launches the backend with ``python -m uvicorn src.api_server:app``
# which IMPORTS this module but never calls ``main()``. If ``basicConfig``
# were only inside ``main()`` (as it used to be), the root logger would
# have no handler in production and every ``logger.info(...)`` call in
# every module under ``src/`` would be silently dropped — leaving
# ``logs/python.log`` with nothing but uvicorn access lines.
#
# We fix it here, at module scope, guarded by ``force=False`` so that:
# - uvicorn's own log config (if any) is still respected; we only
#   initialize if no handler is already attached to the root logger,
# - ``main()`` can still override via ``logging.basicConfig(..., force=True)``
#   for the ``--verbose`` CLI flag.
#
# See (internal postmortem 2026-05-09) § RC-2.

_LOG_LEVEL = os.environ.get("NOVA_LOG_LEVEL", "INFO").upper()
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=getattr(logging, _LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Reduce httpx's default INFO chatter ("HTTP Request: GET ...") to
    # WARNING so our own INFO lines aren't drowned out. Our Finalysis
    # wrapper already logs every call with richer context.
    logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# Repo root — used by ReportRenderer + /diagnose.
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────
# Pydantic request models
# ─────────────────────────────────────────────────────────────


class PreprocessRequest(BaseModel):
    pptx_path: str
    images_dir: Optional[str] = None


class ToolCallRequest(BaseModel):
    """Unified request for both Session A and Session B tools.

    Backwards compatible: ``session_id`` defaults to ``"A"`` so
    callers that don't know about Session B (including the old
    presenterassistant tests) still work.
    """

    tool_name: str
    tool_input: dict
    session_id: Literal["A", "B"] = "A"
    agent_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    browser_session_id: Optional[str] = None


class SlideUpdateRequest(BaseModel):
    slide_index: int


# ─────────────────────────────────────────────────────────────
# Shared module-level state (kept for compatibility with the
# presenterassistant tests that import ``slide_store`` / ``bedrock_client``)
# ─────────────────────────────────────────────────────────────

slide_store = SlideStore()
bedrock_client: Any = None


# ─────────────────────────────────────────────────────────────
# FastAPI app + lifespan
# ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    region = getattr(app.state, "region", "us-east-1")

    # 1. Keyboard hook (runs in a background thread; reads PPT slide).
    hook_thread = threading.Thread(
        target=run_hook_inprocess, args=(slide_store,), daemon=True,
    )
    hook_thread.start()
    logger.info("Keyboard hook started")

    # 2. Bedrock vision client — used by analyze_slide (Claude Haiku vision).
    global bedrock_client
    bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    app.state.bedrock_client = bedrock_client

    # Best-effort warm-up so the first tool call doesn't pay TLS cost.
    try:
        warmup = boto3.client("bedrock", region_name=region)
        warmup.list_foundation_models(byOutputModality="TEXT")
        logger.info("Bedrock client warm-up successful")
    except Exception as exc:   # noqa: BLE001
        logger.warning("Bedrock warm-up failed (will retry on first call): %s", exc)

    # 3. New shared clients (visor / AntV / Finalysis / Bedrock router).
    app.state.slide_store = slide_store
    app.state.visor = VisorClient()
    app.state.antv_chart = AntvChartClient()
    app.state.finalysis = FinalysisClient()
    app.state.bedrock_router = BedrockRouterClient(region=region)
    # Last-known pptx path — populated by /preprocess, consumed by
    # control_slideshow's NO_PRESENTATION self-healing fallback.
    app.state.last_pptx_path = None

    # 4. Platform adapters — PowerPoint module is already imported via
    # the slide tools; Chrome + WindowManager are new.
    app.state.chrome = ChromeAdapter()
    # Opt-in dual-fullscreen + Spaces-swipe mode. When the env flag is
    # set, switch_to_visor/switch_to_slides bypass PPT activation +
    # slideshow start/stop and use Ctrl+←/→ keystrokes instead.
    # See ``src/platform/spaces.py`` and the dual-fullscreen rollout
    # plan in the PR2 design notes.
    _use_spaces_swipe = (
        os.environ.get("NOVA_USE_SPACES_SWIPE", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if _use_spaces_swipe:
        logger.info(
            "WindowManager: spaces-swipe mode ENABLED via "
            "NOVA_USE_SPACES_SWIPE — switch_window will use Ctrl+←/→ "
            "keystrokes instead of PPT slideshow restart."
        )
    app.state.window_manager = WindowManager(
        chrome=app.state.chrome,
        use_spaces_swipe=_use_spaces_swipe,
    )

    # 5. Report rendering (per-specialist templates).
    app.state.report_renderer = ReportRenderer(repo_root=_REPO_ROOT)

    # 6. Opaque data-handle store (shared across Session B tool calls).
    app.state.data_handles = DataHandleStore()

    # 7. Handoff rate limiter (concurrency + window + session caps).
    app.state.handoff_rate = HandoffRateLimiter()

    # 8. Specialist registry — auto-discover + attach toolkits with the
    # shared clients.
    registry = AgentRegistry.auto_discover()
    registry.attach_toolkits(clients={
        "finalysis": app.state.finalysis,
        "bedrock_router": app.state.bedrock_router,
    })
    app.state.registry = registry
    logger.info("Specialist registry ready: %s", registry.ids())

    # 9. In-flight task registry for /cancel_session_tools.
    app.state.in_flight_tools: dict[str, asyncio.Task] = {}

    # 10. Per-handoff pipeline progress tracker (see postmortem
    # 2026-05-09-end-session-before-render.md § RC-1 / § 7 P0-#1).
    #
    # Session B has a strict 6-tool pipeline; ``render_report`` is the
    # one that actually produces the artefact the audience sees. When
    # Carlos calls ``end_session`` mid-pipeline (e.g. after a
    # ``FINALYSIS_ERROR`` on the first ``fetch_data``), the Node session
    # manager handbacks with ``reason="end_session"`` — which used to be
    # treated as graceful by ``/cancel_session_tools`` and therefore
    # dismissed the visor overlay into the cold-boot welcome copy.
    #
    # We track whether ``render_report`` succeeded for the current
    # handoff so the cancel endpoint can tell "pipeline completed
    # cleanly" apart from "pipeline aborted early (call it abnormal)."
    #
    # Keyed by ``agent_id``. Reset in ``handoff_to_specialist`` at the
    # start of every new handoff. Set to ``True`` in the Session B
    # dispatcher when ``render_report`` returns ``ok: True``.
    app.state.b_pipeline_reached_render: dict[str, bool] = {}

    # Cache of the LAST successful render_report tool result keyed by
    # ``agent_id``. Used by the Session B dispatcher to short-circuit
    # a duplicate ``render_report`` call within the same handoff —
    # returning the cached payload means we do NOT re-invoke the
    # renderer (no second file write, no second chokidar event, no
    # visor "force refresh" overlay covering the report the user
    # already sees). Duplicate calls happen when the specialist goes
    # into a "compose_summary → render_report" loop instead of calling
    # end_session after the first render; we still let the handback
    # fire from the CACHED result's ``trigger_handback: True``.
    # Reset in ``handoff_to_specialist`` alongside
    # ``b_pipeline_reached_render``.
    app.state.b_last_render_result: dict[str, dict] = {}

    # 10c. Per-handoff pipeline capture — aggregates structured data from
    # each Session B tool result so we can promote it into
    # ``app.state.current_report`` atomically on ``render_report`` success.
    #
    # Keyed by ``agent_id``. Each slot holds a sub-dict with keys:
    #   fetch   — ticker / kind / indicator / window / start_date /
    #             end_date / count  (from ``fetch_data`` tool input + result)
    #   summary — stats (first/last/high/low/pct_change) + bullets +
    #             description + customer_name  (from ``compose_summary``)
    #   chart   — url + title  (from ``generate_chart``)
    #
    # Reset at the START of every new handoff in
    # ``handoff_to_specialist_handler`` alongside the other per-handoff
    # dicts, and cleared after promotion on render_report success so a
    # stale slice can't leak into the next handoff.
    app.state.b_pipeline_capture: dict[str, dict] = {}

    # 11. The AUTHORITATIVE current-report slot. Populated by
    # ``_dispatch_session_b`` on ``render_report`` success with the full
    # structured payload Nova needs to narrate the chart on the visor —
    # ticker, stats, bullets, description, chart_title, chart_url,
    # customer_name, report_date, rendered_at.
    #
    # This is the SINGLE source of truth consumed by the
    # ``read_current_report`` Session A tool (Nova's mandatory grounding
    # call before quoting any numbers from the visor). Context-window
    # HANDBACK_BRIEFs accumulate across handoffs and confuse Nova
    # Sonic's voice model; a queryable slot fixes the "which report?"
    # ambiguity and eliminates hallucinated numbers like Nova quoting
    # Tesla at ~$100 from training weights when the real report shows
    # $370.85 (see 2026-05-12 demo incident).
    #
    # ``None`` until the first successful render_report of the process
    # lifetime. Subsequent renders overwrite the slot in full (no merge)
    # so "current" always means "most recently rendered", not "union of
    # everything ever rendered".
    app.state.current_report: Optional[dict] = None

    # Track which window was last brought to foreground via an explicit
    # swap (either a Session A switch_window call or the auto-swipe
    # that fires inside handoff_to_specialist). Values:
    #   "visor"   — Chrome visor tab is in front
    #   "slides"  — PowerPoint slideshow is in front
    #   None      — never explicitly switched (cold boot)
    #
    # Used by analyze_slide dispatch to refuse when the presenter is
    # looking at a fresh financial report and meant to ask about the
    # visor chart instead (observed 2026-05-13: Nova called
    # analyze_slide 3× in a row trying to describe a 3-symbol
    # comparison chart; the tool happily returned slide-2 notes).
    app.state.last_foreground_target: Optional[str] = None

    yield

    # Teardown — best-effort close.
    for name, client in (
        ("visor", app.state.visor),
        ("antv", app.state.antv_chart),
        ("finalysis", app.state.finalysis),
        ("chrome", app.state.chrome),
    ):
        try:
            await client.close()
        except Exception as exc:   # noqa: BLE001
            logger.debug("%s.close() failed during shutdown: %s", name, exc)


app = FastAPI(title="nova-sonic-agentic-co-presenter API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Slide control endpoints (unchanged from presenterassistant)
# ─────────────────────────────────────────────────────────────


@app.post("/preprocess")
async def preprocess_endpoint(request: PreprocessRequest):
    """Preprocess PPTX and load into the shared SlideStore."""
    # Remember the last-known pptx path so self-healing tools (e.g.
    # control_slideshow fallback on NO_PRESENTATION) can reopen the deck
    # if the user accidentally closes PowerPoint between voice sessions.
    # See postmortem-style note in control_slideshow.py:_reopen_last_pptx.
    if request.pptx_path:
        app.state.last_pptx_path = request.pptx_path

    if slide_store.total_slides > 0:
        logger.info("Slides already loaded (%d), skipping reprocess",
                    slide_store.total_slides)
        return {"slide_count": slide_store.total_slides, "status": "ok"}
    try:
        if request.images_dir:
            slides = await asyncio.to_thread(
                load_from_images, request.pptx_path, request.images_dir,
            )
        else:
            slides = await asyncio.to_thread(load_cached, request.pptx_path)
            if slides is None:
                slides = await asyncio.to_thread(convert_pptx, request.pptx_path)
                await asyncio.to_thread(save_cache, request.pptx_path, slides)
        count = slide_store.load_slides(slides)
        logger.info("Loaded %d slides from %s", count, request.pptx_path)
        return {"slide_count": count, "status": "ok"}
    except Exception as exc:   # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/slide_info")
async def slide_info_endpoint():
    if slide_store.total_slides == 0:
        raise HTTPException(
            status_code=409,
            detail="No slides loaded. Call /preprocess first.",
        )
    return {
        "current_index": slide_store.current_index,
        "total_slides": slide_store.total_slides,
    }


@app.post("/slide_update")
async def slide_update_endpoint(request: SlideUpdateRequest):
    if slide_store.total_slides == 0:
        raise HTTPException(
            status_code=409,
            detail="No slides loaded. Call /preprocess first.",
        )
    try:
        slide_store.set_current_index(request.slide_index)
        return {"status": "ok", "slide_index": request.slide_index}
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/compat")
async def compat_endpoint():
    from src.platform import powerpoint as ppt
    return await asyncio.to_thread(ppt.diagnose)


@app.get("/active_pptx")
async def active_pptx_endpoint():
    """Return the active PowerPoint presentation name (used by browser UI to pre-fill)."""
    from src.platform import powerpoint as ppt

    def _get_name():
        diag = ppt.diagnose()
        return diag.get("active_presentation_name")

    name = await asyncio.to_thread(_get_name)
    return {"pptx_path": name or ""}


# ─────────────────────────────────────────────────────────────
# Unified tool dispatcher
# ─────────────────────────────────────────────────────────────


@app.post("/tool_call")
async def tool_call_endpoint(request: ToolCallRequest, http_request: Request):
    """Dispatch a tool call to the right handler based on ``session_id``.

    Session A tools (slide control + window switch + handoff) use the
    legacy shape for the existing three and the new async handlers for
    switch_window / handoff_to_specialist.

    Session B tools all go through the :mod:`src.tools.session_b`
    dispatch table, which requires ``agent_id`` (Session B has no
    global "current agent" — the Node manager passes it explicitly).
    """
    app_state = http_request.app.state
    task_key = _task_key(request)

    async def _run() -> dict:
        if request.session_id == "B":
            return await _dispatch_session_b(request, app_state)
        return await _dispatch_session_a(request, app_state)

    # Track the running task so /cancel_session_tools can kill it.
    task = asyncio.current_task()
    if task is not None:
        app_state.in_flight_tools[task_key] = task
    try:
        try:
            result = await _run()
        except asyncio.CancelledError:
            # Surface the cancellation as a clean tool result; the caller
            # (Node session manager) can narrate it briefly.
            return {"result": {"ok": False, "code": "CANCELLED",
                                "message": "tool call cancelled"}}
        return {"result": result}
    finally:
        app_state.in_flight_tools.pop(task_key, None)


async def _dispatch_session_a(
    request: ToolCallRequest, app_state: Any,
) -> dict[str, Any]:
    """Route a Session A tool call to the right handler."""
    tool_name = request.tool_name

    # analyze_slide visor-guard (runs BEFORE the slides-loaded check
    # so it works even if no deck is loaded — what matters is that
    # the presenter is on the visor looking at a fresh report and
    # likely meant to ask about the chart, not a slide).
    if tool_name == "analyze_slide":
        foreground = getattr(app_state, "last_foreground_target", None)
        snapshot = getattr(app_state, "current_report", None)
        if foreground == "visor" and snapshot:
            logger.info(
                "analyze_slide refused: visor foreground + fresh "
                "report (slug=%s) — redirecting Nova to "
                "read_current_report",
                (snapshot or {}).get("slug"),
            )
            return {
                "ok": False,
                "code": "WRONG_TOOL",
                "message": (
                    "El reporte del especialista está en el visor. "
                    "Para hablar del gráfico actual usa la herramienta "
                    "read_current_report (sin argumentos). "
                    "analyze_slide es SOLO para diapositivas de "
                    "PowerPoint; si el presentador realmente quiere "
                    "la diapositiva, primero llama switch_window "
                    "target=slides."
                ),
                "speech_hint": (
                    "Do NOT narrate this error. Immediately call "
                    "read_current_report (no arguments) and narrate "
                    "its response instead."
                ),
            }

    # Existing slide-control tools — require slides loaded.
    if tool_name in ("analyze_slide", "navigate_slide", "control_slideshow"):
        if slide_store.total_slides == 0:
            raise HTTPException(
                status_code=409,
                detail="No slides loaded. Call /preprocess first.",
            )

        if tool_name == "analyze_slide":
            client = getattr(app_state, "bedrock_client", None) or bedrock_client
            return await asyncio.to_thread(
                analyze_slide,
                slide_store=slide_store,
                tool_input=request.tool_input,
                bedrock_client=client,
            )
        if tool_name == "navigate_slide":
            return await asyncio.to_thread(
                navigate_slide,
                slide_store=slide_store,
                tool_input=request.tool_input,
            )
        if tool_name == "control_slideshow":
            return await asyncio.to_thread(
                control_slideshow,
                tool_input=request.tool_input,
                app_state=app_state,
            )

    # New Session A tools.
    if tool_name == "switch_window":
        return await switch_window_handler(
            tool_input=request.tool_input,
            app_state=app_state,
            tool_use_id=request.tool_use_id,
            browser_session_id=request.browser_session_id,
        )

    if tool_name == "handoff_to_specialist":
        return await handoff_to_specialist_handler(
            tool_input=request.tool_input,
            app_state=app_state,
            tool_use_id=request.tool_use_id,
            browser_session_id=request.browser_session_id,
        )

    # Spot-data tools — direct Finalysis passthroughs so Nova can
    # answer instant "what's X trading at?" / "pre-market levels for X"
    # questions in her own voice without the specialist pipeline
    # (no Session B spin-up, no visor, no chart, no report). See
    # src/tools/get_quote.py + src/tools/get_premarket.py for the
    # decision tree that distinguishes these from handoff_to_specialist.
    if tool_name == "get_quote":
        return await get_quote_handler(
            tool_input=request.tool_input,
            app_state=app_state,
            tool_use_id=request.tool_use_id,
            browser_session_id=request.browser_session_id,
        )

    if tool_name == "get_premarket":
        return await get_premarket_handler(
            tool_input=request.tool_input,
            app_state=app_state,
            tool_use_id=request.tool_use_id,
            browser_session_id=request.browser_session_id,
        )

    if tool_name == "read_current_report":
        # Nova's MANDATORY grounding tool. Returns the authoritative
        # snapshot of the report currently on the visor, or a
        # NO_REPORT result when nothing has been rendered yet in this
        # process. Nova's prompt forbids her from quoting any number
        # or trend about a chart on the visor without calling this
        # first — context-window HANDBACK_BRIEFs accumulate across
        # handoffs and cannot be relied on as single source of truth.
        #
        # This is a read-only cheap call (~1 ms): one dict lookup.
        snapshot = getattr(app_state, "current_report", None)
        if not snapshot:
            return {
                "ok": False,
                "code": "NO_REPORT",
                "message": (
                    "No report has been rendered yet. Tell the presenter "
                    "the report is not loaded and offer to retry — do NOT "
                    "substitute training-data guesses."
                ),
            }
        return {"ok": True, "report": snapshot}

    raise HTTPException(
        status_code=400,
        detail=f"Unknown Session A tool: {tool_name!r}",
    )


async def _dispatch_session_b(
    request: ToolCallRequest, app_state: Any,
) -> dict[str, Any]:
    """Route a Session B tool call to the correct handler in
    :mod:`src.tools.session_b`.

    Requires the request to carry ``agent_id`` — the Node session
    manager knows which specialist it spawned Session B for and passes
    it on every tool_call.
    """
    if not request.agent_id:
        raise HTTPException(
            status_code=400,
            detail="session_id='B' requires agent_id in the request body",
        )

    handler = SESSION_B_HANDLERS.get(request.tool_name)
    if handler is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Session B tool: {request.tool_name!r}",
        )

    # ── render_report idempotency guard ─────────────────────
    #
    # If the same agent calls render_report a SECOND time within the
    # same handoff, short-circuit to the cached result from the first
    # call instead of invoking the renderer again. The second call
    # happens when the specialist's LLM loops on the final phase
    # ("compose_summary → render_report" repeated); each re-invocation
    # rewrites the same HTML file to disk, which fires chokidar in the
    # visor and replays the phase overlay animation over a report the
    # user is already looking at — the user-visible "force refresh".
    #
    # The cached result preserves ``trigger_handback: True`` so the
    # Node session manager's existing branch still fires the handback
    # timer after the (first) successful render. See
    # ``src/specialists/toolkits/shared.py::render_report`` for the
    # other half of this change.
    if request.tool_name == "render_report":
        render_cache = getattr(app_state, "b_last_render_result", None)
        if (render_cache is not None
                and request.agent_id in render_cache):
            cached = render_cache[request.agent_id]
            logger.info(
                "render_report idempotent short-circuit for agent=%s "
                "(returning cached result, no file write)",
                request.agent_id,
            )
            return {**cached, "idempotent_replay": True}

    try:
        result = await handler(
            tool_input=request.tool_input,
            app_state=app_state,
            agent_id=request.agent_id,
            tool_use_id=request.tool_use_id,
            browser_session_id=request.browser_session_id,
        )
    except KeyError as exc:
        # Registry lookup missed (agent_id not registered or toolkit
        # not attached) → 400 rather than 500 so the session manager
        # can surface a useful error to Session A.
        raise HTTPException(status_code=400, detail=str(exc))

    # Pipeline progress tracker. When ``render_report`` succeeds we
    # record that for the current handoff so ``/cancel_session_tools``
    # can tell "pipeline completed" apart from "pipeline aborted". See
    # (internal postmortem 2026-05-09) § 7 P0-#1.
    if (
        request.tool_name == "render_report"
        and isinstance(result, dict)
        and result.get("ok") is True
    ):
        tracker = getattr(app_state, "b_pipeline_reached_render", None)
        if tracker is not None:
            tracker[request.agent_id] = True
            logger.info(
                "b_pipeline_reached_render[%s]=True (render_report ok)",
                request.agent_id,
            )
        # Stash the result so a duplicate render_report in the same
        # handoff short-circuits to this payload (see
        # render_report idempotency guard above).
        render_cache = getattr(app_state, "b_last_render_result", None)
        if render_cache is not None:
            render_cache[request.agent_id] = dict(result)

    # ── Pipeline capture for current_report grounding ─────────
    #
    # Accumulate structured slices from each Session B tool so that on
    # ``render_report`` success we can promote a complete, queryable
    # snapshot into ``app_state.current_report`` — the authoritative
    # source of truth for Nova's ``read_current_report`` grounding
    # tool. Skip failed tool results (ok=False) so a partial/aborted
    # pipeline cannot corrupt the slot.
    #
    # This runs in addition to — not instead of — the HANDBACK_BRIEF
    # that the Node session manager assembles for Session A's context.
    # The brief is "seed context"; the current_report slot is the
    # "query the truth" endpoint Nova must call before narrating.
    if isinstance(result, dict) and result.get("ok") is True:
        import datetime as _dt
        capture_store = getattr(app_state, "b_pipeline_capture", None)
        if capture_store is not None:
            capture = capture_store.setdefault(request.agent_id, {})
            ti = request.tool_input or {}
            if request.tool_name == "fetch_data":
                capture["fetch"] = {
                    "ticker": ti.get("symbol"),
                    "kind": ti.get("kind"),
                    "indicator": ti.get("indicator"),
                    "window": ti.get("window"),
                    "start_date": ti.get("start_date"),
                    "end_date": ti.get("end_date"),
                    "count": result.get("count"),
                }
            elif request.tool_name == "compose_summary":
                # ``stats`` carries first/last/high/low/pct_change — the
                # exact numbers Nova hallucinates when grounding is weak.
                capture["summary"] = {
                    "stats": result.get("stats"),
                    "bullets": list(result.get("bullets") or []),
                    "description": result.get("description"),
                    "customer_name": result.get("customer_name"),
                }
            elif request.tool_name == "generate_chart":
                capture["chart"] = {
                    "url": result.get("chart_url"),
                    "title": ti.get("title") or ti.get("chart_title"),
                }

        # Promote on render_report success. Note: this runs AFTER the
        # per-agent capture update above has already added the
        # render_report slice, but render_report's own fields are on
        # ``result`` itself — we don't need to stash them separately.
        if request.tool_name == "render_report":
            capture = (
                capture_store.get(request.agent_id, {})
                if capture_store is not None else {}
            )
            fetch = capture.get("fetch") or {}
            summary = capture.get("summary") or {}
            chart = capture.get("chart") or {}
            snapshot = {
                "agent_id": request.agent_id,
                # Core render_report output — customer-facing fields.
                "slug": result.get("slug"),
                "path": result.get("path"),
                "customer_name": result.get("customer_name"),
                "description": result.get("description"),
                "chart_url": result.get("chart_url") or chart.get("url"),
                "chart_title": result.get("chart_title") or chart.get("title"),
                "bullets": list(
                    result.get("bullets") or summary.get("bullets") or []
                ),
                "report_date": result.get("report_date"),
                # Pipeline-enrichment fields — the anti-hallucination
                # grounding data. ``stats`` is the critical one; Nova
                # must quote ONLY these numbers, never from training.
                "ticker": fetch.get("ticker"),
                "window": fetch.get("window"),
                "kind": fetch.get("kind"),
                "indicator": fetch.get("indicator"),
                "date_range": (
                    {"start": fetch.get("start_date"),
                     "end": fetch.get("end_date"),
                     "count": fetch.get("count")}
                    if fetch.get("start_date") or fetch.get("end_date")
                    else None
                ),
                "stats": summary.get("stats"),
                "rendered_at": _dt.datetime.now(
                    _dt.timezone.utc
                ).isoformat(timespec="seconds"),
            }
            app_state.current_report = snapshot
            logger.info(
                "current_report updated agent=%s slug=%s ticker=%s "
                "stats=%s bullets=%d",
                request.agent_id, snapshot.get("slug"),
                snapshot.get("ticker"),
                "yes" if snapshot.get("stats") else "no",
                len(snapshot.get("bullets") or []),
            )
            # Clear the capture slot — the next handoff starts fresh.
            if capture_store is not None:
                capture_store.pop(request.agent_id, None)

    return result


# ─────────────────────────────────────────────────────────────
# Cancellation + handoff release
# ─────────────────────────────────────────────────────────────


@app.post("/cancel_session_tools")
async def cancel_session_tools_endpoint(
    session_id: str = Query("B", description="Session whose tools to cancel"),
    reason: Optional[str] = Query(
        None,
        description=(
            "Handback reason forwarded by the Node session manager. "
            "Used to decide whether the visor overlay should end with "
            "'Generación interrumpida' (abnormal reasons) or simply "
            "dismiss (graceful reasons like end_session / terminator)."
        ),
    ),
    http_request: Request = None,
):
    """Cancel every in-flight tool call for the given session.

    Called by the Node session manager on EVERY handback (barge-in,
    terminator, end_session, stream error, timeout) so a slow Finalysis
    or Sonnet call can't race the next handoff.

    Visor signalling is branched on the handback ``reason`` **and** on
    whether ``render_report`` completed for this handoff (tracked in
    ``app.state.b_pipeline_reached_render``).

    - **Abnormal** (``barge_in``, ``b_stream_error``, ``b_stream_end``,
      ``b_timeout``, ``b_pipeline_stall``): call
      ``visor.aborted(reason)`` so the client shows
      "Generación interrumpida". The presenter's request was genuinely
      cut short — the overlay would otherwise sit frozen on the last
      phase because ``asyncio.CancelledError`` bypasses the toolkit's
      error branches.
    - **Graceful, pipeline completed** (``end_session`` / ``terminator``
      / ``assistant_name_hail`` / unknown reason **and**
      ``b_pipeline_reached_render[agent_id]`` is ``True``): call
      ``visor.done()`` so the overlay dismisses cleanly. The report
      that ``render_report`` produced is already on screen (via the
      chokidar ``report-ready`` event).
    - **Graceful, pipeline incomplete** (same reasons, but
      ``render_report`` never completed for this handoff): escalate to
      ``visor.aborted(reason="incomplete_pipeline")``. This is the fix
      for the 2026-05-09 airline-chart incident where Carlos called
      ``end_session`` right after ``fetch_data`` and the visor snapped
      back to the cold-boot welcome copy ("Esperando el primer
      reporte…") — a demo-breaking contradiction with what the
      presenter had just asked for. See
      ``(internal postmortem 2026-05-09)``.

    Best-effort: VisorClient swallows transport errors.
    """
    app_state = http_request.app.state
    cancelled: list[str] = []
    prefix = f"{session_id}:"
    for key, task in list(app_state.in_flight_tools.items()):
        if key.startswith(prefix) and not task.done():
            task.cancel()
            cancelled.append(key)
    if session_id == "B":
        # Reasons that mean 'the presenter did not get what they asked for
        # through no fault of their own' → loud visor state.
        ABNORMAL_REASONS = {
            "barge_in",
            "b_stream_error",
            "b_stream_end",
            "b_timeout",
            # Pipeline-stall watchdog (see session-manager.js P1): Carlos
            # started narrating but never advanced through the 6-tool
            # pipeline within SESSION_B_PIPELINE_STALL_MS. Treat as
            # abnormal so the visor paints "Generación interrumpida"
            # rather than dismissing silently — the audience saw a
            # frozen loader, they deserve to see an explicit "aborted".
            # See (internal postmortem 2026-05-08) § 7 P4.
            "b_pipeline_stall",
        }

        # Pipeline progress tracker. ``True`` if ``render_report`` fired
        # ok:true during the current handoff. Reset in
        # ``handoff_to_specialist``.
        tracker = getattr(app_state, "b_pipeline_reached_render", {})
        # The agent that was active for this handoff, if any. We don't
        # get it on the cancel endpoint (Node doesn't pass agent_id),
        # so we infer: if ANY agent reached render → treat as graceful.
        # This is safe because only one agent can be active at a time
        # (single browser WS), and the tracker is reset on each new
        # handoff so stale entries can't leak.
        reached_render = bool(tracker) and any(tracker.values())

        if reason in ABNORMAL_REASONS and not reached_render:
            # Abnormal reason AND the pipeline never produced a
            # report → the presenter's request was genuinely cut
            # short. Paint "Generación interrumpida" so the audience
            # isn't misled by a cold-boot welcome copy.
            effective = reason
            path = "abnormal"
            await_coro = app_state.visor.aborted(reason=reason)
        elif reason in ABNORMAL_REASONS and reached_render:
            # Abnormal reason BUT render_report already succeeded —
            # the report is visible on screen and the handback is
            # just a post-hoc cleanup (e.g. pipeline-stall watchdog
            # firing because the specialist looped after success
            # instead of calling end_session). The shared-toolkit
            # ``render_report`` path already fired ``visor.done()``
            # when the file was written, so issuing it again here
            # races with the client's ``report-ready`` → iframe-swap
            # sequence (2026-05-13 incident: visor reverted to the
            # cold-boot "Esperando el primer reporte" copy because
            # the second ``generating-done`` re-armed the dismiss
            # timer after the iframe was already loaded). Leave the
            # visor signalling to the render path; here we only
            # classify the transition for the session manager.
            effective = f"{reason}/post_render"
            path = "graceful-after-render"
            await_coro = None
        elif not reached_render:
            # Graceful handback but ``render_report`` never completed.
            # Escalate so the visor paints "Generación interrumpida"
            # instead of reverting to the cold-boot welcome copy. See
            # postmortem 2026-05-09 § RC-1 / § 7 P0-#1.
            effective = "incomplete_pipeline"
            path = "escalated-to-abnormal"
            await_coro = app_state.visor.aborted(reason="incomplete_pipeline")
        else:
            # Happy path: graceful handback AFTER render_report
            # succeeded. Same reasoning as the
            # ``graceful-after-render`` branch — the overlay was
            # already dismissed by ``render_report``'s ``visor.done()``
            # and the client has already loaded the iframe via the
            # chokidar ``report-ready`` event. Duplicating the call
            # here used to cause the visor to flash back to the
            # welcome copy (see 2026-05-13 incident).
            effective = reason or "-"
            path = "graceful"
            await_coro = None

        logger.info(
            "cancel_session_tools session=%s reason=%s path=%s "
            "effective=%s cancelled=%d reached_render=%s",
            session_id, reason or "-", path, effective,
            len(cancelled), reached_render,
        )

        try:
            if await_coro is not None:
                await await_coro
        except Exception as exc:   # noqa: BLE001
            logger.debug(
                "visor signalling during cancel failed "
                "(reason=%s path=%s): %s",
                reason, path, exc,
            )

        # Clear the tracker now that the handoff is over, so a stale
        # ``True`` from a previous successful handoff can't mislead the
        # next cancel — ``handoff_to_specialist`` also resets it at the
        # start of a new handoff, but double-clearing is harmless and
        # keeps the state tight.
        if isinstance(tracker, dict):
            tracker.clear()

        # Fix A (2026-05-09): return the classification so the Node
        # session manager can branch HANDBACK_NOTICE correctly. Before
        # this, handback() hardcoded "el reporte está en pantalla" for
        # every graceful reason — a lie when Carlos called end_session
        # without rendering a report. Nova would then tell the user
        # their report was ready when it wasn't. See
        # (internal postmortem 2026-05-09).
        return {
            "cancelled": cancelled,
            "reached_render": reached_render,
            "path": path,
            "effective": effective,
            "reason": reason or "-",
        }
    else:
        logger.info(
            "cancel_session_tools session=%s reason=%s cancelled=%d",
            session_id, reason or "-", len(cancelled),
        )

    return {"cancelled": cancelled}


@app.post("/internal/handoff_released")
async def handoff_released_endpoint(
    agent_id: Optional[str] = Query(None),
    http_request: Request = None,
):
    """Decrement the handoff concurrency counter after the session
    manager finishes a handback."""
    app_state = http_request.app.state
    app_state.handoff_rate.release(agent_id=agent_id)
    return {"status": "ok", "snapshot": app_state.handoff_rate.snapshot()}


@app.post("/internal/handoff_reset")
async def handoff_reset_endpoint(http_request: Request):
    """Wipe all handoff rate-limiter state (sliding window, concurrency,
    session total).

    Admin / demo-recovery only. Useful when a failed handoff (e.g.
    ``EMPTY_TRANSFORM`` on a bad ticker) consumed the per-window budget
    and the presenter needs to retry immediately rather than wait out
    ``NOVA_HANDOFF_WINDOW_S``. No auth — bound to ``127.0.0.1`` by
    uvicorn, so not exposed externally.
    """
    app_state = http_request.app.state
    before = app_state.handoff_rate.snapshot()
    app_state.handoff_rate.reset()
    after = app_state.handoff_rate.snapshot()
    logger.info("handoff_rate.reset called: before=%r after=%r", before, after)
    return {"status": "ok", "before": before, "after": after}


# ─────────────────────────────────────────────────────────────
# Registry reflection
# ─────────────────────────────────────────────────────────────


@app.get("/registry/ids")
async def registry_ids_endpoint(http_request: Request):
    return {"ids": http_request.app.state.registry.ids()}


@app.get("/registry/catalog")
async def registry_catalog_endpoint(
    locale: str = Query("en", description="Two-letter locale tag"),
    http_request: Request = None,
):
    """Rendered catalog block for Session A's prompt injection."""
    return {
        "locale": locale,
        "catalog": http_request.app.state.registry.describe_for_prompt(locale),
    }


@app.get("/registry/current_report")
async def registry_current_report_endpoint(http_request: Request):
    """The AUTHORITATIVE current-report snapshot for Nova's grounding tool.

    Returns whatever ``render_report`` most recently stored into
    ``app.state.current_report``. This is the ONLY truth Nova should
    quote from when narrating numbers, prices, percentages, or trends
    about the report on the visor.

    Response shape when a report is present::

        {
          "ok": true,
          "report": {
            "agent_id": "financial",
            "slug": "tsla-6m",
            "ticker": "TSLA",
            "window": "6m",
            "customer_name": "Tesla Inc.",
            "description": "...",
            "chart_url": "https://...",
            "chart_title": "...",
            "bullets": ["...", "...", ...],
            "stats": {
              "first_value": 424.16,
              "last_value": 370.85,
              "high": 464.70,
              "low": 370.85,
              "pct_change": -12.57,
              "count": 83
            },
            "date_range": {"start": "2025-11-12", "end": "2026-05-12", "count": 83},
            "rendered_at": "2026-05-12T15:49:00+00:00",
            "report_date": "2026-05-12",
            "path": "/.../reports/tsla-6m-2026-05-12.html"
          }
        }

    Response shape when no report has been rendered yet in this process::

        {"ok": false, "code": "NO_REPORT",
         "message": "No report has been rendered yet in this session."}

    Never raises. Always returns 200. Nova's prompt teaches her to
    handle the ``ok=false`` branch by saying the report isn't loaded
    yet rather than substituting training-data guesses.
    """
    snapshot = getattr(http_request.app.state, "current_report", None)
    if not snapshot:
        return {
            "ok": False,
            "code": "NO_REPORT",
            "message": "No report has been rendered yet in this session.",
        }
    return {"ok": True, "report": snapshot}


@app.get("/registry/{agent_id}")
async def registry_agent_endpoint(agent_id: str, http_request: Request):
    try:
        agent = http_request.app.state.registry.agent(agent_id)
    except KeyError:
        raise HTTPException(status_code=404,
                             detail=f"Unknown specialist: {agent_id!r}")
    return {
        "id": agent.id,
        "display_name": agent.display_name,
        "description": agent.description,
        "voice_id": agent.voice_id,
        "locale": agent.locale,
        "visor_phases": list(agent.visor_phases),
        "terminator_phrases": list(agent.terminator_phrases),
        "tool_defs": list(agent.tool_defs),
        "system_prompt_path": str(agent.system_prompt_path),
        "report_template_path": str(agent.report_template_path),
        "typical_duration_seconds": agent.typical_duration_seconds,
        "concurrency_limit": agent.concurrency_limit,
    }


# ─────────────────────────────────────────────────────────────
# Aggregate /diagnose
# ─────────────────────────────────────────────────────────────


@app.get("/diagnose")
async def diagnose_endpoint(http_request: Request):
    """Aggregate health snapshot: PPT + Chrome + visor + chart MCP +
    Bedrock + Finalysis + handle store + rate limiter + registry."""
    from src.platform import powerpoint as ppt
    app_state = http_request.app.state

    ppt_state = await asyncio.to_thread(ppt.diagnose)

    async def _safe(label: str, coro):
        try:
            return await coro
        except Exception as exc:   # noqa: BLE001
            return {"ok": False, "label": label, "error": str(exc)}

    chrome_state = await _safe("chrome", app_state.chrome.health_check())
    visor_state = await _safe("visor", app_state.visor.health_check())
    chart_state = await _safe("antv_chart", app_state.antv_chart.health_check())
    finalysis_state = await _safe("finalysis", app_state.finalysis.health_check())
    # Bedrock: explicitly shallow because health_check pings three models.
    # Always reachable in tests via the fake client; may be slow in prod.
    bedrock_state = await _safe("bedrock", app_state.bedrock_router.health_check())

    # WindowManager snapshot — surfaces the persisted slide_checkpoint
    # and the in-memory was_fullscreen_before_visor flag so a simple
    # ``curl /diagnose | jq .window_manager`` tells the whole story of
    # "what slide will we navigate back to" without the operator having
    # to shell-read ``.slide_checkpoint.json``.
    wm = getattr(app_state, "window_manager", None)
    if wm is not None:
        wm_state = await _safe("window_manager", wm.diagnose())
        # wm.diagnose() already includes its own nested powerpoint +
        # chrome snapshots; strip those to avoid duplicating the
        # top-level fields we already returned.
        if isinstance(wm_state, dict):
            wm_state = {
                k: v for k, v in wm_state.items()
                if k not in ("powerpoint", "chrome")
            }
    else:
        wm_state = {"ok": False, "error": "window_manager not initialised"}

    return {
        "powerpoint": ppt_state,
        "chrome": chrome_state,
        "visor": visor_state,
        "chart_mcp": chart_state,
        "finalysis": finalysis_state,
        "bedrock": bedrock_state,
        "handles": await app_state.data_handles.stats(),
        "handoff_rate": app_state.handoff_rate.snapshot(),
        "window_manager": wm_state,
        "registry": {
            "ids": app_state.registry.ids(),
            "count": len(app_state.registry),
        },
    }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _task_key(request: ToolCallRequest) -> str:
    """Make a stable string key for :data:`in_flight_tools`.

    Shape: ``"<session_id>:<tool_name>:<tool_use_id>"`` so
    /cancel_session_tools can prefix-match on ``"B:"``.
    """
    return f"{request.session_id}:{request.tool_name}:{request.tool_use_id or '-'}"


# ─────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="nova-sonic-agentic-co-presenter API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,  # override the module-scope init (see top of file)
    )

    # Pin region for lifespan.
    app.state.region = args.region
    # Eager init for direct-import callers (matches presenterassistant).
    global bedrock_client
    bedrock_client = boto3.client("bedrock-runtime", region_name=args.region)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
