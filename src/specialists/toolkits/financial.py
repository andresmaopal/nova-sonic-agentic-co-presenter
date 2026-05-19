"""FinancialToolkit — the ``financial`` specialist's concrete implementation.

Implements the three domain-specific methods required by
:class:`SpecialistToolkit` on top of :class:`FinalysisClient`:

- ``fetch_data`` — dispatches to one of the 9 Finalysis methods keyed
  on the ``kind`` field from the tool input.
- ``transform_data`` — reshapes Finalysis JSON into AntV-compatible
  arrays. Inline transforms for simple shapes; delegates to
  ``BedrockRouterClient.transform_data`` (Haiku) for multi-series.
- ``compute_stats`` — numeric summary the shared ``compose_summary``
  hands to Sonnet (first/last/high/low/pct_change, or top-N for
  rankings).

The other four tools (generate_chart, compose_summary, render_report,
end_session) come from :class:`SharedToolkitMixin` for free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from src.clients.bedrock_router import BedrockRouterClient
from src.clients.finalysis import FinalysisClient
from src.specialists.base import (
    FetchResult,
    SpecialistToolkit,
    ToolContext,
    TransformResult,
)
from src.specialists.toolkits.shared import SharedToolkitMixin, _localize


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Indicator ASR-robustness map + cross-kind routing
# ─────────────────────────────────────────────────────────────
#
# 2026-05-12 postmortem (CEMEX / RSI handoff × 3): Nova Sonic STT in
# es-419 reliably mishears the presenter's spoken "R-S-I" as "R-C-I"
# because the Spanish letter names "ese" (S) and "ce" (C) are
# acoustically close. The handoff query therefore arrived at Carlos as
# *"reporte RCI para CEMEX"*. Carlos faithfully passed ``indicator=rci``
# to ``fetch_data``, Finalysis responded 404 on ``/trend/rci`` (no such
# endpoint), and the session aborted with BAD_ARGS. The same behaviour
# was observed three times in a row across h1/h2/h3 as the presenter
# repeated the request louder.
#
# Two defensive layers, in order:
#
# 1. **ASR alias table** — hard-coded map of frequent mistranscriptions
#    to (kind, indicator). Only include *unambiguous* homophones: the
#    source must not itself be a valid Finalysis indicator AND the
#    target must be its obvious speech-to-text near-neighbour. Adding
#    ambiguous aliases here would silently "fix" legitimate queries.
#
# 2. **Cross-kind routing** — if the indicator is valid but the caller
#    put it under the wrong ``kind`` (e.g., the LLM puts RSI under
#    ``kind=trend`` because the user said "tendencia del RSI"), quietly
#    switch to the correct ``kind``. This is a pure bug-fix: Finalysis
#    would return 404 otherwise and we have enough information to
#    unambiguously reroute.
#
# Both layers log at INFO when they fire so postmortems can see that a
# correction was applied rather than the original call being executed.

_INDICATOR_ASR_ALIASES: dict[str, tuple[str, str]] = {
    # R-S-I ↔ R-C-I (most common). All lowercase + hyphenated variants.
    "rci":   ("momentum", "rsi"),
    "r-c-i": ("momentum", "rsi"),
    "r c i": ("momentum", "rsi"),
    "arci":  ("momentum", "rsi"),     # "el RSI" → "el arcí" → "arci"
    # MACD variants heard when the presenter says the whole acronym.
    "maced": ("trend", "macd"),
    "macde": ("trend", "macd"),
    "mak":   ("trend", "macd"),
    # S-M-A homophones (rarer but observed twice in older logs).
    "esemea": ("trend", "sma"),
    "e-s-a":  ("trend", "sma"),
    # A-T-R ↔ A-D-R (ADR is a different catalyst endpoint, not a
    # volatility indicator — route the mishearing back to ATR which
    # is what users almost always mean when they say "volatilidad").
    "adr-volatility": ("volatility", "atr"),
}

# Canonical whitelists for cross-kind routing. Kept in sync with the
# ``Literal`` definitions in ``src/clients/finalysis.py`` — do NOT
# re-export those Literals here because importing them from the client
# creates a circular dependency at startup.
_INDICATORS_BY_KIND: dict[str, frozenset[str]] = {
    "trend": frozenset({
        "sma", "ema", "wma", "macd", "adx", "aroon", "cci", "dpo",
        "ichimoku", "kst", "mass_index", "psar", "trix", "vortex",
    }),
    "momentum": frozenset({
        "rsi", "stochrsi", "stochastic", "roc", "tsi",
        "ultimate_oscillator", "williams_r", "ppo", "pvo",
    }),
    "volatility": frozenset({
        "atr", "bollinger", "donchian", "keltner", "ulcer_index",
    }),
    "volume": frozenset({
        "acc_dist", "cmf", "eom", "force_index", "mfi", "obv",
        "vpt", "vwap",
    }),
}


def _normalize_indicator_args(
    kind: str, indicator: Any,
) -> tuple[str, Any, str | None]:
    """Return (kind, indicator, note) after ASR + cross-kind rerouting.

    ``note`` is ``None`` when no correction was applied; otherwise a
    short human-readable string describing the rewrite, for logging.

    Only runs on the four Finalysis kinds that have an indicator
    whitelist (trend / momentum / volatility / volume). Catalyst,
    volume_comparison, premarket, quote, raw are left untouched —
    they have different indicator semantics (catalyst kinds,
    ranking-comparison kinds, raw paths).
    """
    if kind not in _INDICATORS_BY_KIND or not isinstance(indicator, str):
        return kind, indicator, None

    raw = indicator.lower().strip()
    # Layer 1: hard ASR alias.
    if raw in _INDICATOR_ASR_ALIASES:
        new_kind, new_ind = _INDICATOR_ASR_ALIASES[raw]
        return new_kind, new_ind, (
            f"ASR alias: {indicator!r} under kind={kind!r} → "
            f"kind={new_kind!r} indicator={new_ind!r}"
        )
    # Layer 2: cross-kind reroute (indicator is valid but under the
    # wrong kind). If it's already in the caller's kind whitelist, no
    # change. If it's in another kind's whitelist, route there.
    if raw in _INDICATORS_BY_KIND[kind]:
        return kind, raw, None
    for other_kind, whitelist in _INDICATORS_BY_KIND.items():
        if other_kind == kind:
            continue
        if raw in whitelist:
            return other_kind, raw, (
                f"cross-kind route: {raw!r} belongs to "
                f"kind={other_kind!r}, not {kind!r}"
            )
    # Unknown indicator — let the dispatcher hit Finalysis and surface
    # the 404/422 naturally (BAD_ARGS), so Carlos can narrate honestly.
    return kind, indicator, None


# ─────────────────────────────────────────────────────────────
# FinancialToolkit
# ─────────────────────────────────────────────────────────────


class FinancialToolkit(SharedToolkitMixin, SpecialistToolkit):
    """Carlos's toolkit: Finalysis → AntV → Sonnet → HTML.

    Mixin ordering (`SharedToolkitMixin` first) makes the mixin's
    ``generate_chart`` / ``compose_summary`` / ``render_report`` /
    ``end_session`` win over SpecialistToolkit's abstract declarations.
    """

    def __init__(
        self,
        *,
        finalysis: FinalysisClient,
        bedrock_router: BedrockRouterClient | None = None,
    ) -> None:
        self.finalysis = finalysis
        self.bedrock_router = bedrock_router
        # Per-handle ticker memory. fetch_data writes; compose_summary +
        # render_report read. Used to override any customer_name the LLM
        # hallucinates so the report title can never contradict the body
        # (postmortem 2026-05-08 follow-up, N1: "Alphabet (GOOG)" title
        # on a SPY report). Single-concurrency (handoff_rate max=1)
        # makes per-instance state safe — no cross-handoff bleed.
        #
        # Stored as ``list[str]`` to support multi-symbol fan-out
        # (2026-05-13 Change A/D). A single-symbol fetch stores a
        # length-1 list; a multi-symbol fetch stores the full list in
        # caller-provided order. Downstream callers (``compose_summary``,
        # ``render_report``) treat a length-1 list identically to the
        # legacy single-ticker path — the multi-ticker branch in
        # ``_canonical_display_name`` only triggers for len >= 2.
        self._tickers_by_handle: dict[str, list[str]] = {}
        # The last tickers stored, for tools that don't receive `handle`
        # as a parameter (render_report). Empty list = "never set".
        self._last_tickers: list[str] = []
        # Pre-computed stats task. generate_chart kicks off compute_stats
        # in the background so compose_summary doesn't wait for it.
        self._stats_task: asyncio.Task | None = None
        self._stats_handle: str | None = None

    # ─── fetch_data ──────────────────────────────────────────

    async def fetch_data(
        self, *, params: dict[str, Any], ctx: ToolContext,
    ) -> FetchResult:
        """Fetch from Finalysis keyed on ``params["kind"]``.

        Tool input shape (per ``design.md § 10.1``):

            {
              "kind":       "trend" | "momentum" | "volatility" | "volume"
                            | "catalyst" | "volume_comparison" | "premarket"
                            | "quote" | "raw",
              "indicator":  "sma" | "ema" | "rsi" | ... (kind-specific),
              "symbol":     "TSLA",              # single-symbol path
              "symbols":    ["AMZN","MSFT"],     # multi-symbol fan-out (Change A)
              "start_date": "2025-11-07",
              "end_date":   "2026-05-07",
              "window":     50,                   # single-window path
              "windows":    [20, 50],             # multi-window fan-out (Change A)
              "extra_params": { ...}
            }

        Fan-out rules (2026-05-13 Change A):

        - Pass EITHER ``symbol`` OR ``symbols``, never both.
        - Pass EITHER ``window`` OR ``windows``, never both.
        - ``symbols`` and ``windows`` cannot BOTH be multi-valued in one
          call (would imply a Cartesian product that's never what
          Carlos means). Caller picks one axis to fan out on.
        - Multi-series fan-out applies only to the four time-series
          kinds: trend, momentum, volatility, volume. Other kinds
          (quote, premarket, catalyst, volume_comparison, raw) have
          incompatible response shapes and fail with BAD_ARGS.
        - Both lists cap at 6 elements to match the default chart
          palette and keep the legend legible on a projector.
        """
        kind = (params.get("kind") or "").lower().strip()
        indicator = params.get("indicator")
        symbol = params.get("symbol")
        symbols_raw = params.get("symbols")
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        window = params.get("window")
        windows_raw = params.get("windows")
        extra: dict[str, Any] = dict(params.get("extra_params") or {})

        # ASR robustness + cross-kind routing (2026-05-12 postmortem).
        # See ``_normalize_indicator_args`` above. Silently rewrites
        # ``kind``/``indicator`` when the caller passed a well-known
        # mistranscription ("rci" → momentum/rsi) or put a valid
        # indicator under the wrong kind (RSI under ``kind=trend``).
        # Logs at INFO when a correction fires so postmortems can tell
        # the rewrite from the original call.
        kind, indicator, _correction_note = _normalize_indicator_args(
            kind, indicator,
        )
        if _correction_note:
            logger.info("fetch_data: %s", _correction_note)

        # ── Normalize symbols[] / windows[] into canonical lists ──
        #
        # symbols_list / windows_list are the lists the dispatch loop
        # will walk. For single-call paths they end up length-1, so
        # the downstream merge logic is uniform.
        def _as_symbols(v: Any) -> list[str]:
            if isinstance(v, list):
                return [s.strip().upper() for s in v
                        if isinstance(s, str) and s.strip()]
            return []

        def _as_windows(v: Any) -> list[int]:
            if isinstance(v, list):
                out: list[int] = []
                for w in v:
                    if isinstance(w, bool):
                        continue
                    if isinstance(w, int):
                        out.append(w)
                    elif isinstance(w, float) and w.is_integer():
                        out.append(int(w))
                return out
            return []

        symbols_list = _as_symbols(symbols_raw)
        windows_list = _as_windows(windows_raw)

        # exactly-one-of between scalar and list forms (reject both).
        if isinstance(symbol, str) and symbol.strip() and symbols_list:
            substep = "symbol+symbols conflict · bad-args"
            await ctx.phase(0, substep=substep, status="error")
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    "(pass either symbol OR symbols, not both)"
                ),
            )
        if isinstance(window, int) and not isinstance(window, bool) and windows_list:
            substep = "window+windows conflict · bad-args"
            await ctx.phase(0, substep=substep, status="error")
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    "(pass either window OR windows, not both)"
                ),
            )

        # Collapse scalar inputs into the list view.
        if not symbols_list and isinstance(symbol, str) and symbol.strip():
            symbols_list = [symbol.strip().upper()]
        if not windows_list and isinstance(window, int) and not isinstance(window, bool):
            windows_list = [window]

        multi_symbol = len(symbols_list) >= 2
        multi_window = len(windows_list) >= 2
        fan_out = multi_symbol or multi_window

        # Fan-out guard rails: multi×multi forbidden, cap at 6, kinds
        # limited to the four time-series families.
        if multi_symbol and multi_window:
            await ctx.phase(
                0, substep="symbols×windows not allowed", status="error",
            )
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    "(cannot fan out on symbols and windows in one call)"
                ),
            )
        if multi_symbol and len(symbols_list) > 6:
            await ctx.phase(
                0, substep=f"{len(symbols_list)} symbols exceeds cap",
                status="error",
            )
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    f"(demasiados símbolos: {len(symbols_list)}; máximo 6)"
                ),
            )
        if multi_window and len(windows_list) > 6:
            await ctx.phase(
                0, substep=f"{len(windows_list)} windows exceeds cap",
                status="error",
            )
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    f"(demasiadas ventanas: {len(windows_list)}; máximo 6)"
                ),
            )
        _TS_KINDS = ("trend", "momentum", "volatility", "volume")
        if fan_out and kind not in _TS_KINDS:
            await ctx.phase(
                0, substep=f"fan-out not supported for kind={kind}",
                status="error",
            )
            return FetchResult(
                ok=False, code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    f"(fan-out sólo para trend/momentum/volatility/volume; "
                    f"kind={kind!r})"
                ),
            )

        # ── Phase-0 substep + entry log ──
        if fan_out:
            if multi_symbol:
                pretty = ",".join(symbols_list)
                substep_active = f"{len(symbols_list)}× {pretty} · {indicator} · {kind}"
            else:
                pretty = ",".join(str(w) for w in windows_list)
                substep_active = (
                    f"{symbols_list[0] if symbols_list else '?'} · "
                    f"{indicator}×{pretty} · {kind}"
                )
        else:
            substep_bits = [
                b for b in [
                    symbols_list[0] if symbols_list else None,
                    indicator, kind,
                ] if b
            ]
            substep_active = " · ".join(str(b) for b in substep_bits) or "fetch"
        await ctx.phase(0, substep=substep_active)

        # Trace every call so postmortems can see exactly what Carlos
        # asked for. INFO level — every handoff emits at most one of
        # these. See (internal postmortem 2026-05-09)
        # § RC-3 (toolInput was never reaching logs/python.log).
        logger.info(
            "fetch_data called: kind=%r indicator=%r symbols=%r "
            "start_date=%r end_date=%r windows=%r extra_keys=%s fanout=%s",
            kind, indicator, symbols_list,
            start_date, end_date, windows_list,
            sorted(extra.keys()) if extra else [],
            "multi_symbol" if multi_symbol
            else ("multi_window" if multi_window else "single"),
        )

        # P3 — Reject market-wide screeners combined with a symbol
        # filter. Market-wide catalysts never fan out (they're kind=catalyst
        # which already blocked by the _TS_KINDS guard above), so this
        # rule only applies to the single-call path, preserving legacy.
        # See (internal postmortem 2026-05-08) § 7 P3.
        if (
            not fan_out
            and kind == "catalyst"
            and isinstance(indicator, str)
            and indicator.lower().strip() in _MARKET_WIDE_CATALYST_INDICATORS
            and symbols_list
        ):
            logger.info(
                "fetch_data: refusing market-wide screener with ticker filter "
                "kind=%r indicator=%r symbol=%r — would return 0 rows",
                kind, indicator, symbols_list[0],
            )
            return await self._fail_fetch(
                ctx=ctx,
                code="BAD_ARGS",
                message=(
                    f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                    f"({indicator!r} es un ranking del mercado; no acepta "
                    f"un símbolo específico)"
                ),
                substep_tail=(
                    f"{indicator} · market-wide · rejected"
                ),
            )

        # Guard: auto-expand date range when window(s) require more
        # trading days than the range provides. Use max(windows_list)
        # for the safe floor so every fan-out series has enough data.
        # See postmortem: "dame el SMA de 50 días de Tesla" chose a
        # 60-day range that only yielded 19 rows — Finalysis needs ≥
        # window rows.
        _effective_window = max(windows_list) if windows_list else None
        if _effective_window and start_date and end_date:
            from datetime import date as _date, timedelta as _td
            try:
                _sd = _date.fromisoformat(start_date)
                _ed = _date.fromisoformat(end_date)
                _span = (_ed - _sd).days
                _min_span = int(_effective_window) * 5
                if _span < _min_span:
                    _new_sd = _ed - _td(days=_min_span)
                    logger.info(
                        "fetch_data: auto-expanding date range for "
                        "effective_window=%s: %s→%s (span %dd < min %dd)",
                        _effective_window, start_date, _new_sd.isoformat(),
                        _span, _min_span,
                    )
                    start_date = _new_sd.isoformat()
            except (ValueError, TypeError):
                pass  # malformed dates — let Finalysis reject downstream

        # ── Build the N call specs (length 1 on the single path) ──
        #
        # Each spec is a dict of kwargs for ``_single_finalysis_call``
        # plus a ``group_label`` the merger uses as the key in the
        # unified ``values`` dict. Labels pick themselves:
        #   * multi-symbol → the uppercase symbol (e.g. "AMZN")
        #   * multi-window → f"{indicator}_{window}" (e.g. "ema_20")
        #   * single       → whatever the first numeric key in the
        #                     Finalysis response turns out to be;
        #                     single-call path doesn't merge so the
        #                     label is unused.
        call_specs: list[dict[str, Any]] = []
        if multi_symbol:
            lone_window = windows_list[0] if windows_list else None
            for sym in symbols_list:
                call_specs.append({
                    "group_label": sym,
                    "symbol": sym,
                    "window": lone_window,
                })
        elif multi_window:
            lone_symbol = symbols_list[0] if symbols_list else None
            for w in windows_list:
                call_specs.append({
                    "group_label": f"{indicator}_{w}" if indicator else f"w{w}",
                    "symbol": lone_symbol,
                    "window": w,
                })
        else:
            call_specs.append({
                "group_label": (symbols_list[0] if symbols_list else "series"),
                "symbol": symbols_list[0] if symbols_list else None,
                "window": windows_list[0] if windows_list else None,
            })

        # ── Execute (serialized on fan-out, direct on single) ──
        #
        # Finalysis's backend uses a shared ClickHouse client per
        # process and returns HTTP 503 when it sees two concurrent
        # queries on the same session (observed 2026-05-13 with a
        # 3-symbol AMZN/MSFT/GOOGL fan-out: one or two of the three
        # concurrent /trend/sma calls got back
        # ``ClickHouse server is unreachable: Attempt to execute
        # concurrent queries within the same session`` and the chart
        # rendered with fewer lines than Carlos asked for).
        #
        # Serialize via an asyncio.Semaphore(1) so at most one
        # Finalysis request is in flight at a time. The speed
        # difference is negligible — 3 × ~300 ms sequential ≈ 900 ms
        # vs. ~300 ms parallel = 600 ms saved — well inside the
        # 25 s Session B stall budget. Keeps the gather shape so
        # ``results`` still comes back in input order.
        async def _run(spec: dict[str, Any]) -> dict[str, Any]:
            return await self._single_finalysis_call(
                kind=kind, indicator=indicator,
                symbol=spec["symbol"],
                start_date=start_date, end_date=end_date,
                window=spec["window"], extra=extra,
            )

        results: list[dict[str, Any]]
        if fan_out:
            finalysis_gate = asyncio.Semaphore(1)

            async def _run_gated(spec: dict[str, Any]) -> dict[str, Any]:
                async with finalysis_gate:
                    return await _run(spec)

            results = list(await asyncio.gather(
                *[_run_gated(s) for s in call_specs],
                return_exceptions=False,
            ))
        else:
            results = [await _run(call_specs[0])]

        # ── Partial-failure handling (fan-out only) ──
        #
        # Policy: on the single-call path, ANY failure fails the whole
        # call (unchanged legacy behavior). On fan-out:
        #   * If EVERY call failed → bubble up the first failure with
        #     the same code/message the single path would produce.
        #   * If at least one call succeeded → keep the ok results,
        #     mark the envelope partial_ok=True, and list the failed
        #     series so Carlos can narrate it honestly.
        ok_results: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (spec, r) for spec, r in zip(call_specs, results, strict=True)
            if r.get("ok")
        ]
        failed_results: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (spec, r) for spec, r in zip(call_specs, results, strict=True)
            if not r.get("ok")
        ]

        if not ok_results:
            # Total failure: translate the first error through the
            # same _fail_fetch path the single-call code used to use.
            first = failed_results[0][1]
            code = first.get("code") or "FINALYSIS_ERROR"
            log_detail = first.get("log_detail") or ""
            if code == "BAD_ARGS":
                hint = first.get("message_suffix") or "bad args"
                return await self._fail_fetch(
                    ctx=ctx, code="BAD_ARGS",
                    message=(
                        f"{_localize_fin('bad_args', ctx.specialist.locale)} "
                        f"({hint})"
                    ),
                    substep_tail=f"{substep_active} · {log_detail} · bad-args",
                )
            return await self._fail_fetch(
                ctx=ctx, code="FINALYSIS_ERROR",
                message=_localize_fin("finalysis_error", ctx.specialist.locale),
                substep_tail=(
                    f"{substep_active} · "
                    f"{log_detail + ' · ' if log_detail else ''}sin-datos"
                ),
            )

        # ── Build the envelope that gets stored behind the handle ──
        if not fan_out:
            # Single-call path: store the raw response as-is for
            # byte-for-byte backward compatibility. The whole downstream
            # pipeline (_summarize_raw, transform_data target=line_single,
            # etc.) has been tested against this shape for months.
            raw_to_store: Any = ok_results[0][1]["raw"]
        else:
            # Fan-out path: merge N time-series responses into a single
            # {"data": [{"date", "values": {<group>: v, …}}], …} envelope.
            # Haiku's transform prompt already knows to "extract every
            # indicator series from data[] rows, using the indicator key
            # as the group label" — the group labels we choose here
            # (symbol for multi-symbol, f"{ind}_{w}" for multi-window)
            # become exactly those keys.
            raw_to_store = _merge_time_series_envelopes(
                [(spec["group_label"], r["raw"])
                 for spec, r in ok_results],
            )
            # Annotate partial failures for downstream observability.
            raw_to_store["series_count"] = len(ok_results)
            raw_to_store["series_labels"] = [
                spec["group_label"] for spec, _ in ok_results
            ]
            if failed_results:
                raw_to_store["partial_ok"] = True
                raw_to_store["failed_series"] = [
                    {
                        "label": spec["group_label"],
                        "code": r.get("code"),
                        "detail": r.get("log_detail"),
                    }
                    for spec, r in failed_results
                ]

        # Store raw payload behind an opaque handle; summary stays small.
        handle = await ctx.put_handle("fn", raw_to_store)
        summary, count = _summarize_raw(raw_to_store)

        # Record the ticker(s) for this handle so compose_summary and
        # render_report can canonicalize customer_name against them.
        # Only the symbols that *succeeded* in fan-out land here — a
        # partial success narrates only the symbols actually rendered.
        successful_tickers = [
            spec["group_label"] for spec, _ in ok_results
        ] if multi_symbol else [symbols_list[0]] if symbols_list else []
        if successful_tickers:
            self._tickers_by_handle[handle] = successful_tickers
            self._last_tickers = successful_tickers

        # Postmortem logging: Finalysis can return HTTP 200 with a
        # shape that has no time-series rows and no rankings (e.g.,
        # for an index symbol it doesn't index, or a sector query that
        # came through as a trend request). These "silent empties"
        # previously only surfaced as a mysterious EMPTY_TRANSFORM
        # downstream. Log them here too so we can see the whole chain
        # in one grep.
        if count is None:
            logger.info(
                "fetch_data: no time-series rows and no rankings. "
                "kind=%r indicator=%r symbols=%r raw_shape=%s",
                kind, indicator, symbols_list,
                _describe_raw_shape(raw_to_store),
            )

        # Enrich phase-0 substep post-success so the projector shows
        # how much data the fetch actually produced. The next tool
        # call (transform) will advance to phase 1 within ms on the
        # happy path; this final update is mainly useful when the
        # specialist pauses or retries.
        if count is not None:
            tail = f"{count} rows"
            if fan_out:
                tail = f"{len(ok_results)} series · {count} rows"
                if failed_results:
                    tail += f" · {len(failed_results)} fallaron"
            await ctx.phase(
                0, substep=f"{substep_active} · {tail}",
            )

        return FetchResult(
            ok=True, handle=handle, count=count, summary=summary,
        )

    # ─── transform_data ──────────────────────────────────────

    async def transform_data(
        self, *, handle: str, target: str, ctx: ToolContext,
    ) -> TransformResult:
        """Shape Finalysis data for the chosen chart type.

        Supported ``target`` values:

        - ``line_single`` — single time series (``[{time, value}]``).
        - ``line_multi`` — multi-symbol or multi-indicator comparison;
          delegates to Haiku via ``bedrock_router.transform_data``.
        - ``bar_ranked`` / ``column_categorical`` — screener/ranking
          output (``[{category, value}]``).
        - ``dual_axes`` — price + volume.
        - ``histogram`` — distribution of returns (inline compute).
        - ``pie`` — composition breakdown.
        """
        target = (target or "").lower().strip()
        raw = await ctx.get_handle(handle)
        if raw is None:
            return TransformResult(
                ok=False, code="HANDLE_NOT_FOUND",
                message=_localize("handle_not_found", ctx.specialist.locale),
            )

        await ctx.phase(1, substep=target)

        try:
            chart_data: list[dict[str, Any]]
            if target == "line_single":
                chart_data = _shape_line_single(raw)

            elif target == "line_multi":
                # Fast path — 2026-05-13: when ``raw`` is a Change-A
                # merged multi-series envelope (has ``series_labels``
                # at top level), the shape is already deterministic,
                # so skip the Haiku round-trip and emit
                # ``[{time, value, group}]`` records in-process. This
                # eliminates the stall-watchdog timeout observed on
                # AMZN-vs-MSFT runs where the ~8 KB Haiku input
                # occasionally exceeded Session B's 15 s budget.
                #
                # Haiku remains the fallback for the original use-case:
                # multi-column single-symbol indicators (MACD /
                # Bollinger / Ichimoku / …) whose ``values`` schema
                # isn't predictable and needs an LLM to reshape.
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("series_labels"), list)
                    and raw["series_labels"]
                ):
                    chart_data = _shape_line_multi_from_merged(raw)
                else:
                    if self.bedrock_router is None:
                        return TransformResult(
                            ok=False, code="TRANSFORM_ERROR",
                            message="line_multi requires Haiku transform; bedrock router not wired",
                        )
                    task_desc = (
                        "Each element: {time: str, value: number, group: str}. "
                        "Extract every indicator series from data[] rows, using the "
                        "indicator key as the group label. Drop points whose value is null. "
                        "Sort by time ascending."
                    )
                    resp = await self.bedrock_router.transform_data(
                        task_description=task_desc,
                        source_json=json.dumps(raw, default=str),
                    )
                    if "error" in resp or not isinstance(resp.get("data"), list):
                        return TransformResult(
                            ok=False, code="TRANSFORM_ERROR",
                            message="Haiku transform failed for multi-series",
                        )
                    chart_data = _optimize_line_multi(resp["data"])

            elif target in ("bar_ranked", "column_categorical"):
                chart_data = _shape_ranked(raw)

            elif target == "dual_axes":
                chart_data = _shape_dual_axes(raw)

            elif target == "histogram":
                chart_data = _shape_histogram(raw)

            elif target == "pie":
                chart_data = _shape_pie(raw)

            else:
                return TransformResult(
                    ok=False, code="BAD_ARGS",
                    message=f"unknown target: {target!r}",
                )
        except Exception as exc:   # noqa: BLE001
            logger.exception("FinancialToolkit.transform_data raised: %s", exc)
            return TransformResult(
                ok=False, code="TRANSFORM_ERROR",
                message=_localize_fin("transform_error", ctx.specialist.locale),
            )

        new_handle = await ctx.put_handle("td", chart_data)
        if not chart_data:
            # Defence in depth: every shape helper can legitimately return
            # [] when the raw payload's shape doesn't match the requested
            # ``target`` (e.g., Carlos fetched a single `quote` snapshot
            # then asked for ``target=line_single`` which expects a
            # ``data[]`` time series). Without this guard AntV renders a
            # blank chart and Sonnet narrates a summary that has nothing
            # to stand on. Mark the visor errored + fail loudly so the
            # model calls ``end_session`` cleanly.
            #
            # Postmortem logging: EMPTY_TRANSFORM is a silent failure
            # mode (fetch returned 200 OK, transform returned 0 points
            # in <10 ms, user sees "la serie está vacía"). Without
            # capturing raw-shape info here, diagnosing which kind of
            # mismatch occurred requires reading code. Log enough to
            # disambiguate: (a) wrong target for this shape, (b)
            # Finalysis returned empty data[], (c) fetch returned a
            # quote/snapshot, (d) some other unexpected shape.
            logger.warning(
                "EMPTY_TRANSFORM: target=%r produced 0 points. raw_shape=%s",
                target, _describe_raw_shape(raw),
            )
            await ctx.phase(1, substep=f"{target} · 0-points", status="error")
            return TransformResult(
                ok=False,
                code="EMPTY_TRANSFORM",
                message=(
                    f"transform produced 0 points (target={target!r}); "
                    "the raw fetch likely has no time-series rows for this shape"
                ),
            )
        # Success path: emit a richer substep so the projector shows
        # both the target shape and the optimised point count (which
        # reflects the round/downsample/regroup pipeline). Useful to
        # see how much a long series was compressed for readability.
        await ctx.phase(1, substep=f"{target} · {len(chart_data)} pts")
        return TransformResult(
            ok=True, handle=new_handle, points=len(chart_data),
        )

    # ─── compute_stats ───────────────────────────────────────

    async def compute_stats(
        self, *, handle: str, ctx: ToolContext,
    ) -> dict[str, Any]:
        """Numeric summary for Sonnet's compose_summary prompt.

        If generate_chart already kicked off a background stats task for
        this handle, await that instead of recomputing.

        Returns ``{first_value, last_value, high, low, pct_change, count}``
        for time series; ``{count, top}`` for rankings; ``{quote}`` for
        single snapshots. Caller is responsible for handling empty dicts.
        """
        # Use pre-computed result if available (parallelization win).
        if (
            self._stats_task is not None
            and self._stats_handle == handle
            and not self._stats_task.cancelled()
        ):
            try:
                return await self._stats_task
            except Exception:  # noqa: BLE001
                pass  # Fall through to normal computation.
            finally:
                self._stats_task = None

        return await self._compute_stats_impl(handle=handle, ctx=ctx)

    async def _compute_stats_impl(
        self, *, handle: str, ctx: ToolContext,
    ) -> dict[str, Any]:
        """Actual stats computation (extracted for pre-computation reuse).

        Shape:

        - **Single series** (one numeric key across all rows, or a
          non-time-series shape): returns the flat
          ``{first_value, last_value, high, low, pct_change, count}``
          keys that compose_summary has consumed since day one. No
          ``series`` field — downstream code treats absence as "just
          one line; use the flat keys".

        - **Multi-series** (two or more numeric keys visible across
          rows — either from a multi-column indicator like MACD / BB
          or from the fan-out merge added in Change A). The flat keys
          are still populated for the **primary series** (first key
          encountered in the first row, by insertion order) for
          strict backwards compatibility — every single-symbol
          consumer keeps working. In addition a ``series`` block maps
          ``<label> → {first, last, high, low, pct_change, count}``
          for every series, plus ``series_count`` at the top level so
          the Sonnet prompt can branch on it.
        """
        raw = await ctx.get_handle(handle)
        if raw is None:
            return {}

        # Time-series shape: {"data": [{"date": ..., "values": {...}}, ...]}.
        rows = _extract_time_series_rows(raw)
        if rows:
            # Walk every row once, building a per-label list of values
            # in time order. ``labels_order`` preserves first-seen order
            # so the "primary" series is stable even when later rows
            # introduce new keys.
            labels_order: list[str] = []
            series_values: dict[str, list[float]] = {}
            for r in rows:
                vals = r.get("values") or {}
                if not isinstance(vals, dict):
                    continue
                for k, v in vals.items():
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        continue
                    if k not in series_values:
                        series_values[k] = []
                        labels_order.append(k)
                    series_values[k].append(float(v))

            if not labels_order:
                # No numeric columns anywhere.
                return {}

            def _summary(vals: list[float]) -> dict[str, Any]:
                if not vals:
                    return {}
                first, last = vals[0], vals[-1]
                hi, lo = max(vals), min(vals)
                pct = (((last - first) / first) * 100.0) if first else 0.0
                return {
                    "first": _round2(first),
                    "last": _round2(last),
                    "high": _round2(hi),
                    "low": _round2(lo),
                    "pct_change": _round2(pct),
                    "count": len(vals),
                }

            # Primary series = the first label seen. Flat keys mirror
            # its values for backwards compatibility with every
            # single-symbol consumer (the Sonnet prompt, the handback
            # brief extractor, postmortem logs, etc.).
            primary_label = labels_order[0]
            primary = _summary(series_values[primary_label])
            out: dict[str, Any] = {
                "first_value": primary.get("first"),
                "last_value": primary.get("last"),
                "high": primary.get("high"),
                "low": primary.get("low"),
                "pct_change": primary.get("pct_change"),
                "count": primary.get("count", 0),
            }

            # Multi-series block (only when 2+ distinct series).
            if len(labels_order) >= 2:
                out["series"] = {
                    label: _summary(series_values[label])
                    for label in labels_order
                }
                out["series_count"] = len(labels_order)
                out["primary_label"] = primary_label

            return out

        # Rankings / screeners.
        rankings = _extract_rankings(raw)
        if rankings:
            return {
                "count": len(rankings),
                "top": rankings[:5],
            }

        # Single snapshot (quote, premarket).
        if isinstance(raw, dict):
            if "quote" in raw:
                return {"quote": raw["quote"]}
            # Return a small, JSON-safe subset.
            return {k: v for k, v in raw.items()
                    if isinstance(v, (str, int, float, bool))}

        return {}

    # ─── generate_chart + compose_summary parallelization ────
    #
    # generate_chart and compose_summary are independent (different
    # handles, no data dependency) but the LLM calls them sequentially.
    # We overlap them by pre-computing stats during generate_chart so
    # compose_summary only needs the fast LLM call. Saves ~0.5-1s of
    # the stats computation that would otherwise block the summary.
    # The bigger win is the Haiku model switch (NOVA_SUMMARY_MODEL).

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
        # Speculatively pre-compute stats for the fn- handle so
        # compose_summary doesn't have to wait for it.
        fn_handle = next(
            (h for h in self._tickers_by_handle if h.startswith("fn-")), None,
        )
        if fn_handle and self._stats_handle != fn_handle:
            self._stats_handle = fn_handle
            self._stats_task = asyncio.ensure_future(
                self._compute_stats_impl(handle=fn_handle, ctx=ctx),
            )
        return await super().generate_chart(
            handle=handle,
            tool_name=tool_name,
            title=title,
            axis_x_title=axis_x_title,
            axis_y_title=axis_y_title,
            ctx=ctx,
        )

    # ─── compose_summary / render_report overrides ──────────
    #
    # Both overrides exist for a single reason: *canonicalize
    # ``customer_name`` against the ticker the pipeline actually
    # fetched*. The LLM occasionally hallucinates a company name for
    # an ETF or index query (e.g., SPY → "Alphabet (GOOG)") — see the
    # 2026-05-08 follow-up postmortem, N1. The server is the last
    # place the value passes through before it becomes part of the
    # rendered report title, so this is where we enforce the invariant.
    #
    # Strategy:
    #   • If the LLM-provided ``customer_name`` already contains the
    #     ticker (case-insensitive substring), trust it — it's almost
    #     certainly fine (`"Amazon (AMZN)"`, `"Tesla"`, `"SPY"` …).
    #   • Otherwise, replace with a canonical label derived from the
    #     ticker (known-ETF table or plain symbol fallback) and log a
    #     warning so we can monitor how often this fires.
    #
    # Runs inside the single-concurrency handoff, so per-instance
    # state (``_ticker_by_handle`` / ``_last_ticker``) is safe.

    async def compose_summary(
        self,
        *,
        handle: str,
        context: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        tickers = (
            self._tickers_by_handle.get(handle) or self._last_tickers
        )
        if tickers:
            provided = str(context.get("customer_name") or "")
            canonical = _canonical_display_name(tickers, provided)
            if canonical != provided:
                context = {**context, "customer_name": canonical}
        return await super().compose_summary(
            handle=handle, context=context, ctx=ctx,
        )

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
        # render_report doesn't receive a handle, so use the
        # most-recently-fetched tickers. One concurrent handoff at a
        # time (app_state.handoff_rate.max_concurrent == 1) guarantees
        # this belongs to the same pipeline.
        if self._last_tickers:
            customer_name = _canonical_display_name(
                self._last_tickers, customer_name,
            )
        return await super().render_report(
            customer_name=customer_name,
            description=description,
            chart_url=chart_url,
            chart_title=chart_title,
            bullets=bullets,
            slug=slug,
            ctx=ctx,
            footer_note=footer_note,
        )

    # ─── private helpers ─────────────────────────────────────

    async def _fail_fetch(
        self,
        *,
        ctx: ToolContext,
        code: str,
        message: str,
        substep_tail: str,
    ) -> FetchResult:
        """Centralize every failing exit of ``fetch_data``.

        Posts an *error*-status phase-0 update to the visor before
        returning the FetchResult. Without this, a Finalysis outage (or
        any argument-validation failure) leaves the overlay spinning
        forever on "Consultando Finalysis API … active" because the
        toolkit had already armed phase 0 at the top of ``fetch_data``
        but no branch ever advanced or errored it.

        Mirrors the error-phase pattern already used by
        ``transform_data`` (``... · 0-points · error``),
        ``generate_chart`` (``... · failed · error``),
        ``compose_summary`` (``sonnet · failed · error``), and
        ``render_report`` (``... · failed · error``).

        See (internal postmortem 2026-05-08) § 3.1 / § 7 P0.
        """
        await ctx.phase(0, substep=substep_tail, status="error")
        return FetchResult(ok=False, code=code, message=message)

    async def _bad_args(self, detail: str, ctx: ToolContext) -> FetchResult:
        """Argument-validation failure. Routes through _fail_fetch so the
        visor is always kept in sync."""
        return await self._fail_fetch(
            ctx=ctx,
            code="BAD_ARGS",
            message=f"{_localize_fin('bad_args', ctx.specialist.locale)} ({detail})",
            substep_tail=f"{detail[:48]} · bad-args",
        )

    # ─── _single_finalysis_call ──────────────────────────────
    #
    # Extracted 2026-05-13 (Change A) so ``fetch_data`` can invoke the
    # same dispatch + error-classification logic once per call in the
    # single-symbol path AND N times concurrently in the multi-symbol
    # fan-out path. No visor side-effects, no ``_bad_args`` /
    # ``_fail_fetch`` — the caller owns phase-0 updates because in the
    # fan-out path we only post phase-0 once for the whole batch.
    #
    # Returns one of:
    #   {"ok": True,  "raw": <Finalysis dict>}
    #   {"ok": False, "code": "BAD_ARGS"|"FINALYSIS_ERROR",
    #                 "message_suffix": <str>,   # appended after the localized base
    #                 "log_detail": <str>}       # for debug logging / substep
    async def _single_finalysis_call(
        self,
        *,
        kind: str,
        indicator: Any,
        symbol: str | None,
        start_date: str | None,
        end_date: str | None,
        window: int | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one Finalysis dispatch + classify any error. Pure
        data-plane helper — no visor, no exceptions escape."""
        try:
            if kind == "trend":
                if not indicator or not symbol or not start_date or not end_date:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "trend needs indicator + symbol + dates",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_trend_indicator(
                    indicator, symbol=symbol,
                    start_date=start_date, end_date=end_date,
                    window=window, **_filter_kwargs(extra, _TREND_EXTRA),
                )

            elif kind == "momentum":
                if not indicator or not symbol or not start_date or not end_date:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "momentum needs indicator + symbol + dates",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_momentum_indicator(
                    indicator, symbol=symbol,
                    start_date=start_date, end_date=end_date,
                    window=window, **_filter_kwargs(extra, _MOMENTUM_EXTRA),
                )

            elif kind == "volatility":
                if not indicator or not symbol or not start_date or not end_date:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "volatility needs indicator + symbol + dates",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_volatility_indicator(
                    indicator, symbol=symbol,
                    start_date=start_date, end_date=end_date,
                    window=window, **_filter_kwargs(extra, _VOLATILITY_EXTRA),
                )

            elif kind == "volume":
                if not indicator or not symbol or not start_date or not end_date:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "volume needs indicator + symbol + dates",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_volume_indicator(
                    indicator, symbol=symbol,
                    start_date=start_date, end_date=end_date,
                    window=window,
                )

            elif kind == "catalyst":
                if not indicator:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "catalyst needs indicator (kind value)",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_catalyst(
                    indicator, symbol=symbol,
                    start_date=start_date, end_date=end_date,
                    window=window, **_filter_kwargs(extra, _CATALYST_EXTRA),
                )

            elif kind == "volume_comparison":
                if not indicator:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "volume_comparison needs indicator (kind value)",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_volume_comparison(
                    indicator,
                    symbol=symbol, start_date=start_date, end_date=end_date,
                    target_date=extra.get("target_date"),
                    comparison_date=extra.get("comparison_date"),
                )

            elif kind == "premarket":
                if not symbol:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "premarket needs symbol",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_premarket_levels(
                    symbol, target_date=extra.get("target_date"),
                )

            elif kind == "quote":
                if not symbol:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "quote needs symbol",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.get_current_quote(symbol)

            elif kind == "raw":
                if not indicator:
                    return {
                        "ok": False, "code": "BAD_ARGS",
                        "message_suffix": "raw needs indicator (path)",
                        "log_detail": "missing required param",
                    }
                raw = await self.finalysis.finalysis_raw_get(indicator, extra)

            else:
                return {
                    "ok": False, "code": "BAD_ARGS",
                    "message_suffix": f"unknown kind: {kind!r}",
                    "log_detail": "unknown kind",
                }
        except Exception as exc:   # noqa: BLE001
            logger.exception(
                "FinancialToolkit._single_finalysis_call raised "
                "(kind=%r indicator=%r symbol=%r window=%r): %s",
                kind, indicator, symbol, window, exc,
            )
            return {
                "ok": False, "code": "FINALYSIS_ERROR",
                "message_suffix": None,
                "log_detail": "exception",
            }

        # HTTP / transport errors surfaced as ``{"error": ...}`` dicts
        # by FinalysisClient. Same classification rules as before the
        # Change-A refactor.
        if isinstance(raw, dict) and "error" in raw:
            err_kind = raw.get("error")
            http_status = raw.get("status")
            detail = raw.get("detail")
            is_client_err = (
                err_kind == "http_error"
                and isinstance(http_status, int)
                and 400 <= http_status < 500
            )
            if is_client_err:
                hint = _finalysis_detail_to_hint(detail)
                return {
                    "ok": False, "code": "BAD_ARGS",
                    "message_suffix": f"Finalysis {http_status}: {hint}",
                    "log_detail": f"http {http_status}",
                }
            return {
                "ok": False, "code": "FINALYSIS_ERROR",
                "message_suffix": None,
                "log_detail": f"http {http_status}" if http_status else str(err_kind),
            }

        return {"ok": True, "raw": raw}


# ─────────────────────────────────────────────────────────────
# Module-level helpers (easier to unit-test)
# ─────────────────────────────────────────────────────────────

_TREND_EXTRA = {"window_slow", "window_fast", "window_sign",
                "window1", "window2", "window3", "step", "max_step"}
_MOMENTUM_EXTRA = {"window_slow", "window_fast", "window_sign",
                   "window1", "window2", "window3",
                   "smooth1", "smooth2", "smooth_window", "lbp"}
_VOLATILITY_EXTRA = {"window_dev", "window_atr"}
_CATALYST_EXTRA = {
    "target_date", "benchmark", "atr_window", "atr_multiplier",
    "window_short", "window_medium", "window_long",
    "sma_short", "sma_medium", "sma_long",
    "baseline_short", "baseline_medium", "baseline_long",
    "rvol_baseline", "return_window", "lookback", "lookback_52w",
    "ratio_window", "threshold_pct", "annualize",
    "bb_window", "bb_dev", "kc_window", "kc_atr_window",
    "limit", "min_price", "min_avg_volume", "min_dollar_volume",
    "min_rvol", "min_adr", "min_abs_return_5d",
}

# Catalyst indicators that rank the entire US equity universe. Passing
# a ``symbol`` filter to any of them is a logical contradiction — the
# Finalysis API returns 0 rows in < 10 ms and the pipeline stalls
# (fetch_data → visor phase 0 active, Carlos narrates "no tengo
# datos", visor never advances). We reject these combinations
# client-side so the error code is accurate (BAD_ARGS, not
# FINALYSIS_ERROR) and the visor flips to an error state immediately.
# See (internal postmortem 2026-05-08) § 6 + § 7 P3.
_MARKET_WIDE_CATALYST_INDICATORS: set[str] = {
    "top-growth",                # biggest % gainers today
    "rvol",                       # relative volume screener
    "news-candidate-universe",    # news-driven movers
}


def _filter_kwargs(extra: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Drop any ``extra_params`` keys that aren't accepted by this endpoint."""
    return {k: v for k, v in extra.items() if k in allowed and v is not None}


def _finalysis_detail_to_hint(detail: Any) -> str:
    """Turn a Finalysis error ``detail`` field into a short human hint.

    Finalysis returns two shapes of error body:

    - 404 from a missing endpoint → ``{"detail": "Not Found"}``
    - 422 from FastAPI validation → ``{"detail": [{"type": "missing",
      "loc": ["query", "target_date"], "msg": "Field required",
      "input": null}, ...]}``

    The caller uses this hint in the error message Carlos narrates, so
    it must be **short** (<= 80 chars) and **actionable** (tell him
    what's missing or wrong, not a raw JSON dump).
    """
    if detail is None:
        return "sin detalle"
    if isinstance(detail, str):
        # 404 / single-string detail. Just return it trimmed.
        return detail[:80]
    if isinstance(detail, list) and detail:
        # 422 validation errors. Prefer "missing" errors (most common
        # when Carlos forgets target_date/comparison_date). Surface the
        # field name — that's what Carlos needs to hear.
        missing: list[str] = []
        other: list[str] = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = item.get("loc") or []
            field = loc[-1] if loc else "?"
            err_type = item.get("type", "")
            msg = item.get("msg", "")
            if err_type == "missing":
                missing.append(str(field))
            else:
                other.append(f"{field}: {msg}")
        if missing:
            return f"faltan parámetros: {', '.join(missing[:4])}"
        if other:
            return "; ".join(other)[:80]
    return str(detail)[:80]


# Canonical display names for symbols where Carlos is most likely to
# hallucinate a wrong company name. We deliberately keep this small;
# the invariant we enforce is "customer_name MUST contain the ticker",
# not "customer_name MUST be one of these strings". For anything not
# listed, the fallback is "TICKER" in parentheses.
KNOWN_TICKER_NAMES: dict[str, str] = {
    # Broad-market index ETFs
    "SPY":  "S&P 500 (SPY)",
    "VOO":  "S&P 500 (VOO)",
    "IVV":  "S&P 500 (IVV)",
    "QQQ":  "Nasdaq-100 (QQQ)",
    "DIA":  "Dow Jones (DIA)",
    "IWM":  "Russell 2000 (IWM)",
    # Sector / thematic ETFs commonly requested in demos
    "JETS": "Aerolíneas de Estados Unidos (JETS)",
    "XLF":  "Financial Sector (XLF)",
    "XLK":  "Technology Sector (XLK)",
    "XLE":  "Energy Sector (XLE)",
    "XLV":  "Health Care Sector (XLV)",
    "SMH":  "Semiconductors (SMH)",
    # Commodities / rates
    "GLD":  "Oro (GLD)",
    "SLV":  "Plata (SLV)",
    "USO":  "Petróleo (USO)",
    "TLT":  "Treasuries 20+ (TLT)",
    # Currency
    "UUP":  "Dólar DXY (UUP)",
}


def _canonical_display_name(
    tickers: str | list[str], provided: str,
) -> str:
    """Return a ``customer_name`` guaranteed to be consistent with the
    fetched ``tickers``.

    Accepts either a single ticker string (legacy, single-symbol path
    — byte-for-byte preserved) or a list of tickers (multi-symbol
    comparison path added 2026-05-13).

    **Single-ticker mode** (``tickers="SPY"`` or ``["SPY"]``). Trust
    the LLM's value if EITHER direction matches:
      1. ``provided`` contains the ticker ("Amazon (AMZN)" ✓), OR
      2. The ticker's known canonical name contains ``provided``
         ("Tesla" is inside "Tesla (TSLA)" ✓).
    Otherwise override with the canonical label. This prevents the
    real bug (SPY data titled "Alphabet") while tolerating the harmless
    case where Carlos omits the ticker suffix from a correct name.

    **Multi-ticker mode** (``tickers=["AMZN", "MSFT"]``). Trust the
    LLM's value iff it contains **all** tickers (uppercase substring
    match). Otherwise compose:

    - ``len(tickers) == 2`` → ``"Amazon (AMZN) vs Microsoft (MSFT)"``
      (falls back to bare ticker when a name isn't in
      ``KNOWN_TICKER_NAMES``).
    - ``len(tickers) >= 3`` → ``"Comparación (AMZN, MSFT, GOOG)"`` —
      compact form that stays legible in the 2-slide report header.

    The strict all-tickers check for the multi-ticker branch is
    deliberately stricter than the single-ticker "either direction"
    gate: for a comparison report it's actively misleading if the
    title only names one of the two subjects.
    """
    # Normalize input to a list of uppercase tickers.
    if isinstance(tickers, str):
        raw = [tickers] if tickers.strip() else []
    else:
        raw = list(tickers or [])
    ticker_list = [t.strip().upper() for t in raw if t and t.strip()]
    if not ticker_list:
        return provided

    p = (provided or "").strip()
    p_upper = p.upper()

    # Multi-ticker branch — only when more than one distinct ticker.
    if len(ticker_list) >= 2:
        # Trust provided iff it names every ticker.
        if p and all(t in p_upper for t in ticker_list):
            return p
        if len(ticker_list) == 2:
            a, b = ticker_list
            # KNOWN_TICKER_NAMES already wraps in "(TICKER)" form;
            # for unknowns use the bare ticker to avoid "(AMZN) vs
            # (MSFT)" double-parenthesizing.
            name_a = KNOWN_TICKER_NAMES.get(a, a)
            name_b = KNOWN_TICKER_NAMES.get(b, b)
            canonical = f"{name_a} vs {name_b}"
        else:
            canonical = f"Comparación ({', '.join(ticker_list)})"
        logger.warning(
            "customer_name mismatch (multi-ticker): tickers=%r provided=%r → %r",
            ticker_list, provided, canonical,
        )
        return canonical

    # Single-ticker mode (legacy path — preserved byte-for-byte).
    t = ticker_list[0]
    # Direction 1: provided contains the ticker literally.
    if t in p_upper:
        return p
    # Direction 2: the canonical name for this ticker contains provided.
    canonical = KNOWN_TICKER_NAMES.get(t, f"({t})")
    if p and p.upper() in canonical.upper():
        return p
    logger.warning(
        "customer_name mismatch: ticker=%r provided=%r → %r",
        t, provided, canonical,
    )
    return canonical


def _summarize_raw(raw: Any) -> tuple[dict[str, Any], int | None]:
    """Produce a short, JSON-safe summary of a Finalysis response for
    Session B's narration. Keeps the raw blob out of context."""
    summary: dict[str, Any] = {}
    count: int | None = None

    rows = _extract_time_series_rows(raw)
    if rows:
        values = [_first_numeric(r.get("values") or {}) for r in rows]
        values = [v for v in values if v is not None]
        if values:
            summary = {
                "first_value": _round2(values[0]),
                "last_value": _round2(values[-1]),
            }
            count = len(values)

    if count is None:
        rankings = _extract_rankings(raw)
        if rankings:
            count = len(rankings)
            top = rankings[0]
            summary = {
                "top_symbol": top.get("symbol") or top.get("ticker")
                              or (isinstance(top, dict) and next(iter(top.values()), None)),
            }

    if count is None and isinstance(raw, dict):
        # Single-shot endpoints: pick a tiny JSON-safe slice.
        summary = {k: v for k, v in raw.items()
                   if isinstance(v, (str, int, float, bool))}

    # Multi-series envelope annotations (Change A 2026-05-13). When
    # fetch_data fan-out merges N calls into a single envelope, it
    # stamps ``series_count`` / ``series_labels`` / ``partial_ok`` on
    # top-level. Echo those into the summary so Carlos's phase-1
    # narration ("{N} puntos") can say "2 series · 120 puntos" and
    # Nova sees the fan-out occurred.
    if isinstance(raw, dict):
        if isinstance(raw.get("series_count"), int) and raw["series_count"] > 1:
            summary["series_count"] = raw["series_count"]
        if isinstance(raw.get("series_labels"), list):
            # Cap the labels list in the summary to keep the payload
            # tiny; the full list is still on the raw envelope.
            summary["series_labels"] = list(raw["series_labels"])[:6]
        if raw.get("partial_ok") is True:
            summary["partial_ok"] = True
            if isinstance(raw.get("failed_series"), list):
                summary["failed_series"] = [
                    {"label": f.get("label"), "code": f.get("code")}
                    for f in raw["failed_series"][:6]
                    if isinstance(f, dict)
                ]

    return summary, count


def _merge_time_series_envelopes(
    labeled_payloads: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Merge N Finalysis time-series responses into one multi-series
    envelope keyed by date.

    Each ``(group_label, payload)`` in ``labeled_payloads`` contributes
    one series to the merged ``values`` dict per row. Dates union across
    all series; when a series has no point for a given date its key is
    simply absent (Haiku's transform prompt treats null/absent values
    as "drop that point", so the line ends up with a natural gap).

    Input shapes accepted per payload:
      ``{"data": [{"date": "...", "values": {"<any>": number}}, ...]}``

    The first numeric value in each ``values`` dict is picked as the
    series value (consistent with ``_first_numeric`` used on the
    single-call path). For Finalysis responses that carry multiple
    numeric columns (e.g., Bollinger: hband/mavg/lband) the first is
    the primary signal — suitable for the common multi-symbol and
    multi-window comparisons; richer multi-column merges are a future
    Change B concern, not this one.

    Preserves ``group_label`` order in a synthetic ``series_labels``
    top-level field so the downstream shape helpers can keep a stable
    legend.

    Output shape:
      ``{"data": [{"date": "...", "values": {"<label>": n, …}}, ...],
         "series_labels": [<label>, ...]}``
    """
    # Use a dict keyed by date string; built-in insertion order gives
    # us a stable timeline when we sort later.
    per_date: dict[str, dict[str, float]] = {}
    labels_seen: list[str] = []

    for label, payload in labeled_payloads:
        labels_seen.append(label)
        rows = _extract_time_series_rows(payload)
        for row in rows:
            date = row.get("date") or row.get("time") or row.get("timestamp")
            if date is None:
                continue
            value = _first_numeric(row.get("values") or {})
            if value is None:
                continue
            bucket = per_date.setdefault(str(date), {})
            bucket[label] = float(value)

    merged_rows = [
        {"date": d, "values": per_date[d]}
        for d in sorted(per_date.keys())
    ]
    return {
        "data": merged_rows,
        "series_labels": labels_seen,
    }


def _shape_line_multi_from_merged(raw: Any) -> list[dict[str, Any]]:
    """Fast-path shape for Change-A merged multi-series envelopes.

    When ``raw`` carries a top-level ``series_labels`` list, the merge
    step in ``fetch_data`` has already produced a well-formed
    ``{"data": [{"date": "…", "values": {<label>: number, …}}, …]}``
    structure — we know the exact shape up-front and can emit the
    AntV ``{time, value, group}`` records inline without a Haiku
    round-trip.

    Rationale: the legacy ``transform_data target=line_multi`` path
    (~5-second Haiku call) was built for the multi-column single-symbol
    indicators (MACD / Bollinger / Ichimoku) where the `values` schema
    isn't predictable. For Change-A fan-outs the schema is fully known.
    Under occasional Bedrock latency spikes a ~8 KB JSON input to Haiku
    could exceed Session B's 15-second stall watchdog; this fast path
    eliminates that risk entirely for the symbols[] / windows[] flow.

    Returns the optimizer output (rounded, regrouped, downsampled) so
    the caller can pass it straight to the chart generator.
    """
    labels = raw.get("series_labels") if isinstance(raw, dict) else None
    if not isinstance(labels, list) or not labels:
        return []
    rows = _extract_time_series_rows(raw)
    out: list[dict[str, Any]] = []
    for r in rows:
        date = r.get("date") or r.get("time") or r.get("timestamp")
        if date is None:
            continue
        values = r.get("values") or {}
        if not isinstance(values, dict):
            continue
        for label in labels:
            v = values.get(label)
            if v is None or isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out.append({
                    "time": str(date),
                    "value": float(v),
                    "group": str(label),
                })
    return _optimize_line_multi(out)


def _extract_time_series_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [r for r in data if isinstance(r, dict)]
    return []


def _extract_rankings(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("rankings", "candidates", "results"):
            arr = raw.get(key)
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                return arr
    return []


def _first_numeric(values: dict[str, Any]) -> float | None:
    """Pick the first numeric value from a ``values`` dict (Finalysis's
    per-row object). Some indicators produce multiple columns
    (e.g. ``bollinger`` returns ``hband``, ``mavg``, ``lband``); we take
    whichever key comes first for the single-series transform."""
    for v in values.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _round2(x: float) -> float:
    return round(float(x) * 100) / 100


def _round1(x: float) -> float:
    """Round to 1 decimal place for chart labels.

    Forcing 1 decimal at the Python boundary is the only way to control
    tooltip / point-label precision in AntV line charts: the
    ``@antv/mcp-server-chart`` line schema exposes ``lineWidth`` but no
    label formatter, so precision travels with the data. Most Finalysis
    indicators (prices, SMAs, RSI, returns) are comfortably readable at
    1 decimal on a fullscreen projector; stats computed for the
    executive summary still use ``_round2`` for accuracy.
    """
    return round(float(x) * 10) / 10


# Chart-pipeline optimizations for fullscreen projector readability.
#
# All three are triggered conservatively — below their thresholds the
# helpers are pure pass-throughs, so unit tests with small fixtures
# (3-15 points) are unaffected. The net effect on a real 6-month daily
# Finalysis series (~125 points) is ~30 evenly-spaced weekly buckets
# rendered with 1-decimal values — ~4× fewer dots, larger natural
# label spacing, identical semantic content.
#
# Latency: downsampling/regrouping happens in-process on a list of
# dicts (µs) AND shrinks the JSON body sent to the AntV MCP, so the
# net effect is slightly LOWER end-to-end latency, not higher.

# Max points we ever send to AntV. Above this the natural tick density
# on the projector PNG starts stacking labels even for a projector.
#
# 2026-05-12 — "3× less granularity for time-series" workaround. The
# previous default was 40, which at a 640-px pre-upscale canvas (see
# ``shared._PROJECTOR_CHART_WIDTH``) leaves ~16 px per x-tick — too
# dense once the browser scales the PNG 2.5× and the date text grows
# to ~30 px. Dropping to 14 yields ~45 px per tick pre-upscale and
# ~115 px post-upscale, comfortable for ISO date labels
# ("2026-03-05") on a projector. 14 also happens to match the
# canonical RSI/MACD window, which makes "last 14 periods" queries
# render 1 : 1 without the regroup → downsample round trip losing
# data points mid-pipeline.
_CHART_MAX_POINTS_DEFAULT = 14
# Daily-cadence series longer than this get regrouped to weekly buckets
# (last-of-bucket, preserving close-price semantics).
_REGROUP_WEEKLY_THRESHOLD_DAYS = 60
# Series longer than this get regrouped to monthly buckets instead.
_REGROUP_MONTHLY_THRESHOLD_DAYS = 365


def _downsample_points(
    points: list[dict[str, Any]],
    *,
    max_points: int = _CHART_MAX_POINTS_DEFAULT,
) -> list[dict[str, Any]]:
    """Evenly subsample ``points`` to at most ``max_points`` elements.

    First and last points are always preserved so the visible trend
    anchors line up with the headline "first_value → last_value"
    narration. Assumes ``points`` is already sorted by ``time``.

    Below threshold: returns the list unchanged (fast path, no copy).
    """
    n = len(points)
    if n <= max_points or max_points < 2:
        return points
    # Closed-form even stride. Integer math keeps first & last indices.
    step = (n - 1) / (max_points - 1)
    idxs = [round(i * step) for i in range(max_points)]
    # Dedup while preserving order (rounding can produce collisions at
    # the extremes for small max_points).
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for i in idxs:
        if i in seen:
            continue
        seen.add(i)
        out.append(points[i])
    return out


_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _regroup_by_date_bucket(
    points: list[dict[str, Any]],
    *,
    value_key: str = "value",
) -> list[dict[str, Any]]:
    """Regroup a daily-cadence time series into weekly or monthly buckets.

    For financial close-price-like series we keep the LAST point in each
    bucket (preserving the trend line's endpoints). Volume-like series
    get the same treatment — losing intra-week noise on a projector is
    a net readability win and the narration still tracks the summary
    stats from the full series.

    Triggers only when:
      * every ``time`` field parses as ``YYYY-MM-DD``, AND
      * the span covered is > 60 days.

    Below trigger: returns the list unchanged.

    Args:
        points: Sorted list of ``{"time": "YYYY-MM-DD", value_key: …}``.
        value_key: Which numeric field to keep alongside ``time``.
            Defaults to ``"value"``; callers with multiple series
            should call this once per series already shaped with the
            right key.
    """
    if len(points) < _REGROUP_WEEKLY_THRESHOLD_DAYS:
        return points

    parsed: list[tuple[int, int, int]] = []
    for p in points:
        m = _DATE_ISO.match(str(p.get("time", "")))
        if not m:
            return points  # Non-ISO or non-daily — leave as-is.
        parsed.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))

    # Span in days using the pure-Python date calendar (no extra imports
    # at call time — datetime is already available throughout the app).
    import datetime as _dt
    first = _dt.date(*parsed[0])
    last = _dt.date(*parsed[-1])
    span_days = (last - first).days
    if span_days <= _REGROUP_WEEKLY_THRESHOLD_DAYS:
        return points

    use_monthly = span_days > _REGROUP_MONTHLY_THRESHOLD_DAYS

    # bucket_key(i) returns a tuple that's stable across all points in
    # the same week/month; we keep the LAST point seen per bucket.
    buckets: dict[tuple, dict[str, Any]] = {}
    for (y, m, d), point in zip(parsed, points, strict=True):
        if use_monthly:
            key = (y, m)
        else:
            # ISO-week key: (year, week) from the stdlib calendar
            iso_year, iso_week, _iso_day = _dt.date(y, m, d).isocalendar()
            key = (iso_year, iso_week)
        buckets[key] = point  # dict preserves insertion order on 3.7+

    return list(buckets.values())


def _optimize_line_series(
    points: list[dict[str, Any]],
    *,
    value_keys: tuple[str, ...] = ("value",),
    max_points: int = _CHART_MAX_POINTS_DEFAULT,
) -> list[dict[str, Any]]:
    """Apply the three projector-readability transforms in order:

    1. Round every numeric field in ``value_keys`` to 1 decimal.
    2. Regroup daily → weekly/monthly buckets when the span is wide.
    3. Downsample to at most ``max_points`` if still too dense.

    Order matters: regroup first (shrinks the series), then downsample
    (final safety net for rare non-daily cadences or very wide windows).
    Rounding is applied up-front because both subsequent steps preserve
    values unchanged.
    """
    if not points:
        return points
    rounded: list[dict[str, Any]] = []
    for p in points:
        q = dict(p)
        for k in value_keys:
            v = q.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                q[k] = _round1(float(v))
        rounded.append(q)
    # Regroup is a no-op for non-ISO or short series.
    regrouped = _regroup_by_date_bucket(rounded, value_key=value_keys[0])
    # Final downsample guard.
    return _downsample_points(regrouped, max_points=max_points)


def _shape_line_single(raw: Any) -> list[dict[str, Any]]:
    """Produce ``[{time, value}]`` for the first numeric series in data."""
    rows = _extract_time_series_rows(raw)
    out: list[dict[str, Any]] = []
    for r in rows:
        date = r.get("date") or r.get("time") or r.get("timestamp")
        value = _first_numeric(r.get("values") or {})
        if date is None or value is None:
            continue
        out.append({"time": str(date), "value": value})
    # Sort for deterministic rendering.
    out.sort(key=lambda p: p["time"])
    # Projector-readability pipeline: round to 1 decimal, regroup dense
    # daily series to weekly/monthly, cap at ~40 points.
    return _optimize_line_series(out, value_keys=("value",))


def _shape_ranked(raw: Any) -> list[dict[str, Any]]:
    rankings = _extract_rankings(raw)
    out: list[dict[str, Any]] = []
    for r in rankings:
        cat = (r.get("symbol") or r.get("ticker") or r.get("label")
               or r.get("category") or r.get("name"))
        val = (r.get("value") or r.get("volume_change_percent")
               or r.get("volume") or r.get("change")
               or r.get("pct_change") or r.get("rvol"))
        if cat is None or val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        # 1-decimal rounding so bar labels don't show 4-6 decimal
        # artefacts ("120.43478261") on the projector. Rankings stay
        # in Finalysis-provided order (caller decides top-N).
        out.append({"category": str(cat), "value": _round1(val)})
    return out


def _shape_dual_axes(raw: Any) -> list[dict[str, Any]]:
    """Price + volume dual axes: ``[{time, price, volume}]``."""
    rows = _extract_time_series_rows(raw)
    out: list[dict[str, Any]] = []
    for r in rows:
        date = r.get("date") or r.get("time")
        if date is None:
            continue
        values = r.get("values") or {}
        price = values.get("close") or values.get("price")
        volume = values.get("volume")
        if price is None or volume is None:
            continue
        out.append({"time": str(date), "price": float(price), "volume": float(volume)})
    out.sort(key=lambda p: p["time"])
    # Both price and volume get rounded; date regroup + downsample
    # apply the same as single-series line charts.
    return _optimize_line_series(out, value_keys=("price", "volume"))


def _shape_histogram(raw: Any) -> list[dict[str, Any]]:
    """Distribution of returns as ``[{value: <return_pct>}]``."""
    rows = _extract_time_series_rows(raw)
    out: list[dict[str, Any]] = []
    for r in rows:
        val = _first_numeric(r.get("values") or {})
        if val is None:
            continue
        # Histograms are bucketed inside AntV by value; rounding to 1
        # decimal on the way in makes the x-axis tick labels legible
        # on a projector. No time field → no regroup/downsample path.
        out.append({"value": _round1(val)})
    return out


def _optimize_line_multi(
    points: list[dict[str, Any]],
    *,
    max_points_per_group: int = _CHART_MAX_POINTS_DEFAULT,
) -> list[dict[str, Any]]:
    """Same optimizations as ``_optimize_line_series`` but applied
    per-``group`` so multi-series charts keep series independent.

    Preserves input order for groups first-seen; within a group, points
    are optimized (rounded + regrouped + downsampled) and re-sorted by
    ``time`` ascending.
    """
    if not points:
        return points
    by_group: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for p in points:
        g = str(p.get("group", ""))
        if g not in by_group:
            by_group[g] = []
            order.append(g)
        by_group[g].append(p)
    out: list[dict[str, Any]] = []
    for g in order:
        series = by_group[g]
        series.sort(key=lambda r: str(r.get("time", "")))
        optimized = _optimize_line_series(
            series, value_keys=("value",), max_points=max_points_per_group,
        )
        out.extend(optimized)
    return out


def _shape_pie(raw: Any) -> list[dict[str, Any]]:
    """Portfolio/composition breakdown from a rankings-style payload
    (``[{category, value}]`` where the set of categories is small)."""
    return _shape_ranked(raw)


# Localized helper for financial-specific error messages. We extend the
# shared locale table rather than adding new keys there so the shared
# file stays 100% domain-agnostic.
_FIN_MESSAGES: dict[str, dict[str, str]] = {
    "finalysis_error": {
        "es-419": "No encontré datos.",
        "en-US":  "No data found.",
    },
    "transform_error": {
        "es-419": "No pude transformar los datos.",
        "en-US":  "Could not transform the data.",
    },
    "bad_args": {
        "es-419": "Parámetros inválidos.",
        "en-US":  "Invalid arguments.",
    },
}


def _localize_fin(key: str, locale: str) -> str:
    bucket = _FIN_MESSAGES.get(key, {})
    if locale in bucket:
        return bucket[locale]
    lang = (locale or "")[:2].lower()
    for loc, msg in bucket.items():
        if loc.lower().startswith(lang):
            return msg
    return bucket.get("en-US") or key


def _describe_raw_shape(raw: Any) -> str:
    """Compact, log-safe description of a Finalysis payload's shape.

    Used by the EMPTY_TRANSFORM warning so postmortems can tell at a
    glance *why* a transform produced zero points. Never prints the
    full payload — only keys, lengths, and a couple of sentinel flags.

    Examples::

        "dict(keys=['data','symbol'], data_len=0)"        # empty series
        "dict(keys=['quote'], hint=quote-snapshot)"       # wrong shape for line_single
        "dict(keys=['rankings'], rankings_len=5)"         # ranking-shaped
        "dict(keys=['error','message','path'])"           # Finalysis error
        "dict(keys=['data'], data_len=48, sample_row_keys=['date','values'])"
        "list(len=3)"
        "str(len=42)"
        "None"
    """
    if raw is None:
        return "None"
    if isinstance(raw, list):
        return f"list(len={len(raw)})"
    if isinstance(raw, str):
        return f"str(len={len(raw)})"
    if not isinstance(raw, dict):
        return type(raw).__name__

    keys = list(raw.keys())[:10]
    parts = [f"keys={keys}"]

    data = raw.get("data")
    if isinstance(data, list):
        parts.append(f"data_len={len(data)}")
        if data and isinstance(data[0], dict):
            parts.append(f"sample_row_keys={list(data[0].keys())[:6]}")

    for rk in ("rankings", "candidates", "results"):
        arr = raw.get(rk)
        if isinstance(arr, list):
            parts.append(f"{rk}_len={len(arr)}")

    # Sentinel hints.
    if "quote" in raw:
        parts.append("hint=quote-snapshot")
    if "error" in raw:
        parts.append(f"hint=finalysis-error:{str(raw.get('error'))[:40]}")

    return "dict(" + ", ".join(parts) + ")"
