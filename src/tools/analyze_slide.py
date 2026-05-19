"""``analyze_slide`` tool — Claude-Sonnet-powered slide analysis for Nova Sonic.

When the presenter asks Nova Sonic to explain the current slide, the handler:

1. **Syncs** the SlideStore with PowerPoint's actual current slide (fast
   AppleScript read, ~50-100 ms) so the vision layer never answers about a
   stale slide.
2. **Checks the cache** keyed on ``(slide_index, normalised_query)``. On a
   hit, returns the cached text without calling Bedrock.
3. **Prioritises speaker notes**: if the slide's notes are substantial
   (≥ ``NOTES_THRESHOLD`` characters after stripping), skips the vision call
   entirely and uses a text-only Claude call with the notes as the primary
   context — faster, cheaper, and the presenter's own words come through.
4. **Falls back to vision** when notes are missing or too short — sends the
   slide image + any short notes to Claude Sonnet via the Converse API.
5. **Caches successful results** only. Fallback/error messages are NOT cached
   so a retry can hit the real model.

Never raises back into the Nova Sonic stream — all failure paths resolve to
:data:`FALLBACK_MESSAGE` so the voice session stays alive.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from src.models import SlideData
from src.slide_store import SlideStore
from src.slide_sync import resync_slide_store

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------- #
# Constants
# ----------------------------------------------------------------------- #

FALLBACK_MESSAGE = "Unable to analyze this slide right now"
# 2026-05-18 — reduced from 512. Both the notes preamble (1-4
# sentences) and the spatial preamble (2-5 sentences) ask for short,
# presentation-ready answers — typically ~120-180 tokens. A 256-token
# cap leaves comfortable headroom while letting the model terminate
# its stop_reason ~10-25% sooner than the previous 512 cap on
# longer-running outputs. No quality regression in practice; if a
# specific use case ever needs more, override per-call via the
# explicit ``max_tokens=`` parameter on call_claude_*.
DEFAULT_MAX_TOKENS = 256
DEFAULT_TIMEOUT_SECONDS = 15

# Speaker-notes path activates when notes.strip() has >= this many characters.
# 80 chars is roughly one long sentence — below this, notes are too thin
# to answer a meaningful question and we need the image instead.
NOTES_THRESHOLD = 80

# Tool descriptor consumed by the agent app when registering with Nova Sonic.
TOOL_NAME = "analyze_slide"
TOOL_DESCRIPTION = (
    "Analyze or explain the current slide. Use ONLY when the presenter "
    "explicitly asks to describe, explain, or answer a question about a slide."
)
TOOL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What to analyze: describe, talking_points, or a specific question"
            ),
        }
    },
    "required": ["query"],
}

# Brief system-style preamble prepended to every prompt.
_PROMPT_PREAMBLE = (
    "You are a co-presenter helping deliver a live presentation. "
    "Extract key points, insights, and talking points. Interpret — do not just "
    "describe. Be concise and presentation-ready (1-4 sentences). "
    "NEVER mention slide numbers or say 'this slide' / 'slide N' — present "
    "the content naturally as if speaking to the audience."
)

_NOTES_PRIORITY_PREAMBLE = (
    "The presenter has written speaker notes for THIS slide. Treat those "
    "notes as the authoritative source — they contain the message the "
    "presenter wants to deliver. Summarise, rephrase, or answer the query "
    "using the notes as your primary context. Ignore anything that isn't "
    "grounded in the notes."
)

_SPATIAL_PROMPT_PREAMBLE = (
    "You are a co-presenter helping deliver a live presentation. "
    "The presenter is asking about a SPECIFIC REGION or ELEMENT of this slide. "
    "Describe the content at the requested location precisely. "
    "Reference spatial positions (top, bottom, left, right) when relevant. "
    "Be thorough about the specific area asked about (2-5 sentences). "
    "NEVER mention slide numbers or say 'this slide' — present the content "
    "naturally as if speaking to the audience."
)

# Keywords that signal the query requires visual/spatial analysis of the image.
#
# 2026-05-18 — tightened to POSITION-only words. The earlier list also
# contained pure object nouns ("chart", "graph", "table", "diagram",
# "image", "picture", "figure", "icon", "logo", "arrow", "color",
# "shape", "layout", "box") which forced the vision path on every
# "explain the chart"-style query, even when 1000+ chars of speaker
# notes already answered the question. The vision path costs
# ~3-5 s extra (image upload + vision-mode inference) so this was a
# silent latency drag on roughly half of slide-explanation queries.
#
# What changed:
# - Position words (genuinely require seeing the slide to identify
#   the region) → KEPT.
# - Object nouns (often described verbatim in speaker notes; vision
#   is only needed if the notes don't cover them, in which case the
#   length-threshold guard ``len(notes) >= NOTES_THRESHOLD`` already
#   routes to vision automatically) → DROPPED.
#
# The user's actual spatial queries — "explain the LEFT chart",
# "explain the TOP 3 boxes" — still match a position word and still
# get the vision path. Pure-noun queries — "explain the chart",
# "describe the diagram" — now get the fast notes path when notes
# are substantial. Net: same interpretability, ~3-5 s faster on the
# common case.
_SPATIAL_KEYWORDS = frozenset({
    # English position / spatial-locator words
    "top", "bottom", "left", "right", "above", "below",
    "upper", "lower", "middle", "center", "side", "edge", "corner",
    # Spanish position / spatial-locator words
    "arriba", "abajo", "izquierda", "derecha", "superior", "inferior",
    "centro", "medio", "lado", "borde", "esquina",
})


def _requires_vision(query: str) -> bool:
    """Return True if the query references spatial/visual elements."""
    tokens = set(re.split(r"\W+", query.lower()))
    return bool(tokens & _SPATIAL_KEYWORDS)


# ----------------------------------------------------------------------- #
# Prompt construction
# ----------------------------------------------------------------------- #


def build_vision_prompt(slide: SlideData, total_slides: int, query: str, *, spatial: bool = False) -> str:
    """Prompt used when the vision model receives the slide image.

    Always includes:
      * the preamble (spatial-aware if the query references positions),
      * the slide position (``Slide N of M.``),
      * the speaker notes verbatim if present,
      * the user query.
    """
    preamble = _SPATIAL_PROMPT_PREAMBLE if spatial else _PROMPT_PREAMBLE
    parts = [
        preamble,
        f"Slide {slide.index + 1} of {total_slides}.",
    ]
    if slide.speaker_notes and slide.speaker_notes.strip():
        parts.append(f"Speaker notes: {slide.speaker_notes}.")
    parts.append(f"User query: {query}")
    return "\n".join(parts)


def build_notes_prompt(slide: SlideData, total_slides: int, query: str) -> str:
    """Prompt used when speaker notes are substantial and we skip vision.

    The notes are inlined verbatim so Claude can rephrase them directly.
    """
    return "\n".join([
        _PROMPT_PREAMBLE,
        _NOTES_PRIORITY_PREAMBLE,
        f"Slide {slide.index + 1} of {total_slides}.",
        "Speaker notes (authoritative):",
        slide.speaker_notes.strip(),
        "",
        f"User query: {query}",
    ])


# ----------------------------------------------------------------------- #
# Bedrock Converse API calls
# ----------------------------------------------------------------------- #


def call_claude_vision(
    bedrock_client: Any,
    model_id: str,
    image_base64: str,
    prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    media_format: str = "png",
) -> str:
    """Invoke Claude via Bedrock Converse with a slide image + text prompt."""
    image_bytes = base64.b64decode(image_base64)

    response = bedrock_client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": media_format, "source": {"bytes": image_bytes}}},
                    {"text": prompt},
                ],
            }
        ],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"]


def call_claude_text(
    bedrock_client: Any,
    model_id: str,
    prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Invoke Claude via Bedrock Converse with a text-only prompt (no image)."""
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"]


# Backwards-compat alias (tests import `call_nova_vision`).
call_nova_vision = call_claude_vision


# ----------------------------------------------------------------------- #
# Query normalization
# ----------------------------------------------------------------------- #


def normalize_query(query: str) -> str:
    """Normalise query for cache key: lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", query.strip().lower())


# ----------------------------------------------------------------------- #
# Tool entry point
# ----------------------------------------------------------------------- #


def analyze_slide(
    slide_store: SlideStore,
    tool_input: Dict[str, Any],
    bedrock_client: Optional[Any] = None,
    vision_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
) -> Dict[str, Any]:
    """Handle a Nova Sonic ``analyze_slide`` tool_use event.

    The function is intentionally total: it always returns a JSON-serialisable
    dict with ``slide_index``, ``total_slides``, and ``analysis``. Any failure
    — missing/invalid query, Bedrock error, unexpected exception — resolves to
    :data:`FALLBACK_MESSAGE` so the Nova Sonic stream stays healthy.

    Routing rule:
        If the current slide has speaker notes ≥ :data:`NOTES_THRESHOLD`
        characters after stripping, a text-only Claude call is used with the
        notes as the primary context. Otherwise, Claude is called with the
        slide image (vision path).

    Args:
        slide_store: The shared SlideStore populated at session start.
        tool_input: Decoded JSON payload from Nova Sonic. Must contain a
            string ``query`` field.
        bedrock_client: Optional pre-configured ``bedrock-runtime`` client. If
            ``None``, one is created via :func:`boto3.client` using the
            default credential chain.
        vision_model_id: Bedrock model ID used for BOTH the vision and
            text-only paths (same Claude model).
    """
    # Snapshot position up front so the response and logs stay consistent
    # even if the keyboard hook advances the slide mid-call. Sync first so
    # we never reason about a stale current_index.
    resync_slide_store(slide_store)
    index = slide_store.current_index
    total = slide_store.total_slides

    # Guard against malformed tool input.
    query = tool_input.get("query") if isinstance(tool_input, dict) else None
    if not isinstance(query, str) or not query:
        logger.warning("analyze_slide received invalid tool_input: %r", tool_input)
        return {
            "slide_index": index,
            "total_slides": total,
            "analysis": FALLBACK_MESSAGE,
        }

    norm_query = normalize_query(query)

    # Cache hit short-circuits the model entirely.
    cached = slide_store.get_cached_analysis(index, norm_query)
    if cached is not None:
        return {
            "slide_index": index,
            "total_slides": total,
            "analysis": cached,
        }

    slide = slide_store.get_current_slide()
    notes = (slide.speaker_notes or "").strip()
    use_notes_only = len(notes) >= NOTES_THRESHOLD and not _requires_vision(query)
    spatial = _requires_vision(query)

    try:
        if bedrock_client is None:
            bedrock_client = boto3.client("bedrock-runtime")

        if use_notes_only:
            # Text-only path — notes are substantial, skip the image.
            prompt = build_notes_prompt(slide, total, query)
            t0 = time.perf_counter()
            analysis = call_claude_text(
                bedrock_client=bedrock_client,
                model_id=vision_model_id,
                prompt=prompt,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.info(
                "analyze_slide slide=%d: notes path (notes=%d chars, "
                "latency=%dms, prompt_chars=%d)",
                index + 1, len(notes), latency_ms, len(prompt),
            )
        else:
            # Vision path — notes missing or too short, send the image.
            prompt = build_vision_prompt(slide, total, query, spatial=spatial)
            t0 = time.perf_counter()
            analysis = call_claude_vision(
                bedrock_client=bedrock_client,
                model_id=vision_model_id,
                image_base64=slide.image_base64,
                prompt=prompt,
                media_format=slide.image_format,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.info(
                "analyze_slide slide=%d: vision path (notes=%d chars, "
                "spatial=%s, latency=%dms, image_b64=%d, prompt_chars=%d)",
                index + 1, len(notes), spatial, latency_ms,
                len(slide.image_base64), len(prompt),
            )

        # Cache successful results only.
        slide_store.cache((index, norm_query), analysis)

        return {
            "slide_index": index,
            "total_slides": total,
            "analysis": analysis,
        }

    except (ClientError, ReadTimeoutError, EndpointConnectionError) as exc:
        logger.exception("Bedrock call failed for slide %d: %s", index + 1, exc)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        logger.exception("Unexpected error in analyze_slide slide %d: %s", index + 1, exc)

    # Fallback result — not cached so retries can hit the real model.
    return {
        "slide_index": index,
        "total_slides": total,
        "analysis": FALLBACK_MESSAGE,
    }
