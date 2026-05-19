"""PresentationAgentApp — entry point for the Presentation Assistant Agent.

Built on bedrock-agentcore's ``BedrockAgentCoreApp``, which provides:

* ``@app.entrypoint`` decorator for the main invocation handler
* Custom HTTP routes via the underlying Starlette/FastAPI layer
* Built-in AgentCore runtime integration for deployment

Tasks covered
-------------
7.1  Create ``src/agent_app.py`` using ``BedrockAgentCoreApp``
7.2  ``@app.entrypoint`` handler — load slides, build tool def, start session
7.3  System prompt builder with slide count + analyze_slide instructions
7.4  ``POST /slide_update`` custom HTTP route with bounds validation
7.5  Wire analyze_slide to tool_use events (one tool_result per tool_use)
7.6  Session teardown — release all references for GC
7.7  SigV4 via AWS credentials (no hardcoded keys), HTTPS only

Security (Requirement 9)
------------------------
* ``NovaSonicSession`` authenticates via ``EnvironmentCredentialsResolver``
  (SigV4) — no hardcoded keys.
* The boto3 vision client uses the default credential chain (SigV4, HTTPS).
* All Bedrock endpoints are HTTPS by default.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import boto3
from bedrock_agentcore.app import BedrockAgentCoreApp
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.models import SessionConfig, SlideData
from src.nova_sonic_session import NovaSonicSession
from src.slide_store import SlideStore
from src.tools.analyze_slide import (
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    analyze_slide,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Task 7.1: Create app using BedrockAgentCoreApp
# ------------------------------------------------------------------ #
app = BedrockAgentCoreApp()

# Module-level state shared between the entrypoint and /slide_update route.
_slide_store: SlideStore = SlideStore()
_sonic_session: Optional[NovaSonicSession] = None


# ------------------------------------------------------------------ #
# Task 7.3: System prompt builder (Property 10)
# ------------------------------------------------------------------ #


def build_system_prompt(slide_count: int) -> str:
    """Build the system prompt for Nova Sonic.

    The prompt MUST include (Property 10 / Requirement 7.2):
    * the total slide count
    * instructions to use the ``analyze_slide`` tool
    * instructions to be a concise presentation companion

    Args:
        slide_count: Total number of slides in the loaded deck.

    Returns:
        A system prompt string ready for Nova Sonic.
    """
    return (
        f"You are a concise presentation companion assisting a live presenter. "
        f"The presentation has {slide_count} slides. "
        f"When the presenter asks about slide content, talking points, or anything "
        f"visual on a slide, use the {TOOL_NAME} tool to analyze the current slide. "
        f"Keep your spoken responses brief — 1 to 4 sentences unless the presenter "
        f"asks for more detail. Be helpful, accurate, and stay grounded in what the "
        f"slides actually show."
    )


def build_tool_definition() -> dict:
    """Build the tool definition dict for Nova Sonic registration.

    Returns:
        A dict matching the Nova Sonic ``toolConfiguration.tools[]`` schema.
    """
    return {
        "toolSpec": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": {
                "json": json.dumps(TOOL_INPUT_SCHEMA),
            },
        }
    }


# ------------------------------------------------------------------ #
# Task 7.2: @app.entrypoint handler
# ------------------------------------------------------------------ #


@app.entrypoint
async def handle_invocation(request: Dict[str, Any]) -> Any:
    """Main invocation handler (Requirement 7.1–7.5).

    Expects *request* with:
    * ``slides`` — list of dicts with ``index``, ``image_base64``,
      ``speaker_notes``
    * ``config`` — optional dict matching :class:`SessionConfig` fields
    * ``region`` — optional AWS region string (default ``"us-east-1"``)

    The handler:
    1. Loads slides into :data:`_slide_store`.
    2. Builds the system prompt and tool definition.
    3. Opens a :class:`NovaSonicSession`.
    4. Streams audio and routes tool calls until the session ends.
    5. Tears down all resources on exit (task 7.6).
    """
    global _slide_store, _sonic_session

    # -- Parse slides -------------------------------------------------- #
    raw_slides = request.get("slides", [])
    if not raw_slides:
        return {"error": "No slides provided"}

    slides = [
        SlideData(
            index=s.get("index", i),
            image_base64=s["image_base64"],
            speaker_notes=s.get("speaker_notes", ""),
        )
        for i, s in enumerate(raw_slides)
    ]

    # Load into SlideStore (resets index and cache).
    _slide_store = SlideStore()
    _slide_store.load_slides(slides)

    # -- Parse config -------------------------------------------------- #
    config_data = request.get("config", {})
    config = SessionConfig(**config_data) if config_data else SessionConfig()
    region = request.get("region", "us-east-1")

    # -- Build system prompt and tool definition ----------------------- #
    system_prompt = build_system_prompt(_slide_store.total_slides)
    tool_def = build_tool_definition()

    # -- Start Nova Sonic session -------------------------------------- #
    _sonic_session = NovaSonicSession(config=config, region=region)
    await _sonic_session.start_session(system_prompt, tool_def)

    # Lazy boto3 vision client — uses default credential chain (SigV4,
    # HTTPS) with no hardcoded keys (task 7.7 / Requirement 9.1, 9.4).
    bedrock_vision_client = boto3.client("bedrock-runtime", region_name=region)

    try:
        # -- Stream audio and route tool calls (tasks 7.2, 7.5) -------- #
        async for event_type, payload in _sonic_session.process_responses():
            if event_type == "tool_use":
                # Task 7.5: exactly one tool_result per tool_use
                # (Property 7 / Requirement 4.4).
                tool_result = analyze_slide(
                    slide_store=_slide_store,
                    tool_input=payload.get("content", {}),
                    bedrock_client=bedrock_vision_client,
                    vision_model_id=config.vision_model_id,
                )
                await _sonic_session.send_tool_result(
                    tool_use_id=payload.get("content_id", ""),
                    result=tool_result,
                )
            elif event_type == "audio":
                # Requirement 7.4: stream audio bytes to presenter.
                yield payload
            elif event_type == "session_end":
                break

            # Renew session if approaching the 8-minute limit.
            await _sonic_session.check_and_renew()

    finally:
        # Task 7.6: session teardown.
        await _teardown()


# ------------------------------------------------------------------ #
# Task 7.6: Session teardown
# ------------------------------------------------------------------ #


async def _teardown() -> None:
    """Release all session resources for garbage collection.

    Clears references to:
    * the :class:`NovaSonicSession` (and its audio buffers)
    * the :class:`SlideStore` (slide images, speaker notes, cache)

    After this call, all large objects become eligible for GC
    (Requirement 7.6).
    """
    global _sonic_session, _slide_store

    if _sonic_session is not None:
        try:
            await _sonic_session.end_session()
        except Exception as exc:
            logger.error("Error ending sonic session: %s", exc)
        _sonic_session = None

    # Replace with a fresh empty store so image/notes references are freed.
    _slide_store = SlideStore()

    logger.info("Session teardown complete — all resources released")


# ------------------------------------------------------------------ #
# Task 7.4: POST /slide_update custom HTTP route
# ------------------------------------------------------------------ #


@app.route("/slide_update", methods=["POST"])
async def handle_slide_update(request: Request) -> JSONResponse:
    """Accept slide-index updates from the keyboard hook.

    Validates that ``slide_index`` is an integer within the loaded deck's
    bounds and updates :data:`_slide_store.current_index`.

    Returns:
        200 with ``{"status": "ok", "slide_index": <int>}`` on success.
        400 with ``{"error": "<reason>"}`` on validation failure.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400
        )

    slide_index = body.get("slide_index")

    # Reject non-int values (including booleans).
    if not isinstance(slide_index, int) or isinstance(slide_index, bool):
        return JSONResponse(
            {"error": "slide_index must be an integer"}, status_code=400
        )

    try:
        _slide_store.set_current_index(slide_index)
    except (IndexError, ValueError) as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=400
        )

    return JSONResponse({"status": "ok", "slide_index": slide_index})
