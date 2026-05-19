"""``get_premarket`` — Session A pre-market snapshot tool.

Returns pre-market high/low/open/close + gap% for a single symbol.
Companion to :mod:`src.tools.get_quote`, same design contract: keep
Nova on-voice, no Session B spin-up, no visor, no chart.

The underlying Finalysis endpoint (``/premarket/levels``) returns a
single-point snapshot keyed on the most recent trading day (or an
explicit ``target_date``). There is no time series to graph, so this
is a natural fit for a direct Session A tool.

Response shape (JSON-safe)::

    {"ok": True,
     "symbol": "NVDA",
     "target_date": "2026-04-10",
     "premarket_high": 347.53,
     "premarket_low": 344.51,
     "premarket_open": 346.00,
     "premarket_close": 346.21,
     "premarket_range_pct": 0.88,
     "premarket_volume": 1265660,
     "previous_close": 345.725,
     "gap_pct": 0.08,
     "speech_hint": "..."}
"""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


# ISO date regex (YYYY-MM-DD). Anything else is rejected with
# BAD_ARGS so we don't leak a free-form string into Finalysis.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_SPEECH_HINT = (
    "Answer in ONE short sentence with the pre-market open, range "
    "(high/low), and gap percent. Match the presenter's language. "
    "Example: 'NVIDIA abrió pre-market en 346, rango 344 a 347, "
    "gap de cero coma cero ocho por ciento.' / "
    "'NVIDIA opened pre-market at 346, range 344 to 347, gap of "
    "zero point zero eight percent.' Do NOT quote the raw volume "
    "number unless asked. Do NOT offer to generate a report — "
    "stay silent once done."
)


async def get_premarket_handler(
    *,
    tool_input: dict,
    app_state: Any,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Handle a ``get_premarket`` tool call from Session A.

    Args:
        tool_input: ``{"symbol": "NVDA", "target_date"?: "2026-04-10"}``.
            Symbol is normalized to uppercase. ``target_date`` is
            optional (omit for the most recent trading day).
        app_state: FastAPI ``app.state`` — must carry ``finalysis``.

    Returns:
        Flat JSON-safe dict. Same conventions as get_quote_handler.
    """
    if not isinstance(tool_input, dict):
        tool_input = {}

    symbol_raw = tool_input.get("symbol")
    if not isinstance(symbol_raw, str) or not symbol_raw.strip():
        logger.info("get_premarket: missing symbol (input=%r)", tool_input)
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": "symbol is required",
        }

    symbol = symbol_raw.strip().upper()
    if len(symbol) > 8 or not all(c.isalnum() or c in (".", "-") for c in symbol):
        logger.info(
            "get_premarket: symbol %r looks like free text, not a ticker", symbol,
        )
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": (
                f"{symbol!r} doesn't look like a ticker. Pass a ticker "
                "like NVDA, TSLA, SPY — not a company name."
            ),
        }

    target_date_raw = tool_input.get("target_date")
    target_date: str | None = None
    if target_date_raw is not None:
        if not isinstance(target_date_raw, str) or not _ISO_DATE.match(
            target_date_raw.strip()
        ):
            logger.info(
                "get_premarket: bad target_date=%r", target_date_raw,
            )
            return {
                "ok": False,
                "code": "BAD_ARGS",
                "message": (
                    "target_date must be YYYY-MM-DD "
                    f"(got {target_date_raw!r})"
                ),
            }
        target_date = target_date_raw.strip()

    finalysis = getattr(app_state, "finalysis", None)
    if finalysis is None:
        logger.error("get_premarket: app_state.finalysis not configured")
        return {
            "ok": False,
            "code": "FINALYSIS_ERROR",
            "message": "premarket service not configured",
        }

    logger.info(
        "get_premarket: fetching symbol=%s target_date=%s", symbol, target_date,
    )
    raw = await finalysis.get_premarket_levels(symbol, target_date=target_date)

    # Disambiguate two distinct "error" meanings on this endpoint:
    #
    # 1. Client-wrapper errors from :class:`FinalysisClient._request`.
    #    These always set ``error`` to one of three known sentinel
    #    strings and never include the domain fields
    #    (``premarket_high`` etc.):
    #      - ``"http_error"``  → {"error": "http_error", "status": 4xx/5xx, ...}
    #      - ``"request_failed"`` → transport/DNS/timeout
    #      - ``"invalid_json"``   → server returned non-JSON
    #
    # 2. Finalysis's own payload includes ``"error": null`` as a
    #    literal success marker alongside the premarket levels. A
    #    non-null ``error`` on a 200 response means the symbol is
    #    valid but the target session had no pre-market activity —
    #    surfaced below as ``NO_DATA`` so Nova can narrate honestly.
    if isinstance(raw, dict):
        err_val = raw.get("error")
        is_client_wrapper_error = (
            isinstance(err_val, str)
            and err_val in ("http_error", "request_failed", "invalid_json")
        )
        if is_client_wrapper_error:
            status = raw.get("status")
            detail = raw.get("detail")
            is_http_client_err = (
                err_val == "http_error"
                and isinstance(status, int)
                and 400 <= status < 500
            )
            logger.info(
                "get_premarket: finalysis failure kind=%r status=%r detail=%.160s",
                err_val, status, repr(detail),
            )
            if is_http_client_err:
                return {
                    "ok": False,
                    "code": "BAD_ARGS",
                    "message": f"no premarket data for {symbol!r} ({status})",
                }
            return {
                "ok": False,
                "code": "FINALYSIS_ERROR",
                "message": f"premarket service unavailable for {symbol!r}",
            }

    if not isinstance(raw, dict):
        logger.warning(
            "get_premarket: unexpected response shape: %r",
            type(raw).__name__,
        )
        return {
            "ok": False,
            "code": "FINALYSIS_ERROR",
            "message": "premarket service returned malformed data",
        }

    # Finalysis's /premarket/levels returns HTTP 200 with a non-null
    # ``error`` field when the symbol is valid but the session had no
    # pre-market bars (illiquid / low-volume tickers). Detect by the
    # combined signal: error is non-null AND the domain anchors are
    # missing from the payload.
    inline_error = raw.get("error")
    if inline_error and "premarket_high" not in raw:
        logger.info(
            "get_premarket: finalysis inline error for %s: %s",
            symbol, str(inline_error)[:160],
        )
        return {
            "ok": False,
            "code": "NO_DATA",
            "message": f"no premarket activity for {symbol!r}",
        }

    # All three price anchors null → effectively no data.
    if (raw.get("premarket_high") is None
            and raw.get("premarket_low") is None
            and raw.get("premarket_open") is None):
        logger.info("get_premarket: all price fields null for %s", symbol)
        return {
            "ok": False,
            "code": "NO_DATA",
            "message": f"no premarket activity for {symbol!r}",
        }

    return {
        "ok": True,
        "symbol": raw.get("symbol", symbol),
        "target_date": raw.get("target_date"),
        "premarket_high": raw.get("premarket_high"),
        "premarket_low": raw.get("premarket_low"),
        "premarket_open": raw.get("premarket_open"),
        "premarket_close": raw.get("premarket_close"),
        "premarket_range_pct": raw.get("premarket_range_pct"),
        "premarket_volume": raw.get("premarket_volume"),
        "previous_close": raw.get("previous_close"),
        "gap_pct": raw.get("gap_pct"),
        "speech_hint": _SPEECH_HINT,
    }
