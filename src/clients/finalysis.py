"""FinalysisClient — async httpx port of the Finalysis MCP server.

Wraps the Finalysis HTTP API (``https://finalysis.ronhom.com``). The API
exposes 57+ technical-analysis endpoints across 8 categories; this
client groups them into 9 semantic methods keyed on a
``kind``/``indicator`` enum, mirroring the MCP server at
``gmb-presenter-demo/mcp-servers/finalysis/server.py`` so the behavior
is identical.

Unlike the MCP version this runs **in-process** inside the FastAPI
backend — no MCP/uv cold-start per call. One ``httpx.AsyncClient`` is
shared across all method calls for connection pooling.

Errors never raise — every method returns either a parsed JSON dict on
success or a structured ``{"error": ..., "message": ..., "path": ...}``
dict on failure. Session B narrates the error in Spanish before calling
``end_session``.

Config via env vars:

- ``FINALYSIS_API_KEY`` — required. Loaded via ``python-dotenv`` if
  ``.env`` is present.
- ``FINALYSIS_BASE_URL`` — default ``https://finalysis.ronhom.com``.
- ``FINALYSIS_TIMEOUT`` — default 30 (seconds).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://finalysis.ronhom.com"
DEFAULT_TIMEOUT = 30.0


# ─────────────────────────────────────────────────────────────
# Literal enums (same as the MCP server's)
# ─────────────────────────────────────────────────────────────

TrendIndicator = Literal[
    "sma", "ema", "wma", "macd", "adx", "aroon", "cci", "dpo",
    "ichimoku", "kst", "mass_index", "psar", "trix", "vortex",
]

MomentumIndicator = Literal[
    "rsi", "stochrsi", "stochastic", "roc", "tsi",
    "ultimate_oscillator", "williams_r", "ppo", "pvo",
]

VolatilityIndicator = Literal[
    "atr", "bollinger", "donchian", "keltner", "ulcer_index",
]

VolumeIndicator = Literal[
    "acc_dist", "cmf", "eom", "force_index", "mfi", "obv", "vpt", "vwap",
]

Catalyst = Literal[
    "rvol", "returns", "gap-analysis", "price-position", "support-resistance",
    "historical-volatility", "volume-price-divergence", "context",
    "relative-strength", "stagnation", "bollinger-squeeze", "adr", "streak",
    "risk-reward", "news-candidate-universe",
]

VolumeComparisonKind = Literal[
    "top-by-total-volume", "top-growth", "top-drop", "change",
]


# ─────────────────────────────────────────────────────────────
# FinalysisClient
# ─────────────────────────────────────────────────────────────


class FinalysisClient:
    """Async client for the Finalysis API.

    Intended lifetime is a FastAPI lifespan — one instance per process,
    shared across all Session B handoffs.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("FINALYSIS_API_KEY", "")
        self._base_url = (base_url or os.environ.get(
            "FINALYSIS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._timeout = timeout if timeout is not None else float(
            os.environ.get("FINALYSIS_TIMEOUT", DEFAULT_TIMEOUT))

        if not self._api_key:
            logger.warning(
                "FinalysisClient created without FINALYSIS_API_KEY — "
                "requests will return HTTP 401/403."
            )

        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "X-API-Key": self._api_key,
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ─── 1. Trend indicators ─────────────────────────────────

    async def get_trend_indicator(
        self,
        indicator: TrendIndicator,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        window: int | None = None,
        window_slow: int | None = None,
        window_fast: int | None = None,
        window_sign: int | None = None,
        window1: int | None = None,
        window2: int | None = None,
        window3: int | None = None,
        step: float | None = None,
        max_step: float | None = None,
    ) -> dict[str, Any]:
        """Fetch a trend indicator (SMA, EMA, MACD, ADX, Ichimoku, …)."""
        return await self._request(
            f"/trend/{indicator}",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "window": window,
                "window_slow": window_slow,
                "window_fast": window_fast,
                "window_sign": window_sign,
                "window1": window1,
                "window2": window2,
                "window3": window3,
                "step": step,
                "max_step": max_step,
            },
        )

    # ─── 2. Momentum indicators ──────────────────────────────

    async def get_momentum_indicator(
        self,
        indicator: MomentumIndicator,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        window: int | None = None,
        window_slow: int | None = None,
        window_fast: int | None = None,
        window_sign: int | None = None,
        window1: int | None = None,
        window2: int | None = None,
        window3: int | None = None,
        smooth1: int | None = None,
        smooth2: int | None = None,
        smooth_window: int | None = None,
        lbp: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a momentum indicator (RSI, StochRSI, ROC, Williams %R, …)."""
        return await self._request(
            f"/momentum/{indicator}",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "window": window,
                "window_slow": window_slow,
                "window_fast": window_fast,
                "window_sign": window_sign,
                "window1": window1,
                "window2": window2,
                "window3": window3,
                "smooth1": smooth1,
                "smooth2": smooth2,
                "smooth_window": smooth_window,
                "lbp": lbp,
            },
        )

    # ─── 3. Volatility indicators ────────────────────────────

    async def get_volatility_indicator(
        self,
        indicator: VolatilityIndicator,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        window: int | None = None,
        window_dev: int | None = None,
        window_atr: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a volatility indicator (ATR, Bollinger, Donchian, Keltner, Ulcer)."""
        return await self._request(
            f"/volatility/{indicator}",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "window": window,
                "window_dev": window_dev,
                "window_atr": window_atr,
            },
        )

    # ─── 4. Volume indicators ────────────────────────────────

    async def get_volume_indicator(
        self,
        indicator: VolumeIndicator,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        window: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a volume indicator (OBV, CMF, MFI, VWAP, Force Index, …)."""
        return await self._request(
            f"/volume/{indicator}",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "window": window,
            },
        )

    # ─── 5. Catalyst / screener endpoints ────────────────────

    async def get_catalyst(
        self,
        kind: Catalyst,
        *,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        target_date: str | None = None,
        benchmark: str | None = None,
        atr_window: int | None = None,
        atr_multiplier: float | None = None,
        window: int | None = None,
        window_short: int | None = None,
        window_medium: int | None = None,
        window_long: int | None = None,
        sma_short: int | None = None,
        sma_medium: int | None = None,
        sma_long: int | None = None,
        baseline_short: int | None = None,
        baseline_medium: int | None = None,
        baseline_long: int | None = None,
        rvol_baseline: int | None = None,
        return_window: int | None = None,
        lookback: int | None = None,
        lookback_52w: int | None = None,
        ratio_window: int | None = None,
        threshold_pct: float | None = None,
        annualize: bool | None = None,
        bb_window: int | None = None,
        bb_dev: int | None = None,
        kc_window: int | None = None,
        kc_atr_window: int | None = None,
        limit: int | None = None,
        min_price: float | None = None,
        min_avg_volume: int | None = None,
        min_dollar_volume: int | None = None,
        min_rvol: float | None = None,
        min_adr: float | None = None,
        min_abs_return_5d: float | None = None,
    ) -> dict[str, Any]:
        """Fetch a catalyst/screener endpoint (rvol, gaps, relative strength,
        context, news-candidate-universe, …).

        Symbol-based catalysts need ``symbol``, ``start_date``, ``end_date``.
        ``news-candidate-universe`` is market-wide and uses ``target_date``.
        """
        return await self._request(
            f"/catalyst/{kind}",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "target_date": target_date,
                "benchmark": benchmark,
                "atr_window": atr_window,
                "atr_multiplier": atr_multiplier,
                "window": window,
                "window_short": window_short,
                "window_medium": window_medium,
                "window_long": window_long,
                "sma_short": sma_short,
                "sma_medium": sma_medium,
                "sma_long": sma_long,
                "baseline_short": baseline_short,
                "baseline_medium": baseline_medium,
                "baseline_long": baseline_long,
                "rvol_baseline": rvol_baseline,
                "return_window": return_window,
                "lookback": lookback,
                "lookback_52w": lookback_52w,
                "ratio_window": ratio_window,
                "threshold_pct": threshold_pct,
                "annualize": annualize,
                "bb_window": bb_window,
                "bb_dev": bb_dev,
                "kc_window": kc_window,
                "kc_atr_window": kc_atr_window,
                "limit": limit,
                "min_price": min_price,
                "min_avg_volume": min_avg_volume,
                "min_dollar_volume": min_dollar_volume,
                "min_rvol": min_rvol,
                "min_adr": min_adr,
                "min_abs_return_5d": min_abs_return_5d,
            },
        )

    # ─── 6. Volume comparison (market rankings) ──────────────

    async def get_volume_comparison(
        self,
        kind: VolumeComparisonKind,
        *,
        target_date: str | None = None,
        comparison_date: str | None = None,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Compare volume across dates or rank the market.

        ``change`` requires ``symbol`` + ``start_date`` + ``end_date``;
        the other three use ``target_date`` + ``comparison_date``.
        """
        if kind == "change":
            if not symbol:
                return {
                    "error": "missing_param",
                    "message": "symbol is required for kind=change",
                }
            return await self._request(
                f"/volume-comparison/change/{symbol}",
                {"start_date": start_date, "end_date": end_date},
            )
        return await self._request(
            f"/volume-comparison/{kind}",
            {"target_date": target_date, "comparison_date": comparison_date},
        )

    # ─── 7. Pre-market levels ────────────────────────────────

    async def get_premarket_levels(
        self,
        symbol: str,
        *,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        """Pre-market high/low/open/close for ``symbol``."""
        return await self._request(
            "/premarket/levels",
            {"symbol": symbol, "target_date": target_date},
        )

    # ─── 8. Current quote ────────────────────────────────────

    async def get_current_quote(self, symbol: str) -> dict[str, Any]:
        """Current bid/ask/last snapshot for ``symbol``."""
        return await self._request("/quote/current", {"symbol": symbol})

    # ─── 9. Raw passthrough (escape hatch) ───────────────────

    async def finalysis_raw_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call any Finalysis GET endpoint by path. Escape hatch for
        endpoints not covered by a dedicated method."""
        if not path.startswith("/"):
            path = "/" + path
        return await self._request(path, params or {})

    # ─── Health check ────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Used by ``/diagnose``. Returns reachability + latency."""
        import time
        t0 = time.perf_counter()
        try:
            client = await self._client()
            # A lightweight call that Finalysis supports: current quote for SPY.
            resp = await client.get("/quote/current", params={"symbol": "SPY"})
            latency_ms = round((time.perf_counter() - t0) * 1000)
            return {
                "ok": resp.status_code < 400,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "base_url": self._base_url,
            }
        except Exception as exc:   # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "base_url": self._base_url,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }

    # ─── Internal request wrapper ────────────────────────────

    async def _request(
        self, path: str, params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Shared GET wrapper: drop None params, parse JSON, surface
        structured errors as dicts (never raise)."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        try:
            client = await self._client()
            resp = await client.get(path, params=clean)
        except httpx.RequestError as exc:
            logger.warning("finalysis %s request failed: %s", path, exc)
            return {"error": "request_failed", "message": str(exc), "path": path}

        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except Exception:   # noqa: BLE001
                body = resp.text
            logger.info(
                "finalysis %s returned HTTP %d: %s",
                path, resp.status_code, str(body)[:200],
            )
            return {
                "error": "http_error",
                "status": resp.status_code,
                "path": path,
                "detail": body,
            }

        try:
            return resp.json()
        except Exception as exc:   # noqa: BLE001
            logger.warning("finalysis %s returned non-JSON: %s", path, exc)
            return {"error": "invalid_json", "message": str(exc), "path": path}
