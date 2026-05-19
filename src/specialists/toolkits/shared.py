"""SharedToolkitMixin — generic defaults for the four domain-agnostic
Session B tools.

Three of Session B's six tools are genuinely domain-specific and must
be implemented by each :class:`SpecialistToolkit`:

- ``fetch_data``  (calls the domain's data source)
- ``transform_data``  (domain-specific shape)
- ``compute_stats``  (domain-specific stat set for the summary)

The other three — ``generate_chart``, ``compose_summary``, ``render_report``
— are genuinely shared across every specialist because the underlying
clients (``AntvChartClient``, ``BedrockRouterClient``, ``ReportRenderer``)
are domain-agnostic. A fourth tool, ``end_session``, is literally
boilerplate.

Specialists compose via multiple inheritance:

    class FinancialToolkit(SpecialistToolkit, SharedToolkitMixin):
        def __init__(self, finalysis): self.finalysis = finalysis
        async def fetch_data(self, *, params, ctx): ...
        async def transform_data(self, *, handle, target, ctx): ...
        async def compute_stats(self, *, handle, ctx): ...

This file is 100% generic — it contains no financial, legal, or
medical wording. Language/locale is pulled from
``ctx.specialist.locale`` on every call so mixing specialists in the
same process Just Works.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.clients.antv_chart import AntvChartError
from src.specialists.base import ToolContext


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Localized error/ack strings
# ─────────────────────────────────────────────────────────────

_MESSAGES: dict[str, dict[str, str]] = {
    "handle_not_found": {
        "es-419": "Handle inválido o expirado.",
        "en-US":  "Handle not found or expired.",
    },
    "chart_error": {
        "es-419": "El gráfico falló.",
        "en-US":  "Chart generation failed.",
    },
    "summary_error": {
        "es-419": "El resumen no se pudo generar.",
        "en-US":  "Summary generation failed.",
    },
    "summary_bad_count": {
        "es-419": "Se esperaban 3–5 viñetas.",
        "en-US":  "Expected 3–5 bullets.",
    },
    "render_error": {
        "es-419": "Error al escribir el reporte.",
        "en-US":  "Failed to write the report.",
    },
    "ack_done": {
        "es-419": "Reporte listo.",
        "en-US":  "Report ready.",
    },
}


# Projector-friendly chart defaults. The HTML report template renders
# the chart inside a flex 1fr row with ``object-fit: contain`` (see
# ``reports/templates/financial.html`` line 26), so the natural PNG
# pixel size controls label legibility on a fullscreen video beam.
#
# 2026-05-12 — "labels 2.5× larger" workaround for projector demos.
#
# The AntV MCP (``@antv/mcp-server-chart`` v0.9.x) exposes *only*
# ``backgroundColor``, ``palette``, ``texture``, and ``lineWidth`` via
# its ``style`` schema — no font-size knob for titles, axis ticks, or
# legend. The server's zod validator strips any unknown ``style.*``
# key, so custom G2 configs can't be pushed through from the client.
#
# Trick: render the PNG at ``1 / _PROJECTOR_LABEL_SCALE`` of the target
# display size and let the browser upscale it inside the
# ``.chart-frame`` flex container. AntV's default tick labels are
# pixel-fixed at ~12 px regardless of canvas, so the visible label
# size is ``12 × _PROJECTOR_LABEL_SCALE`` px after the browser upscale.
#
# 2026-05-18 — scale dropped from 2.5 → 1.5 (a 40% reduction in visible
# tick-label size, including the dense X-axis date labels on time-series
# charts that previously crowded the bottom of the chart on multi-symbol
# 3M comparisons). Canvas grew from 640×288 to 1067×480, which also
# means less raster upscale and a sharper PNG. The knob is GLOBAL —
# X-axis, Y-axis, and legend labels all shrink by the same 40%; AntV
# MCP doesn't expose per-axis font sizes so this is the only viable
# lever today.
#
#   scale 2.5 → labels ~30 px visible, lineWidth 2 → ~5 px visible
#   scale 1.5 → labels ~18 px visible, lineWidth 3 → ~4.5 px visible
#
# ``_PROJECTOR_LINE_WIDTH`` was bumped from 2 → 3 so the *visual*
# stroke thickness stays roughly the same after the upscale change;
# without that compensation, lines would visibly thin out as an
# unwanted side effect of the label shrink.
#
# Tradeoff: the PNG itself is raster-upscaled and looks mildly blurry
# on a 4K display. With the 1.5× scale the upscale is gentler and the
# blur is barely noticeable; the smaller source canvas also encourages
# the paired downsampling in ``financial.py``
# (``_CHART_MAX_POINTS_DEFAULT`` = 14) to not over-crowd the x-axis.
#
# If AntV MCP ever exposes ``style.axisLabel.fontSize`` etc., revert
# to a native 1600×720 canvas and pass explicit font sizes.
_PROJECTOR_LABEL_SCALE = 1.5
_PROJECTOR_CHART_WIDTH = int(round(1600 / _PROJECTOR_LABEL_SCALE))   # 1067
_PROJECTOR_CHART_HEIGHT = int(round(720 / _PROJECTOR_LABEL_SCALE))   # 480
_THICK_LINE_TOOLS: frozenset[str] = frozenset({
    "generate_line_chart",
    "generate_area_chart",
    "generate_dual_axes_chart",
})
# Pre-upscale stroke width. After the browser's 1.5× image upscale this
# is visually ≈4.5 px — close to the previous ≈5 px target so lines
# don't visibly thin out when ``_PROJECTOR_LABEL_SCALE`` changes.
_PROJECTOR_LINE_WIDTH = 3


def _localize(key: str, locale: str) -> str:
    """Return the ``key`` localized to ``locale`` with fallbacks."""
    bucket = _MESSAGES.get(key, {})
    if locale in bucket:
        return bucket[locale]
    # Try language-code prefix (e.g. "es").
    lang = (locale or "")[:2].lower()
    for loc, msg in bucket.items():
        if loc.lower().startswith(lang):
            return msg
    # Final fallback — English if we have it, else any message, else the key.
    if "en-US" in bucket:
        return bucket["en-US"]
    if bucket:
        return next(iter(bucket.values()))
    return key


# ─────────────────────────────────────────────────────────────
# SharedToolkitMixin
# ─────────────────────────────────────────────────────────────


class SharedToolkitMixin:
    """Generic implementations of the four cross-domain tools.

    Each method returns a JSON-safe dict. On success the ``ok`` flag is
    ``True`` and domain payload is included; on error ``ok`` is
    ``False`` with ``code`` + localized ``message``.
    """

    # ── generate_chart ───────────────────────────────────────

    async def generate_chart(
        self,
        *,
        handle: str,
        tool_name: str,
        title: str,
        axis_x_title: str | None = None,
        axis_y_title: str | None = None,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Render an AntV chart for the data behind ``handle``."""
        data = await ctx.get_handle(handle)
        if data is None:
            # Mark phase 2 as errored so the visor doesn't freeze at phase 1.
            await ctx.phase(
                2, substep=f"{tool_name} · handle-missing", status="error",
            )
            return {
                "ok": False,
                "code": "HANDLE_NOT_FOUND",
                "message": _localize("handle_not_found", ctx.specialist.locale),
            }
        # Defence in depth: AntV renders a blank image from an empty list
        # without raising, and the downstream summary+report pipeline has no
        # way to know the chart is void. Catch the zero-row case here and
        # fail loudly so the specialist narrates honestly and hands back.
        if isinstance(data, list) and len(data) == 0:
            await ctx.phase(
                2, substep=f"{tool_name} · empty-data", status="error",
            )
            return {
                "ok": False,
                "code": "CHART_EMPTY_DATA",
                "message": _localize("chart_error", ctx.specialist.locale),
            }
        try:
            extra_style: dict[str, Any] | None = None
            if tool_name in _THICK_LINE_TOOLS:
                extra_style = {"lineWidth": _PROJECTOR_LINE_WIDTH}
            # 2026-05-12 — do NOT bake the chart title into the PNG.
            # The report template already renders the same title above
            # the image in the golden accent colour (see
            # ``reports/templates/financial.html`` ``.chart-title``).
            # Keeping the baked-in white AntV title duplicated it and
            # ate vertical space from the plot area. Pass empty title
            # to AntV — the client omits the key from arguments so no
            # title band gets reserved. The original ``title`` kwarg
            # is still carried through the toolkit for logging /
            # phase-substep purposes and flows independently into the
            # HTML via ``render_report(chart_title=...)``.
            url = await ctx.antv_chart.generate(
                tool=tool_name,
                data=data,
                title="",
                axis_x_title=axis_x_title,
                axis_y_title=axis_y_title,
                width=_PROJECTOR_CHART_WIDTH,
                height=_PROJECTOR_CHART_HEIGHT,
                extra_style=extra_style,
            )
        except AntvChartError as exc:
            logger.info("shared toolkit: chart generation failed: %s", exc)
            # Mark phase 2 as errored so the visor reflects reality.
            await ctx.phase(
                2, substep=f"{tool_name} · failed", status="error",
            )
            return {
                "ok": False,
                "code": "CHART_ERROR",
                "message": _localize("chart_error", ctx.specialist.locale),
            }
        await ctx.phase(
            2,
            substep=(
                f"{tool_name} · {len(data)} pts · "
                f"{_PROJECTOR_CHART_WIDTH}×{_PROJECTOR_CHART_HEIGHT}"
            ),
        )
        # 2026-05-18 incident — DO NOT return the chart URL/data URI to
        # the model.
        #
        # Background: earlier today (a) the chart canvas grew from
        # 640×288 to 1067×480 (label-shrink change), and (b) the chart
        # client now returns an inlined ``data:image/png;base64,…`` URI
        # instead of an https URL (CDN-eviction fix). The combined
        # effect: the chart "URL" became a 400-800 KB string that Nova
        # Sonic had to (i) ingest as a tool result, then (ii) copy
        # verbatim into ``render_report``'s ``chart_url`` parameter. The
        # token-emission cost of copying ~1 MB of base64 verbatim
        # exceeded the Node session-manager's
        # SESSION_B_PIPELINE_STALL_MS=25 s watchdog → every report
        # generation was cancelled with reason=b_pipeline_stall *after*
        # compose_summary completed, so the loader sat at "Componiendo
        # resumen" for 25 s and the visor flipped to
        # "generación interrumpida". See logs/python.log around 08:19
        # and 08:20 on 2026-05-18.
        #
        # Fix: stash the URL/data URI in ``ctx.data_handles`` (the same
        # opaque-handle store already used for ``td-`` and ``fn-``
        # payloads) and return only a 11-char handle (``ch-<8hex>``) to
        # the model. ``render_report`` resolves the handle back to the
        # real URL just before invoking the report renderer. The model
        # never has to see — or copy — the chart bytes.
        chart_handle = await ctx.data_handles.put("ch", url)
        return {"ok": True, "chart_url": chart_handle, "tool_used": tool_name}

    # ── compose_summary ──────────────────────────────────────

    async def compose_summary(
        self,
        *,
        handle: str,
        context: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Produce 3–5 bullets in the specialist's locale via Sonnet.

        ``context`` is expected to include at least ``customer_name`` and
        ``description``. Stats are computed from ``handle`` via the
        concrete toolkit's ``compute_stats`` method so each specialist
        decides what numbers matter.
        """
        try:
            stats = await self.compute_stats(handle=handle, ctx=ctx)  # type: ignore[attr-defined]
        except Exception as exc:   # noqa: BLE001
            logger.exception("shared toolkit: compute_stats raised: %s", exc)
            await ctx.phase(3, substep="stats · failed", status="error")
            return {
                "ok": False,
                "code": "SUMMARY_ERROR",
                "message": _localize("summary_error", ctx.specialist.locale),
            }

        full_context = {**context, "stats": stats}
        resp = await ctx.bedrock_router.compose_summary(
            context_json=json.dumps(full_context, ensure_ascii=False),
        )
        if "error" in resp:
            logger.info("shared toolkit: Sonnet error: %s", resp.get("message"))
            await ctx.phase(3, substep="sonnet · failed", status="error")
            return {
                "ok": False,
                "code": "SUMMARY_ERROR",
                "message": _localize("summary_error", ctx.specialist.locale),
            }

        bullets = resp.get("bullets")
        # Bullet-count contract:
        #   Single series   → 3-5 bullets (legacy rule, unchanged).
        #   Multi-series    → (N+1) to min(N+2, 8) bullets. One per
        #                     series + one comparative is the target;
        #                     the extra bullet is an optional talking
        #                     point that Haiku may produce without
        #                     triggering a Sonnet fallback. Widened
        #                     2026-05-13 after the TSLA/NVDA stall —
        #                     the strict "exactly N+1" gate was the
        #                     main driver of Sonnet fallbacks.
        # See SUMMARY_SYSTEM in bedrock_router.py + _expected_bullet_range
        # for the single source of truth; this is the server-side
        # defence-in-depth check.
        series_count = None
        if isinstance(stats, dict):
            raw_sc = stats.get("series_count")
            if isinstance(raw_sc, int) and raw_sc >= 2:
                series_count = raw_sc
        if series_count is not None:
            expected_low = min(series_count + 1, 8)
            expected_high = min(series_count + 2, 8)
        else:
            expected_low, expected_high = 3, 5
        valid_count = (
            isinstance(bullets, list)
            and expected_low <= len(bullets) <= expected_high
        )
        if not valid_count:
            logger.info(
                "shared toolkit: bad bullet shape (len=%s, expected=[%d,%d], "
                "series_count=%r): raw=%r",
                len(bullets) if isinstance(bullets, list) else "N/A",
                expected_low, expected_high, series_count,
                resp.get("raw"),
            )
            await ctx.phase(3, substep="bullets · bad-count", status="error")
            return {
                "ok": False,
                "code": "SUMMARY_ERROR",
                "message": _localize("summary_bad_count", ctx.specialist.locale),
            }

        # Drop any entries that aren't non-empty strings (defensive).
        clean = [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
        if not (expected_low <= len(clean) <= expected_high):
            await ctx.phase(3, substep="bullets · bad-count", status="error")
            return {
                "ok": False,
                "code": "SUMMARY_ERROR",
                "message": _localize("summary_bad_count", ctx.specialist.locale),
            }

        await ctx.phase(
            3,
            substep=(
                f"{len(clean)} bullets"
                + (
                    f" · {int(resp['latency_ms'])} ms"
                    if isinstance(resp.get("latency_ms"), (int, float))
                    else ""
                )
            ),
        )
        # Echo back the numeric ``stats`` dict and the caller-provided
        # ``customer_name`` / ``description`` so the Node session
        # manager can passively capture them for the HANDBACK_BRIEF it
        # sends to Session A at handback.
        #
        # These three fields are pure mirrors of data the caller
        # already knows, so adding them costs nothing; the win is that
        # every downstream consumer (Node capture, Python cache,
        # logs) now gets a single self-describing payload instead of
        # having to correlate across call sites. See
        # ``(internal postmortem 2026-05-11)``
        # (forthcoming) for the architecture note.
        return {
            "ok": True,
            "bullets": clean,
            "stats": stats,
            "customer_name": context.get("customer_name"),
            "description":   context.get("description"),
            "latency_ms": resp.get("latency_ms"),
            "tokens": resp.get("tokens"),
        }

    # ── render_report ────────────────────────────────────────

    async def render_report(
        self,
        *,
        customer_name: str,
        description: str,
        chart_url: str,
        chart_title: str,
        bullets: list[str],
        slug: str,
        ctx: ToolContext,
        footer_note: str | None = None,
    ) -> dict[str, Any]:
        """Write the two-slide HTML using the specialist's template.

        Phase layout (6 phases after the 2026-05-13 loader change):
          0 Consultando Finalysis API
          1 Transformando series temporales
          2 Seleccionando y construyendo gráfica
          3 Componiendo resumen ejecutivo (Sonnet)
          4 Auditando resultados con Agente revisor  ← mock, emitted here
          5 Ensamblando reporte                       ← actual render
        """
        import asyncio as _asyncio
        import datetime as _dt

        # Mock audit phase — the "reviewer agent" is a visual beat in
        # the loader, not a real LLM call. It fires between summary
        # and render so the audience sees two short substeps tick by
        # before the report materialises. Keep sleeps short: the
        # overall pipeline budget is already tight (see
        # SESSION_B_PIPELINE_STALL_MS in session-manager.js) and the
        # visor SSE loop paints the substep update ≈ immediately.
        await ctx.phase(4, substep="verificando datasets")
        await _asyncio.sleep(0.4)
        await ctx.phase(4, substep="resumiendo")
        await _asyncio.sleep(0.4)

        # 2026-05-18 — resolve ``ch-…`` chart handles into the real
        # URL/data URI just before passing to the report renderer. The
        # model only ever sees the small handle (see
        # render_chart_for_handle for why); the renderer needs the full
        # URL to inline the bytes into the HTML ``<img src>``. Anything
        # that already looks like a real URL (https://, data:image/) is
        # passed through unchanged so legacy callers and the test suite
        # stay green.
        #
        # The ORIGINAL ``chart_url`` (handle or URL) is preserved in
        # ``chart_url_for_brief`` and echoed back in this tool's return
        # value. Reason: Nova Sonic processes the tool_result before
        # the trigger_handback cancellation lands; if we returned the
        # resolved data URI here, Carlos would have to ingest a 600 KB
        # base64 string before emitting his closing phrase, re-creating
        # the b_pipeline_stall regression at the *end* of the pipeline
        # instead of the middle. The Node session-manager already
        # passes whatever it sees through ``_cap(…, 160)`` for the
        # HANDBACK_BRIEF, so a short handle is strictly better than a
        # truncated data URI for tracing.
        chart_url_for_brief = chart_url
        if chart_url.startswith("ch-"):
            resolved = await ctx.data_handles.get(chart_url)
            if not resolved:
                logger.info(
                    "shared toolkit: render_report got unknown chart handle %r",
                    chart_url,
                )
                await ctx.phase(
                    5, substep=f"{slug} · chart handle expired", status="error",
                )
                return {
                    "ok": False,
                    "code": "RENDER_ERROR",
                    "message": _localize("render_error", ctx.specialist.locale),
                }
            chart_url = resolved

        today = _dt.date.today().isoformat()
        try:
            path = ctx.report_renderer.render(
                customer_name=customer_name,
                description=description,
                chart_url=chart_url,
                chart_title=chart_title,
                bullets=list(bullets),
                slug=slug,
                report_date=today,
                footer_note=(
                    footer_note
                    if footer_note is not None
                    else "Generado con Finalysis + AntV + Kiro"
                ),
                template_path=ctx.specialist.report_template_path,
            )
        except Exception as exc:   # noqa: BLE001 - broad on purpose here
            logger.exception("shared toolkit: render failed: %s", exc)
            await ctx.phase(5, substep=f"{slug} · failed", status="error")
            return {
                "ok": False,
                "code": "RENDER_ERROR",
                "message": _localize("render_error", ctx.specialist.locale),
            }

        # Include file size so the presenter can see "the report is
        # ~7 kB and it hit disk" in the final substep. Stat can fail
        # in exotic test setups — fall back to slug-only.
        try:
            size_kb = max(1, round(path.stat().st_size / 1024))
            substep = f"{slug} · {size_kb} kB"
        except OSError:
            substep = slug
        await ctx.phase(5, substep=substep)
        await ctx.visor.done()
        # Setting ``trigger_handback: True`` turns a successful
        # render_report into an implicit end_session from the Node
        # session manager's point of view. The report is already on
        # screen — there is no reason to wait for the specialist to
        # call end_session explicitly. The manager uses a longer
        # grace window (GRACE_AFTER_RENDER_COMPLETE_MS) to let the
        # specialist finish the terminator phrase before the handback
        # actually fires, so this doesn't clip audio.
        #
        # Why this matters: before this change the happy path relied
        # on Carlos calling end_session after the terminator phrase.
        # If he looped (dup compose_summary → dup render_report → no
        # end_session) the ONLY backstop was the 15 s pipeline-stall
        # watchdog, so the audience watched the report for 15 s in
        # silence before Nova returned. See
        # ``(internal postmortem 2026-05-10)``
        # (forthcoming) for the incident timeline.
        #
        # The extra echo fields (customer_name, chart_url, bullets,
        # …) are what the Node session manager accumulates into the
        # HANDBACK_BRIEF. They're the same values the caller passed
        # in, mirrored back so Node doesn't have to reconstruct them
        # from the tool_input in a separate code path. Kept a small
        # copy of ``bullets`` (pure list of strings) rather than a
        # reference because Python's dict lives longer than this
        # function's frame does.
        return {
            "ok": True,
            "trigger_handback": True,
            "path": str(path),
            "slug": slug,
            "customer_name": customer_name,
            "description": description,
            "chart_url": chart_url_for_brief,
            "chart_title": chart_title,
            "bullets": list(bullets),
            "report_date": today,
        }

    # ── end_session ──────────────────────────────────────────

    async def end_session(
        self, *, summary: str | None, ctx: ToolContext,
    ) -> dict[str, Any]:
        """Signal handback to the Node session manager.

        The session manager inspects ``trigger_handback`` in the tool
        result before forwarding it back to the model, and runs
        ``handback({reason: "end_session"})`` when it's True.
        """
        return {
            "ok": True,
            "trigger_handback": True,
            "message": summary or _localize("ack_done", ctx.specialist.locale),
        }
