"""``get_quote`` — Session A spot-price tool.

Returns a live quote snapshot (bid / ask / last / session / timestamp)
for a single symbol. Wired for Nova (Session A) so she can answer
instant "what's X trading at?" questions *without* spinning up the
specialist pipeline (no Session B stream, no visor, no chart, no
report). Reply latency target: ~1.5 s end-to-end (Finalysis round-trip
+ Bedrock TTS).

Companion: :mod:`src.tools.get_premarket` for pre-market levels.

Design rationale: the Finalysis ``/quote/current`` endpoint returns a
single-point snapshot — there is no time series to visualize. Going
through ``handoff_to_specialist`` for this is pure overhead (session
spin-up, voice change to Carlos, visor phase flashes, ~3-4 s latency).
Handling it directly in Nova keeps the voice consistent and the
audience focused on the slideshow.

Response shape (JSON-safe, ready for a Nova Sonic ``toolResult``)::

    {"ok": True,
     "symbol": "AMZN",
     "last": 186.42,
     "bid": 186.40,
     "ask": 186.44,
     "session": "closed",        # or "regular" / "pre" / "post"
     "premarket_price": null,
     "timestamp": "2026-05-12T20:57:44Z",
     "speech_hint": "..."}        # one-sentence narration template

Errors never raise — all failure paths return
``{"ok": False, "code": "...", "message": "..."}``.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# Index → liquid ETF proxy alias map. Finalysis covers US equities and
# ETFs, not the underlying indices. When Nova emits get_quote with an
# index ticker (SPX for S&P 500, NDX for Nasdaq-100, DJI for Dow,
# VIX for volatility, RUT for Russell 2000) Finalysis returns 404 and
# the audience hears a confused "no data" from Nova. Auto-translate to
# the most liquid ETF proxy so the user gets a useful price.
#
# The alias is silent from the user's perspective: the return payload
# reports the ETF ticker we actually queried, so Nova narrates "SPY
# está en 580" rather than pretending to quote the index itself. Her
# prompt teaches her to narrate the underlying asset by its natural
# name ("S&P 500", "Nasdaq") — consistent with the handoff-to-Carlos
# mapping in ``src/prompts/specialists/financial.md``.
#
# Keep this list CONSERVATIVE: only include unambiguous cases where
# there is one canonical ETF proxy. For assets with multiple competing
# ETFs (e.g., gold GLD vs IAU, small-caps IWM vs VB) we pick the most
# liquid and widely-quoted option.
_INDEX_TO_ETF_ALIAS: dict[str, str] = {
    "SPX":   "SPY",    # S&P 500 index → SPY
    "^SPX":  "SPY",
    ".SPX":  "SPY",
    "NDX":   "QQQ",    # Nasdaq-100 index → QQQ
    "^NDX":  "QQQ",
    "DJI":   "DIA",    # Dow Jones Industrial Average → DIA
    "^DJI":  "DIA",
    "DJIA":  "DIA",
    "VIX":   "VXX",    # CBOE Volatility Index → VXX (short-vol ETF)
    "^VIX":  "VXX",
    "RUT":   "IWM",    # Russell 2000 → IWM
    "^RUT":  "IWM",
}


def _resolve_index_alias(symbol: str) -> tuple[str, str | None]:
    """If ``symbol`` is a known non-tradeable index ticker, return
    ``(aliased_etf, original)``. Otherwise return ``(symbol, None)``.

    The caller uses the returned tuple to fire the Finalysis call
    against the ETF while logging what the original input was. Case
    is already upper-normalized by the caller.
    """
    if symbol in _INDEX_TO_ETF_ALIAS:
        return _INDEX_TO_ETF_ALIAS[symbol], symbol
    return symbol, None


# Narration template baked into the tool result so Nova's voice model
# emits a consistent, concise utterance without re-inventing phrasing
# every turn. Matches the "reply with ONE short sentence" pattern
# used by ``switch_window`` (``speech_hint``).
_SPEECH_HINT_OPEN = (
    "Answer in ONE short sentence quoting ONLY the last price, "
    "session state, and symbol. Match the presenter's language. "
    "Example: 'Amazon está en 186.42, mercado abierto.' / "
    "'Amazon is at 186.42, market open.' Do NOT mention bid/ask "
    "unless the presenter explicitly asked for them. Do NOT offer "
    "to generate a report afterward — stay silent once done."
)
_SPEECH_HINT_CLOSED = (
    "Answer in ONE short sentence quoting ONLY the last price, "
    "that the session is CLOSED, and the symbol. Match the "
    "presenter's language. Example: 'Amazon cerró en 186.42.' / "
    "'Amazon closed at 186.42.' Do NOT mention bid/ask unless "
    "asked. Do NOT offer to generate a report afterward."
)


async def get_quote_handler(
    *,
    tool_input: dict,
    app_state: Any,
    tool_use_id: str | None = None,
    browser_session_id: str | None = None,
) -> dict[str, Any]:
    """Handle a ``get_quote`` tool call from Session A.

    Args:
        tool_input: ``{"symbol": "AMZN"}``. Symbol is normalized to
            uppercase; leading/trailing whitespace is stripped. Lower-
            case or mixed-case input is accepted — most voice models
            emit tickers lowercase.
        app_state: FastAPI ``app.state`` — must carry
            ``finalysis`` (:class:`FinalysisClient`).

    Returns:
        Flat JSON-safe dict. On success: the snapshot plus a
        ``speech_hint`` tuned to the session state. On failure: a
        ``{ok: False, code, message}`` dict — Nova narrates a brief
        apology.
    """
    symbol_raw = tool_input.get("symbol") if isinstance(tool_input, dict) else None
    if not isinstance(symbol_raw, str) or not symbol_raw.strip():
        logger.info("get_quote: missing symbol (input=%r)", tool_input)
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": "symbol is required",
        }

    symbol = symbol_raw.strip().upper()

    # Translate non-tradeable index tickers (SPX, NDX, DJI, VIX, RUT)
    # to their most-liquid ETF proxies (SPY, QQQ, DIA, VXX, IWM)
    # BEFORE the shape validator. Some index conventions use a "^" or
    # "." prefix (Yahoo: ^SPX, Refinitiv: .SPX) that would otherwise
    # be rejected by the alnum-only shape gate. Doing the alias first
    # both unblocks those inputs and keeps the downstream Finalysis
    # call working against a real ETF ticker.
    queried_symbol, original_index = _resolve_index_alias(symbol)
    if original_index is not None:
        logger.info(
            "get_quote: index alias %s → %s (finalysis covers ETFs, not indices)",
            original_index, queried_symbol,
        )
        # Reassign so the shape validator below sees the ETF
        # (which passes) instead of the original with the prefix
        # (which would be rejected).
        symbol = queried_symbol

    # Basic sanity: tickers are 1-6 alphanumerics (plus `.` for dual-class
    # shares like BRK.B). Anything longer is almost certainly a company
    # name that leaked through — route back to specialist.
    if len(symbol) > 8 or not all(c.isalnum() or c in (".", "-") for c in symbol):
        logger.info("get_quote: symbol %r looks like free text, not a ticker", symbol)
        return {
            "ok": False,
            "code": "BAD_ARGS",
            "message": (
                f"{symbol!r} doesn't look like a ticker symbol. "
                "Pass a ticker like AMZN, TSLA, SPY — not a company name."
            ),
        }

    finalysis = getattr(app_state, "finalysis", None)
    if finalysis is None:
        logger.error("get_quote: app_state.finalysis is not configured")
        return {
            "ok": False,
            "code": "FINALYSIS_ERROR",
            "message": "quote service not configured",
        }

    logger.info("get_quote: fetching symbol=%s", queried_symbol)
    raw = await finalysis.get_current_quote(queried_symbol)

    # FinalysisClient returns one of three client-wrapper error shapes
    # on failure (``http_error`` / ``request_failed`` / ``invalid_json``).
    # Match only those sentinel values so a legitimate success payload
    # that happens to include an ``"error"`` field (unlikely for
    # /quote/current, but shared convention on other endpoints) isn't
    # misclassified. Keeps parity with :mod:`src.tools.get_premarket`.
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
                "get_quote: finalysis failure kind=%r status=%r detail=%.160s",
                err_val, status, repr(detail),
            )
            # Error messages reference what the CALLER passed so Nova's
            # narration matches the user's ear ("no pude cotizar SPX")
            # rather than revealing the alias.
            user_facing_symbol = original_index or queried_symbol
            if is_http_client_err:
                return {
                    "ok": False,
                    "code": "BAD_ARGS",
                    "message": f"no quote available for {user_facing_symbol!r} ({status})",
                }
            return {
                "ok": False,
                "code": "FINALYSIS_ERROR",
                "message": f"quote service unavailable for {user_facing_symbol!r}",
            }

    if not isinstance(raw, dict):
        logger.warning("get_quote: unexpected response shape: %r", type(raw).__name__)
        return {
            "ok": False,
            "code": "FINALYSIS_ERROR",
            "message": "quote service returned malformed data",
        }

    # Pull the fields we care about; Finalysis may return nulls for
    # bid/ask outside regular session hours.
    session = (raw.get("session") or "").lower().strip() or "unknown"
    hint = _SPEECH_HINT_CLOSED if session == "closed" else _SPEECH_HINT_OPEN

    # If we translated an index ticker, tell Nova what actually got
    # quoted (SPY) AND what the presenter asked for (SPX). Her prompt
    # teaches her to narrate with the natural asset name — "S&P 500
    # está en 580" — not the ticker alias.
    out: dict[str, Any] = {
        "ok": True,
        "symbol": raw.get("symbol", queried_symbol),
        "last": raw.get("last"),
        "bid": raw.get("bid"),
        "ask": raw.get("ask"),
        "premarket_price": raw.get("premarket_price"),
        "session": session,
        "timestamp": raw.get("timestamp"),
        "speech_hint": hint,
    }
    if original_index is not None:
        out["requested_symbol"] = original_index
        out["alias_applied"] = True

    # Defensive: if Finalysis returns HTTP 200 but `last` is null, the
    # symbol is valid-but-stale (rare, but observed for illiquid
    # tickers after-hours). Surface as a soft error so Nova can say
    # "no recent trade" instead of quoting `null`.
    if out["last"] is None and out["bid"] is None and out["ask"] is None:
        user_facing_symbol = original_index or queried_symbol
        logger.info("get_quote: all price fields null for %s", queried_symbol)
        return {
            "ok": False,
            "code": "NO_DATA",
            "message": f"no recent price for {user_facing_symbol!r}",
        }

    return out
