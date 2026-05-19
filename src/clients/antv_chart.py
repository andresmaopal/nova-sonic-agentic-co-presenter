"""AntvChartClient — JSON-RPC client for the AntV chart MCP server.

The ``@antv/mcp-server-chart`` package runs as a subprocess on
``127.0.0.1:1122/mcp`` in streamable-HTTP mode (launched by
``scripts/ensure-chart.sh``). It exposes JSON-RPC methods
``tools/call`` with names like ``generate_line_chart``,
``generate_column_chart``, ``generate_waterfall_chart``, etc.

This client is the Python in-process replacement for
``gmb-presenter-demo/scripts/chart-call.sh`` — same protocol, just
async httpx instead of shell + curl + python3.

Successful calls return the chart image URL (``https://…``) from the
first content block. Any deviation (HTTP error, JSON-RPC ``error``
field, non-https URL, empty content) raises ``AntvChartError``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)


# Use "localhost" (not 127.0.0.1) so httpx's resolver can try both the
# AAAA (::1) and A (127.0.0.1) records. @antv/mcp-server-chart binds
# IPv6-only by default on macOS unless launched with `--host 0.0.0.0`;
# hitting the IPv4 literal then fails with ECONNREFUSED.  See the
# matching defence-in-depth in scripts/ensure-chart.sh.
DEFAULT_ENDPOINT = f"http://localhost:{os.environ.get('CHART_PORT', '1122')}/mcp"

# 2026-05-18 PM — DEFAULT_TIMEOUT lowered from 60 s → 18 s to align with
# the Node session-manager's SESSION_B_PIPELINE_STALL_MS = 25_000 ms
# watchdog. The earlier 60 s timeout meant a slow AntV upstream (typical
# cause: AntGroup-hosted image renderer overload) would block this client
# well past the 25 s watchdog window. The watchdog cancelled the tool
# call, the toolkit returned ``code=CANCELLED``, and the visor flipped
# to "generación interrumpida" with no useful error to the user.
#
# Setting the chart client to 18 s gives the chart pipeline ~7 s of
# buffer below the watchdog, so when AntV is genuinely slow (3 symbols
# × wide date range, peak load on the upstream renderer), this client
# raises ``AntvChartError`` cleanly and the toolkit's CHART_ERROR path
# narrates a graceful "hubo un problema con la gráfica" via Carlos's
# voice — vastly better UX than a silent watchdog cancellation.
#
# Override via env var ``NOVA_ANTV_TIMEOUT_S`` if you need a different
# value (e.g., dialled down for a stress test, or up for a one-off
# integration debugging session).
#
# Source incident: 2026-05-18 16:48 — TSLA/NVDA/PLTR 6-month
# comparison hit ``generate_chart duration=25009ms ok=false
# code=CANCELLED``, see logs/node.log.
DEFAULT_TIMEOUT = float(os.environ.get("NOVA_ANTV_TIMEOUT_S", "18"))

# AntV light-theme defaults aligned with the visor + report template.
#
# 2026-05-18 — switched from One-Dark-on-black to a WSJ-style palette on
# pure white. Earlier this same day we used the visor's ``--surface``
# token (``#F6F9FC``) for the chart background to match ``.chart-frame``,
# but that token is a very pale gray-blue (``rgb(246,249,252)``) that
# read as light gray against the white body. The hard requirement is now
# "all backgrounds white, no grayscale anywhere": both the chart canvas
# below AND the ``.chart-frame`` / ``--surface`` token in the template
# (see ``reports/templates/financial.html``) are pinned to ``#FFFFFF``.
# The palette below is anchored on the visor's existing CSS variables so
# multi-series legends harmonise with the surrounding chrome:
#
#   #0a4f8a  --accent        deep blue (primary line)
#   #2e8b57  --success       sea green
#   #c47a1f                  burnt amber (warm complement to deep blue)
#   #7d4f9c                  muted plum
#   #1e88e5  --accent-soft   medium blue
#   #cc4d3a                  muted brick red (declines / negatives)
#
# The 6-colour cap matches ``palette and keep the legend legible on a
# projector`` in ``financial.py`` (the symbol/window fan-out cap).
DEFAULT_PALETTE: list[str] = [
    "#0A4F8A", "#2E8B57", "#C47A1F", "#7D4F9C", "#1E88E5", "#CC4D3A",
]
DEFAULT_BACKGROUND = "#FFFFFF"

# 2026-05-14 — narrow retry for transient upstream image-render errors.
# `@antv/mcp-server-chart` accepts the spec, validates it locally, then
# POSTs to an AntGroup-hosted renderer to produce the image URL. That
# upstream POST occasionally drops the connection (`socket hang up`,
# ECONNRESET, 5xx gateway). A single retry with ~1.5s backoff absorbs
# the blip while staying well under the Session B handoff watchdog
# budget. Schema/validation errors (`-32602` Invalid params, etc.) MUST
# NOT match here — we want those to surface fast.
_TRANSIENT_UPSTREAM_PATTERNS: tuple[str, ...] = (
    "socket hang up",
    "ECONNRESET",
    "ETIMEDOUT",
    "EPIPE",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
)
_TRANSIENT_RETRY_BACKOFF_S = 1.5


def _is_transient_upstream_error(error_obj: Any) -> bool:
    """Return True iff a JSON-RPC ``error`` body looks like a transient
    failure from the AntV upstream image-render service (``socket hang up``,
    ECONNRESET, 5xx gateway). Used to gate the single in-method retry —
    schema/validation errors are never treated as transient.
    """
    if not isinstance(error_obj, dict):
        return False
    message = str(error_obj.get("message", ""))
    return any(pat in message for pat in _TRANSIENT_UPSTREAM_PATTERNS)


class AntvChartError(RuntimeError):
    """Raised when the AntV MCP call fails to produce a usable chart URL."""


class AntvChartClient:
    """Async JSON-RPC client for the AntV chart MCP server."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._endpoint = endpoint or DEFAULT_ENDPOINT
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self._http: httpx.AsyncClient | None = None
        self._rpc_id = 0

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    # ─── Main entry point ────────────────────────────────────

    async def generate(
        self,
        *,
        tool: str,
        data: list[dict[str, Any]],
        title: str,
        axis_x_title: str | None = None,
        axis_y_title: str | None = None,
        theme: str = "default",
        width: int = 1200,
        height: int = 600,
        background_color: str = DEFAULT_BACKGROUND,
        palette: list[str] | None = None,
        extra_style: dict[str, Any] | None = None,
        extra_arguments: dict[str, Any] | None = None,
    ) -> str:
        """Render a chart and return its ``https://`` image URL.

        Args:
            tool: AntV tool name (``generate_line_chart``,
                ``generate_bar_chart``, ``generate_waterfall_chart``, …).
            data: AntV-shaped data array. Exact fields depend on ``tool``.
            title: Chart title (required).
            axis_x_title, axis_y_title: Optional axis labels.
            theme: Defaults to ``"default"`` (AntV/G2 light theme) to
                match the visor + report template, which both use a
                single white surface (``--bg:#ffffff``,
                ``--surface:#f6f9fc``). Pass ``"dark"`` only if you're
                generating a chart for an external dark surface.
            width, height: Pixels.
            background_color: Applied via ``style.backgroundColor``.
                Default is ``#FFFFFF`` (pure white) — the report template
                pins ``.chart-frame`` and the ``--surface`` design token
                to the same value, so the chart edge becomes invisible
                and there is no gray boundary anywhere on the page.
            palette: Series palette. Default is a WSJ-style 6-colour
                set anchored on the visor's accent tokens (deep blue,
                sea green, burnt amber, plum, medium blue, brick red).
            extra_style: Merged into the ``style`` dict. Use this for
                per-tool overrides (e.g., waterfall's
                ``positiveColor``/``negativeColor``/``totalColor``).
            extra_arguments: Merged into the tool ``arguments`` dict for
                tool-specific options (e.g., ``innerRadius`` for pie/donut).

        Returns:
            The raw ``https://…`` image URL from the MCP response.

        Raises:
            AntvChartError: On HTTP error, JSON-RPC error, missing content,
                or non-https URL.
        """
        style: dict[str, Any] = {
            "backgroundColor": background_color,
            "palette": palette if palette is not None else list(DEFAULT_PALETTE),
        }
        if extra_style:
            style.update(extra_style)

        arguments: dict[str, Any] = {
            "data": data,
            "theme": theme,
            "width": width,
            "height": height,
            "style": style,
        }
        # 2026-05-12 — keep title optional at the wire level. The zod
        # schema on the MCP server defaults title to "" when missing,
        # which G2 interprets as "no title band", reclaiming the
        # vertical space for the plot area. The report template draws
        # its own golden chart title in HTML (see
        # ``reports/templates/financial.html .chart-title``), so the
        # baked-in white title was a duplicate. Empty/None → omit.
        if title:
            arguments["title"] = title
        if axis_x_title is not None:
            arguments["axisXTitle"] = axis_x_title
        if axis_y_title is not None:
            arguments["axisYTitle"] = axis_y_title
        if extra_arguments:
            arguments.update(extra_arguments)

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }

        # Single retry on transient upstream errors. The MCP server forwards
        # `socket hang up` / ECONNRESET / 5xx from its image-render upstream
        # via the JSON-RPC `error` envelope — a true network blip, not a bad
        # spec. We already paid the cost of validating the chart payload, so
        # a second attempt is cheap. Schema errors (-32602 etc.) skip the
        # retry path so real bugs surface immediately. Connection errors to
        # *our* MCP server (httpx.RequestError) also bypass the retry —
        # those mean ensure-chart.sh isn't running and won't self-heal.
        client = await self._client()
        max_attempts = 2
        body: dict[str, Any] = {}
        latency_ms = 0
        for attempt in range(1, max_attempts + 1):
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    self._endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            except httpx.TimeoutException as exc:
                # 2026-05-18 PM — explicit timeout branch so the postmortem
                # log shows clearly which attempt timed out (vs. a connection
                # refused / DNS failure). The chart-MCP client timeout is
                # bounded below the Node session-manager's 25 s pipeline-
                # stall watchdog, so we surface this as a clean CHART_ERROR
                # (Carlos can narrate "hubo un problema con la gráfica")
                # instead of letting Node race-cancel us.
                latency_ms = round((time.perf_counter() - t0) * 1000)
                logger.info(
                    "antv: %s POST timed out after %dms on attempt %d "
                    "(timeout=%.1fs)",
                    tool, latency_ms, attempt, self._timeout,
                )
                raise AntvChartError(
                    f"AntV MCP timed out after {latency_ms}ms "
                    f"(timeout={self._timeout}s, tool={tool})"
                ) from exc
            except httpx.RequestError as exc:
                raise AntvChartError(
                    f"connection to AntV MCP failed (is ensure-chart.sh running?): {exc}"
                ) from exc

            latency_ms = round((time.perf_counter() - t0) * 1000)
            # 2026-05-18 PM — log every attempt's latency, not just the
            # final success line at the bottom of generate(). Without
            # this, a slow-then-cancelled run leaves no trace in the
            # logs of how long the call was actually in flight.
            logger.info(
                "antv: %s POST attempt %d/%d completed in %dms (status=%d)",
                tool, attempt, max_attempts, latency_ms, resp.status_code,
            )

            if resp.status_code >= 400:
                raise AntvChartError(
                    f"AntV MCP returned HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )

            try:
                body = resp.json()
            except ValueError as exc:
                raise AntvChartError(
                    f"AntV MCP returned non-JSON ({exc}): {resp.text[:300]}"
                ) from exc

            error_obj = body.get("error")
            if error_obj is None:
                break  # success path — fall through to content extraction

            if attempt < max_attempts and _is_transient_upstream_error(error_obj):
                logger.info(
                    "antv: transient upstream error on %s, retrying once: %r",
                    tool, error_obj,
                )
                await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_S)
                payload["id"] = self._next_id()
                continue

            raise AntvChartError(
                f"AntV MCP reported error: {error_obj!r}"
            )

        content = body.get("result", {}).get("content", [])
        if not content:
            raise AntvChartError(
                f"AntV MCP returned empty content: {body!r}"
            )

        text = content[0].get("text", "")
        if not text.startswith("https://"):
            raise AntvChartError(
                f"AntV MCP returned non-https URL: {text!r}"
            )

        # Defence against the CDN-eviction failure mode.
        #
        # 2026-05-18 incident: a TSLA/NVDA chart probed-and-passed at
        # generation time (HEAD → 200 image/jpeg) and the report was
        # written with the alipayobjects.com URL inlined. ~5 minutes
        # later the user opened the visor and saw a broken image —
        # the CDN had evicted the image (HEAD now returned 404). The
        # earlier ``_verify_chart_url`` probe only catches *creation-
        # time* CDN 404s, not post-generation eviction.
        #
        # Fix: after the probe confirms the image is reachable, GET
        # the bytes immediately and return them inlined as a
        # ``data:<content-type>;base64,<b64>`` URI. The report HTML
        # then carries the image bytes directly — fully self-contained,
        # no further CDN round-trips, immune to eviction. This does
        # inflate the report HTML by ~200-300 KB (typical chart PNG
        # size after base64), which is still tiny in absolute terms
        # and well under any practical threshold for chokidar /
        # visor / browser handling.
        try:
            inlined = await self._download_chart_as_data_uri(text)
        except AntvChartError:
            # Re-raise after logging so the caller sees CHART_ERROR and
            # can handback cleanly.
            raise

        logger.info(
            "antv %s → data: (latency=%dms, src=%s)",
            tool, latency_ms, text.split("/")[-1][:40],
        )
        return inlined

    async def _download_chart_as_data_uri(self, url: str) -> str:
        """HEAD-probe then GET the chart image and return a
        ``data:<content-type>;base64,<b64>`` URI so the bytes can be
        inlined directly into the report HTML.

        This is the eviction-proof replacement for the older
        ``_verify_chart_url`` that only HEAD-probed and returned the
        original URL — that path passed at generation time but allowed
        the report HTML to depend on the CDN keeping the URL alive
        forever, which it does not. See the call-site comment for the
        2026-05-18 incident that motivated this.

        The HEAD step is kept as a fast pre-check so we fail before
        paying for a multi-hundred-KB GET against an obviously broken
        URL. A single retry with a short backoff absorbs CDN
        propagation latency.

        Raises :class:`AntvChartError` on any unrecoverable failure
        (HEAD returned non-image, GET failed, GET body too large,
        etc.).
        """
        client = await self._client()
        last_status: int | None = None
        last_error: str | None = None
        content_type: str | None = None

        # ── 1. HEAD probe (kept for fast failure on broken URLs) ──
        for attempt in range(2):
            try:
                probe = await client.head(url, timeout=3.0, follow_redirects=True)
            except httpx.RequestError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.6)
                continue
            last_status = probe.status_code
            content_type = probe.headers.get("content-type", "")
            if 200 <= probe.status_code < 300 and content_type.startswith("image/"):
                break  # healthy
            if probe.status_code == 404 and attempt == 0:
                # Transient — CDN may still be propagating. Short backoff.
                await asyncio.sleep(0.6)
                continue
            # Any other non-image response: fail immediately.
            raise AntvChartError(
                "AntV-generated chart URL is not reachable as an image "
                f"(HEAD status={last_status}, last_error={last_error}, url={url})"
            )
        else:
            # Loop finished without breaking → both attempts failed.
            raise AntvChartError(
                "AntV-generated chart URL is not reachable as an image "
                f"(HEAD status={last_status}, last_error={last_error}, url={url})"
            )

        # ── 2. GET the bytes and inline them ──
        try:
            resp = await client.get(url, timeout=10.0, follow_redirects=True)
        except httpx.RequestError as exc:
            raise AntvChartError(
                f"AntV chart GET failed despite passing HEAD probe: "
                f"{type(exc).__name__}: {exc} (url={url})"
            ) from exc

        if not (200 <= resp.status_code < 300):
            raise AntvChartError(
                f"AntV chart GET returned HTTP {resp.status_code} after HEAD-200 "
                f"(eviction race? url={url})"
            )

        body_ct = resp.headers.get("content-type", content_type or "image/png")
        if not body_ct.startswith("image/"):
            raise AntvChartError(
                f"AntV chart GET returned non-image content-type "
                f"(got {body_ct!r}, url={url})"
            )

        body = resp.content
        if not body:
            raise AntvChartError(
                f"AntV chart GET returned empty body (url={url})"
            )

        # Sanity cap: refuse to inline more than 5 MB. Real charts at
        # 1200×600 PNG are ~150-300 KB; anything wildly larger is a
        # signal something is off (e.g. CDN serving HTML).
        if len(body) > 5 * 1024 * 1024:
            raise AntvChartError(
                f"AntV chart body too large to inline ({len(body)} bytes, url={url})"
            )

        b64 = base64.b64encode(body).decode("ascii")
        # Strip any charset/parameter suffix from the content-type so
        # the data URI mediatype stays canonical (e.g. "image/png" not
        # "image/png; charset=binary").
        mediatype = body_ct.split(";", 1)[0].strip()
        return f"data:{mediatype};base64,{b64}"

    # ─── Health check ────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Used by ``/diagnose``. An empty POST returns a fast 400 or 405;
        any response (other than a connection error) means the server is up."""
        t0 = time.perf_counter()
        try:
            client = await self._client()
            resp = await client.post(
                self._endpoint,
                json={},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=2.0,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            # Any non-connection response means the server is reachable.
            return {
                "ok": True,
                "reachable": True,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "endpoint": self._endpoint,
            }
        except Exception as exc:   # noqa: BLE001
            return {
                "ok": False,
                "reachable": False,
                "error": str(exc),
                "endpoint": self._endpoint,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }
