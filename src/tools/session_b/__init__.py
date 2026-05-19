"""Session B tool-handler dispatchers.

These are **thin** — each one looks up the active specialist's
toolkit in the registry and delegates with the right argument names.
The Nova Sonic ``tool_use`` event is already validated by the JSON
schema declared in the specialist's ``AGENT.tool_defs``, so we don't
re-validate shapes here.

Every handler signature is:

    async def <name>_handler(
        *, tool_input: dict, app_state, agent_id: str,
        tool_use_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> dict

``app_state`` is the FastAPI ``request.app.state`` — it provides the
shared clients (``visor``, ``data_handles``, ``bedrock_router``,
``antv_chart``, ``report_renderer``, ``registry``) that every Session
B tool consumes via :class:`ToolContext`.

All handlers return a JSON-safe ``dict`` suitable for sending back to
Nova Sonic as a ``toolResult`` payload.
"""

from __future__ import annotations

import logging
from typing import Any

from src.specialists.base import (
    FetchResult,
    SpecialistToolkit,
    ToolContext,
    TransformResult,
)


logger = logging.getLogger(__name__)


__all__ = [
    "fetch_data_handler",
    "transform_data_handler",
    "generate_chart_handler",
    "compose_summary_handler",
    "render_report_handler",
    "end_session_handler",
    "SESSION_B_HANDLERS",
]


# ─────────────────────────────────────────────────────────────
# Shared plumbing
# ─────────────────────────────────────────────────────────────


def _build_ctx(
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None,
    browser_session_id: str | None,
) -> tuple[SpecialistToolkit, ToolContext]:
    """Look up the toolkit + build a :class:`ToolContext` bag.

    Raises :class:`KeyError` if ``agent_id`` isn't registered — the
    Python dispatcher in ``api_server.py`` catches that and returns a
    400 to the Node session manager.
    """
    agent = app_state.registry.agent(agent_id)
    toolkit = app_state.registry.toolkit(agent_id)
    ctx = ToolContext(
        agent_id=agent_id,
        specialist=agent,
        visor=app_state.visor,
        data_handles=app_state.data_handles,
        bedrock_router=app_state.bedrock_router,
        antv_chart=app_state.antv_chart,
        report_renderer=app_state.report_renderer,
        browser_session_id=browser_session_id,
        tool_use_id=tool_use_id,
    )
    return toolkit, ctx


def _as_dict(result: Any) -> dict[str, Any]:
    """Coerce a ``FetchResult`` / ``TransformResult`` / plain dict into a
    JSON-safe tool-result payload."""
    if isinstance(result, (FetchResult, TransformResult)):
        return result.to_dict()
    if isinstance(result, dict):
        return result
    return {"ok": False, "code": "INVALID_RESULT",
            "message": f"handler returned {type(result).__name__}"}


# ─────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────


async def fetch_data_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.fetch_data(params=tool_input)``."""
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)
    result = await toolkit.fetch_data(params=tool_input or {}, ctx=ctx)
    return _as_dict(result)


async def transform_data_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.transform_data(handle, target)``.

    Tool schema (per ``SpecialistAgent.tool_defs``):
        {"handle": str, "target": str, "series_label"?: str}
    """
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)
    handle = _required(tool_input, "handle")
    target = _required(tool_input, "target")
    if handle is None or target is None:
        return _bad_args("transform_data needs handle + target")
    result = await toolkit.transform_data(
        handle=handle, target=target, ctx=ctx,
    )
    return _as_dict(result)


async def generate_chart_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.generate_chart(handle, tool_name, title, ...)``.

    Tool schema:
        {"handle": str, "tool_name": str, "title": str,
         "axis_x_title"?: str, "axis_y_title"?: str}
    """
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)
    handle = _required(tool_input, "handle")
    tool_name = _required(tool_input, "tool_name")
    title = _required(tool_input, "title")
    if handle is None or tool_name is None or title is None:
        return _bad_args("generate_chart needs handle + tool_name + title")
    return await toolkit.generate_chart(
        handle=handle,
        tool_name=tool_name,
        title=title,
        axis_x_title=tool_input.get("axis_x_title"),
        axis_y_title=tool_input.get("axis_y_title"),
        ctx=ctx,
    )


async def compose_summary_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.compose_summary(handle, context)``.

    Tool schema:
        {"handle": str, "customer_name": str, "description": str,
         "narrative"?: str}
    """
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)
    handle = _required(tool_input, "handle")
    customer_name = _required(tool_input, "customer_name")
    description = _required(tool_input, "description")
    if handle is None or customer_name is None or description is None:
        return _bad_args("compose_summary needs handle + customer_name + description")

    # Build the context dict the mixin passes to Sonnet. Includes the
    # caller-supplied narrative when present; stats are computed by the
    # mixin from the handle so the LLM can't fabricate numbers.
    context = {
        "customer_name": customer_name,
        "description": description,
    }
    narrative = tool_input.get("narrative")
    if narrative:
        context["narrative"] = narrative
    return await toolkit.compose_summary(
        handle=handle, context=context, ctx=ctx,
    )


async def render_report_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.render_report(...)``.

    Tool schema:
        {"customer_name": str, "description": str,
         "chart_url": str, "chart_title": str,
         "bullets": [str], "slug": str}
    """
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)

    required = ("customer_name", "description", "chart_url",
                "chart_title", "bullets", "slug")
    missing = [k for k in required if tool_input.get(k) in (None, "")]
    if missing:
        return _bad_args(f"render_report missing: {missing}")

    bullets = tool_input["bullets"]
    if not isinstance(bullets, list):
        return _bad_args("bullets must be a list of strings")

    return await toolkit.render_report(
        customer_name=str(tool_input["customer_name"]),
        description=str(tool_input["description"]),
        chart_url=str(tool_input["chart_url"]),
        chart_title=str(tool_input["chart_title"]),
        bullets=list(bullets),
        slug=str(tool_input["slug"]),
        ctx=ctx,
    )


async def end_session_handler(
    *,
    tool_input: dict,
    app_state: Any,
    agent_id: str,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Call ``toolkit.end_session(summary)``.

    The shared mixin returns ``{ok: True, trigger_handback: True, ...}``
    which the Node session manager inspects to fire ``handback({reason:
    "end_session"})``. Tool input is optional:

        {"summary"?: str}
    """
    toolkit, ctx = _build_ctx(app_state, agent_id, tool_use_id, browser_session_id)
    summary = None
    if isinstance(tool_input, dict):
        raw = tool_input.get("summary")
        if isinstance(raw, str) and raw.strip():
            summary = raw.strip()
    return await toolkit.end_session(summary=summary, ctx=ctx)


# ─────────────────────────────────────────────────────────────
# Dispatch table — consumed by src/api_server.py
# ─────────────────────────────────────────────────────────────

SESSION_B_HANDLERS: dict[str, Any] = {
    "fetch_data":       fetch_data_handler,
    "transform_data":   transform_data_handler,
    "generate_chart":   generate_chart_handler,
    "compose_summary":  compose_summary_handler,
    "render_report":    render_report_handler,
    "end_session":      end_session_handler,
}


# ─────────────────────────────────────────────────────────────
# Tiny helpers
# ─────────────────────────────────────────────────────────────


def _required(tool_input: dict, key: str) -> Any:
    """Return ``tool_input[key]`` if it's truthy (or zero), else ``None``."""
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get(key)
    if value is None or value == "":
        return None
    return value


def _bad_args(message: str) -> dict[str, Any]:
    return {"ok": False, "code": "BAD_ARGS", "message": message}
