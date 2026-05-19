"""BedrockRouterClient — async port of the ``bedrock-router`` MCP server.

Routes LLM sub-tasks to specific Amazon Bedrock models based on the
task's complexity / latency profile:

- **Nova Lite 2**  — trivial intent classification (JSON extraction).
  Fastest TTFT, cheapest, multilingual. ~200–500 ms.
- **Claude Haiku 4.5** — deterministic JSON↔JSON transformation where
  schema adherence matters more than prose. ~400–900 ms.
- **Claude Sonnet 4.6** — nuanced ``es-419`` executive-summary bullets.
  Quality here materially drives report quality. ~1.5–3.5 s.

Identical routing + system prompts as ``mcp-servers/bedrock-router/server.py``
— just wrapped in ``asyncio.to_thread`` around boto3's synchronous
``converse`` API so it plays nicely with the FastAPI event loop.

Errors never raise — each method returns ``{"error": ..., "latency_ms":
..., "model_id": ...}`` on failure so Session B can narrate a short
apology in Spanish and call ``end_session``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Default model IDs (overridable via env)
# ─────────────────────────────────────────────────────────────

DEFAULT_NOVA_LITE = "us.amazon.nova-2-lite-v1:0"
DEFAULT_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_SONNET = "us.anthropic.claude-sonnet-4-6"
# Summary model: Haiku is 3-4× faster than Sonnet for bullet generation
# with negligible quality loss. Override with NOVA_SUMMARY_MODEL env var.
DEFAULT_SUMMARY = DEFAULT_HAIKU

# Per-model latency ceilings for compose_summary. When a _converse call
# exceeds these budgets the task is cancelled (asyncio.wait_for) and
# compose_summary falls through to the Sonnet fallback.
#
# Rationale (2026-05-13 postmortem): Haiku occasionally tails to 25+ s
# under Bedrock load, blowing past Session B's 25 s pipeline-stall
# watchdog and killing the whole handoff with "Generación interrumpida".
# Observed median Haiku latency for compose_summary is 3-5 s, so a 10 s
# cap is generous (~2× the 95th percentile) without masking genuine
# slowness. If Haiku ever genuinely needs >10 s we'd rather eat the
# 6-8 s Sonnet round-trip than ride the unbounded tail.
#
# Sonnet's 18 s budget keeps the whole compose_summary ceiling at
# 10 + 18 = 28 s on the worst-case double-fallback path. The Session B
# pipeline-stall watchdog is 25 s (see SESSION_B_PIPELINE_STALL_MS in
# websocket-server/session-manager.js) which catches anything above —
# but we never expect to get there because either Haiku returns
# promptly (5 s) or the timeout hits at 10 s and Sonnet resolves in
# 6-8 s → 16-18 s total, inside the watchdog.
#
# Overridable via env for operators who want to tune for a different
# model mix (e.g., running Sonnet as primary with no fallback).
HAIKU_PRIMARY_TIMEOUT = float(os.environ.get(
    "NOVA_COMPOSE_SUMMARY_PRIMARY_TIMEOUT_S", "10.0",
))
SONNET_FALLBACK_TIMEOUT = float(os.environ.get(
    "NOVA_COMPOSE_SUMMARY_FALLBACK_TIMEOUT_S", "18.0",
))


# ─────────────────────────────────────────────────────────────
# System prompts (verbatim from the MCP server)
# ─────────────────────────────────────────────────────────────

CLASSIFY_SYSTEM = """You extract structured intent from financial-analysis queries.
Always respond with ONLY a JSON object, no prose. Keys:
  symbol         — uppercase ticker or null
  symbols        — array of tickers for multi-symbol comparisons, or null
  indicator      — one of: sma, ema, rsi, macd, bollinger, obv, vwap, none
  start_date     — ISO YYYY-MM-DD or null
  end_date       — ISO YYYY-MM-DD or null
  kind           — one of: symbol_indicator, comparison, screener, quote, catalyst, raw
  time_phrase    — raw phrase the user used (e.g. "últimos 6 meses")
If the user writes in Spanish, interpret Spanish company names (Amazon→AMZN, Microsoft→MSFT, Apple→AAPL, etc.)."""


TRANSFORM_SYSTEM = """You convert Finalysis API responses into AntV chart data.
Respond with ONLY a JSON array (no prose, no code fences). Each element must
be an object with the fields the caller specifies in the task description.
Respect keys and casing exactly. Do NOT invent values — if a value is null in
the source, drop that point."""


SUMMARY_SYSTEM = """Eres un analista financiero senior. Escribes viñetas de
resumen ejecutivo para una audiencia C-suite en español latinoamericano (es-419).

═══════════════════════════════════════════════════════════════
CONTEO DE VIÑETAS (PRIMERA PRIORIDAD — REGLA ABSOLUTA)
═══════════════════════════════════════════════════════════════
Caso normal (una sola serie): devuelve EXACTAMENTE 3, 4 o 5 viñetas.
NUNCA 0, 1, 2, 6, 7 ni más en el caso de una sola serie.

Caso COMPARACIÓN MULTI-SERIE: si el input incluye el campo
``stats.series`` (diccionario de dos o más series, p. ej.
``{"AMZN": {...}, "MSFT": {...}}`` o ``{"ema_20": {...}, "ema_50": {...}}``),
entonces devuelve entre ``series_count + 1`` y ``series_count + 2``
viñetas (con un tope duro de 8). Objetivo natural: ``series_count + 1``
(una por serie + una comparativa final); la viñeta extra es un
talking-point opcional si el dato lo amerita.

  - UNA viñeta por cada serie, en el orden en que aparecen en
    ``stats.series``. Cada viñeta autocontenida describe el movimiento
    de SU serie (dirección, magnitud, niveles clave) con las cifras
    específicas del input (first, last, high, low, pct_change de esa
    serie — NO inventes).
  - UNA viñeta comparativa FINAL que ponga las series en contexto
    una contra otra (quién lideró, divergencia, correlación visible,
    implicación forward-looking).
  - OPCIONAL: UNA viñeta adicional con un talking-point de contexto
    (volumen, tendencia macro, evento relevante visible en los
    datos) sólo si el dato de verdad lo amerita. Si dudas, omítela.

Ejemplos del conteo esperado:
  series_count=2 → 3 o 4 viñetas (2 por-serie + 1 comparativa [+1 opcional])
  series_count=3 → 4 o 5 viñetas
  series_count=4 → 5 o 6 viñetas
  series_count=5 → 6 o 7 viñetas
  series_count=6 → 7 u 8 viñetas
  series_count≥7 → máximo 8 viñetas (prioriza las series con mayor
                    movimiento + 1 comparativa)

Si el input tiene pocas estadísticas, AÚN ASÍ genera el conteo
requerido: desglosa dirección + magnitud, contexto de volumen/rango,
e implicación — siempre hay material.

Un array fuera del rango requerido se considera RESPUESTA INVÁLIDA y
será rechazado, rompiendo el pipeline y dejando al presentador sin
reporte en vivo. Cuenta antes de cerrar el JSON.
═══════════════════════════════════════════════════════════════

Requisitos de contenido:
- Cada viñeta autocontenida y autoexplicativa.
- Cada viñeta DEBE incluir cifras específicas del input (precios, %, volúmenes).
- Destaca valores atípicos, tendencias, divergencias y una implicación forward-looking.
- Tono profesional, conciso, sin adjetivos vacíos.
- NO inventes datos. Si falta un dato, omítelo — pero genera el conteo requerido.
- Si la narrativa del usuario sugiere causación (p. ej. "por la guerra en X"),
  sé honesto: si los datos no la respaldan, dilo explícitamente.
- En comparaciones multi-serie, menciona cada serie POR SU NOMBRE
  exacto (ticker o label del campo series) — nunca "la primera" /
  "la segunda".

Formato de salida:
Responde ÚNICAMENTE con un array JSON de strings (sin prosa, sin fences,
sin explicación previa, sin comentarios posteriores). Cada string es una
viñeta en HTML-safe plain text (sin <li>, sin HTML). El primer carácter
de tu respuesta debe ser ``[`` y el último debe ser ``]``."""


# ─────────────────────────────────────────────────────────────
# BedrockRouterClient
# ─────────────────────────────────────────────────────────────


class BedrockRouterClient:
    """Async wrapper over ``bedrock-runtime.converse`` with model routing."""

    def __init__(
        self,
        *,
        region: str | None = None,
        nova_lite_model_id: str | None = None,
        haiku_model_id: str | None = None,
        sonnet_model_id: str | None = None,
        summary_model_id: str | None = None,
        client: Any = None,
    ) -> None:
        self.region = region or os.environ.get("AWS_REGION") \
            or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self.nova_lite_id = nova_lite_model_id or os.environ.get(
            "BEDROCK_NOVA_LITE_MODEL_ID", DEFAULT_NOVA_LITE)
        self.haiku_id = haiku_model_id or os.environ.get(
            "BEDROCK_HAIKU_MODEL_ID", DEFAULT_HAIKU)
        self.sonnet_id = sonnet_model_id or os.environ.get(
            "BEDROCK_SONNET_MODEL_ID", DEFAULT_SONNET)
        self.summary_id = summary_model_id or os.environ.get(
            "NOVA_SUMMARY_MODEL", DEFAULT_SUMMARY)

        if client is not None:
            self._boto = client
        else:
            config = Config(
                retries={"max_attempts": 2, "mode": "standard"},
                read_timeout=30,
                connect_timeout=5,
            )
            self._boto = boto3.client(
                "bedrock-runtime", region_name=self.region, config=config,
            )

    # ─── Public entry points ─────────────────────────────────

    async def classify_intent(
        self, *, query: str, today_iso: str | None = None,
    ) -> dict[str, Any]:
        """Extract structured intent from a user query (Nova Lite 2).

        Returns::

            {
              "parsed":     {...} | None,    # parsed JSON object
              "raw":        "<model output>",
              "model_id":   "...",
              "latency_ms": <int>,
              "tokens":     {"input": N, "output": N},
            }

        On Bedrock error, returns ``{"error": ..., "message": ..., "model_id": ...}``.
        """
        user_msg = f"Today: {today_iso or 'unknown'}\nQuery: {query}"
        resp = await self._converse(
            model_id=self.nova_lite_id,
            system=CLASSIFY_SYSTEM,
            user=user_msg,
            max_tokens=400,
            temperature=0.0,
        )
        if "error" in resp:
            return resp
        parsed = _try_json(resp["text"])
        return {
            "parsed": parsed,
            "raw": resp["text"],
            "model_id": resp["model_id"],
            "latency_ms": resp["latency_ms"],
            "tokens": {
                "input": resp.get("input_tokens"),
                "output": resp.get("output_tokens"),
            },
        }

    async def transform_data(
        self, *, task_description: str, source_json: str,
    ) -> dict[str, Any]:
        """Deterministic JSON→JSON transformation (Claude Haiku 4.5).

        Returns::

            {
              "data":       [...] | None,
              "raw":        "<model output or None>",
              "model_id":   "...",
              "latency_ms": <int>,
              "tokens":     {"input": N, "output": N},
            }
        """
        user_msg = f"TASK:\n{task_description}\n\nSOURCE:\n{source_json}"
        resp = await self._converse(
            model_id=self.haiku_id,
            system=TRANSFORM_SYSTEM,
            user=user_msg,
            max_tokens=8000,
            temperature=0.0,
        )
        if "error" in resp:
            return resp
        parsed = _try_json(resp["text"])
        return {
            "data": parsed,
            "raw": resp["text"] if parsed is None else None,
            "model_id": resp["model_id"],
            "latency_ms": resp["latency_ms"],
            "tokens": {
                "input": resp.get("input_tokens"),
                "output": resp.get("output_tokens"),
            },
        }

    async def compose_summary(self, *, context_json: str) -> dict[str, Any]:
        """3–5 es-419 executive-summary bullets (or ``series_count+1`` for
        multi-series comparisons; see SUMMARY_SYSTEM).

        Uses ``NOVA_SUMMARY_MODEL`` (default: Haiku for speed). Falls
        back to Sonnet in two cases:

        1. Primary model returned a bedrock error ("error" in resp).
        2. Primary model returned parseable JSON but the array didn't
           have the expected bullet count — the downstream toolkit
           rejects anything outside the range, so we re-ask a more
           capable model with a second chance before surfacing a
           user-visible failure. Single-series expects 3-5; multi-series
           expects ``series_count + 1`` (capped at 8). The expected
           count is derived from ``stats.series_count`` in
           ``context_json``.

        The fallback is skipped when the primary model IS Sonnet (no
        point re-asking the same model) or when no Sonnet ID is
        configured.

        Returns::

            {
              "bullets":    [...] | None,    # on success: 3-5 or series_count+1
              "raw":        "<model output or None>",
              "model_id":   "...",
              "latency_ms": <int>,
              "tokens":     {"input": N, "output": N},
              "retried":    bool,            # True if we fell back to Sonnet
              "retry_reason": "error" | "bad_count" | "timeout" | None,
            }

        Fallback triggers:
          * ``"error"``    — primary returned a Bedrock service error.
          * ``"timeout"``  — primary exceeded HAIKU_PRIMARY_TIMEOUT
            (default 10 s). The in-flight call is cancelled and
            Sonnet is asked instead. Added 2026-05-13 to bound
            Haiku's latency tail and keep compose_summary inside
            Session B's 25 s pipeline-stall watchdog.
          * ``"bad_count"`` — primary returned valid JSON but the
            bullet count was outside the expected window (see
            ``_expected_bullet_range``).
        """
        retry_reason: str | None = None

        # Derive expected bullet count window from the context's
        # stats.series_count (if present). Falls back to the legacy
        # 3-5 single-series window.
        expected_low, expected_high = _expected_bullet_range(context_json)

        resp = await self._converse_with_timeout(
            model_id=self.summary_id,
            system=SUMMARY_SYSTEM,
            user=context_json,
            max_tokens=1200,
            temperature=0.3,
            timeout_s=HAIKU_PRIMARY_TIMEOUT,
        )
        primary_latency_ms = resp.get("latency_ms")

        # --- Fallback trigger 1: primary returned a bedrock error. ---
        # Includes the new 'timeout' synthetic error emitted by
        # _converse_with_timeout when Haiku exceeds
        # HAIKU_PRIMARY_TIMEOUT — same fallback path, same observable
        # behaviour (bullets eventually return or the function bubbles
        # up the error).
        if "error" in resp and self.summary_id != self.sonnet_id:
            logger.warning(
                "compose_summary: primary model %s failed (%s), falling back to %s",
                self.summary_id, resp.get("message", ""), self.sonnet_id,
            )
            retry_reason = "timeout" if resp.get("error") == "timeout" else "error"
            resp = await self._converse_with_timeout(
                model_id=self.sonnet_id,
                system=SUMMARY_SYSTEM,
                user=context_json,
                max_tokens=1200,
                temperature=0.3,
                timeout_s=SONNET_FALLBACK_TIMEOUT,
            )

        if "error" in resp:
            return resp

        parsed = _try_json(resp["text"])
        bullets = parsed if isinstance(parsed, list) else None

        # --- Fallback trigger 2: bad bullet count from primary. ---
        # This catches the exact failure mode seen in postmortem
        # 2026-05-10 where Haiku returned len=2 on the second identical
        # Tesla request. Sonnet is stricter about the "EXACTAMENTE N"
        # rules in SUMMARY_SYSTEM. The expected range depends on
        # whether the context carries a multi-series ``stats.series_count``
        # hint (see ``_expected_bullet_range``).
        if (
            retry_reason is None
            and self.summary_id != self.sonnet_id
            and (
                bullets is None
                or not (expected_low <= len(bullets) <= expected_high)
            )
        ):
            logger.warning(
                "compose_summary: primary model %s produced bad bullet count "
                "(len=%s, expected=[%d,%d]) — falling back to %s",
                self.summary_id,
                len(bullets) if isinstance(bullets, list) else "N/A",
                expected_low, expected_high,
                self.sonnet_id,
            )
            retry_reason = "bad_count"
            resp = await self._converse_with_timeout(
                model_id=self.sonnet_id,
                system=SUMMARY_SYSTEM,
                user=context_json,
                max_tokens=1200,
                temperature=0.3,
                timeout_s=SONNET_FALLBACK_TIMEOUT,
            )
            if "error" in resp:
                return resp
            parsed = _try_json(resp["text"])
            bullets = parsed if isinstance(parsed, list) else None

        # Observability: log per-model latency so future postmortems
        # see the Haiku/Sonnet breakdown without needing to enable
        # debug tracing. Primary path logs once; retry path logs
        # twice (primary, then fallback). Added 2026-05-13 after
        # TSLA-vs-NVDA compose_summary stall — we couldn't tell from
        # existing logs whether Haiku itself was slow or whether the
        # retry-to-Sonnet fired.
        final_latency_ms = resp.get("latency_ms")
        if retry_reason is None:
            logger.info(
                "compose_summary: model=%s latency=%sms bullets=%s "
                "expected=[%d,%d] retried=no",
                resp.get("model_id"), final_latency_ms,
                len(bullets) if isinstance(bullets, list) else "N/A",
                expected_low, expected_high,
            )
        else:
            logger.info(
                "compose_summary: primary=%s primary_latency=%sms "
                "fallback=%s fallback_latency=%sms total=%sms bullets=%s "
                "expected=[%d,%d] retry_reason=%s",
                self.summary_id, primary_latency_ms,
                resp.get("model_id"), final_latency_ms,
                (primary_latency_ms or 0) + (final_latency_ms or 0),
                len(bullets) if isinstance(bullets, list) else "N/A",
                expected_low, expected_high, retry_reason,
            )

        return {
            "bullets": bullets,
            "raw": resp["text"] if bullets is None else None,
            "model_id": resp["model_id"],
            "latency_ms": resp["latency_ms"],
            "tokens": {
                "input": resp.get("input_tokens"),
                "output": resp.get("output_tokens"),
            },
            "retried": retry_reason is not None,
            "retry_reason": retry_reason,
        }

    # ─── Health check ────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Ping all configured models. Used by ``/diagnose``."""
        results: dict[str, Any] = {}
        checks = [
            ("nova_lite", self.nova_lite_id),
            ("haiku", self.haiku_id),
            ("sonnet", self.sonnet_id),
        ]
        # Include summary model if it's distinct from the above.
        known_ids = {m for _, m in checks}
        if self.summary_id not in known_ids:
            checks.append(("summary", self.summary_id))
        for label, model_id in checks:
            r = await self._converse(
                model_id=model_id,
                system="Respond with the single word: OK",
                user="ping",
                max_tokens=10,
                temperature=0.0,
            )
            ok = (
                "error" not in r
                and r.get("text", "").strip().upper().startswith("OK")
            )
            results[label] = {
                "model_id": model_id,
                "ok": ok,
                "latency_ms": r.get("latency_ms"),
                "error": r.get("message"),
            }
        return {"region": self.region, "checks": results}

    # ─── Internal: single converse call (sync boto wrapped async) ──

    async def _converse(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Wrap the synchronous boto3 ``converse`` in ``asyncio.to_thread``."""
        return await asyncio.to_thread(
            _converse_sync,
            self._boto, model_id, system, user, max_tokens, temperature,
        )

    async def _converse_with_timeout(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> dict[str, Any]:
        """``_converse`` bounded by a soft deadline.

        If the Bedrock call doesn't finish within ``timeout_s`` seconds
        we cancel the awaiter and return the same ``{"error": ...}``
        shape that :func:`_converse_sync` emits on a real Bedrock
        failure, so compose_summary's existing fallback branches
        (check ``"error" in resp``) route through cleanly.

        The backing ``asyncio.to_thread`` job keeps running in the
        thread pool after cancellation — Python can't preempt a sync
        boto call — but its result is discarded. boto3's connection
        pool reclaims the socket when the underlying HTTP read
        eventually completes (bounded by boto's ``read_timeout=30``).

        Added 2026-05-13 to bound Haiku's latency tail on
        compose_summary; observed a 25 s Haiku stall that blew past
        the pipeline-stall watchdog. See HAIKU_PRIMARY_TIMEOUT.
        """
        t0 = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._converse(
                    model_id=model_id,
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            logger.warning(
                "compose_summary: model=%s exceeded %.1fs timeout "
                "(elapsed=%dms) — discarding in-flight call, falling "
                "over to Sonnet",
                model_id, timeout_s, elapsed_ms,
            )
            return {
                "error": "timeout",
                "model_id": model_id,
                "message": f"Bedrock call exceeded {timeout_s:.1f}s",
                "latency_ms": elapsed_ms,
            }


# ─────────────────────────────────────────────────────────────
# Module-level helpers (unit-test-friendly)
# ─────────────────────────────────────────────────────────────


def _converse_sync(
    boto_client: Any,
    model_id: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Synchronous body of :meth:`BedrockRouterClient._converse`.

    Exposed at module scope so tests can patch / exercise it directly.
    """
    t0 = time.perf_counter()
    try:
        resp = boto_client.converse(
            modelId=model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        return {
            "error": "bedrock_error",
            "model_id": model_id,
            "message": str(exc),
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }

    latency_ms = round((time.perf_counter() - t0) * 1000)
    try:
        text = resp["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = ""
    usage = resp.get("usage", {}) or {}
    return {
        "text": text,
        "model_id": model_id,
        "latency_ms": latency_ms,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "stop_reason": resp.get("stopReason"),
    }


def _expected_bullet_range(context_json: str) -> tuple[int, int]:
    """Return ``(low, high)`` inclusive bullet-count window for the
    given Sonnet context.

    Rules (mirror ``SUMMARY_SYSTEM`` in this module):

    * Single series (no ``stats.series_count`` or ``< 2``) → ``(3, 5)``.
    * Multi-series with N ≥ 2 → ``(N+1, min(N+2, 8))``: one per series
      + one comparative is the target, but Haiku is allowed one bullet
      of slack (e.g., 2 series → 3 OR 4 bullets) so routine ±1 drift
      doesn't trigger a Sonnet fallback. The upper bound is capped at
      8 to match :class:`~src.models.financial.ReportBundle` validation.

    Never raises — malformed JSON or missing keys fall back to the
    legacy single-series range. The retry heuristic and the toolkit
    validator both call this so the Sonnet prompt, the client retry,
    and the server-side validator stay in lockstep.

    The tolerance widening (2026-05-13) was added after a live-demo
    TSLA-vs-NVDA run where Haiku returned 4 bullets instead of the
    strict 3, Sonnet fallback fired, and the combined latency
    exceeded Session B's 15 s stall watchdog. Accepting ±1 bullet
    cut the fallback rate to roughly zero while keeping the output
    quality indistinguishable on a live projector.
    """
    series_count: int | None = None
    try:
        ctx = json.loads(context_json) if context_json else None
        if isinstance(ctx, dict):
            stats = ctx.get("stats")
            if isinstance(stats, dict):
                raw_sc = stats.get("series_count")
                if isinstance(raw_sc, int) and raw_sc >= 2:
                    series_count = raw_sc
    except (ValueError, TypeError):
        pass
    if series_count is not None:
        low = min(series_count + 1, 8)
        high = min(series_count + 2, 8)
        return low, high
    return 3, 5


def _try_json(text: str) -> Any:
    """Parse a JSON blob out of ``text``, tolerating ``` fences and prose.

    Returns ``None`` if nothing parseable is found.
    """
    if not text:
        return None
    stripped = text.strip()
    # Strip a single code fence if present.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
    # Try to locate the first {...} or [...] balanced block.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = stripped.find(opener)
        j = stripped.rfind(closer)
        if i != -1 and j > i:
            candidate = stripped[i:j + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    # Last resort — whole string.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
