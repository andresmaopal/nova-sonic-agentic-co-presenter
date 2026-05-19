"""Financial specialist declaration.

This file is the **only** thing the ``AgentRegistry.auto_discover``
needs to pick up the financial specialist. It exports:

- ``AGENT``  — a :class:`SpecialistAgent` describing Carlos.
- ``TOOLKIT_FACTORY`` — a callable ``(clients) -> FinancialToolkit``.

Adding a second specialist (e.g. ``legal``) means dropping a sibling
file in this directory with the same two exports. No other code change
is required to make the new specialist appear in Session A's prompt
catalog or in the ``handoff_to_specialist`` tool enum.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Repository root: src/specialists/agents/financial.py → ../../../..  = repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]


# ─────────────────────────────────────────────────────────────
# Nova Sonic tool schemas for Session B
# ─────────────────────────────────────────────────────────────

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "fetch_data",
            "description": (
                "Fetch market data from Finalysis for a given symbol, "
                "indicator, and date range. Use this first. Returns an "
                "opaque handle (fn-...) you must pass to transform_data. "
                "For multi-series comparisons, pass 'symbols' (e.g., "
                "['AMZN','MSFT']) OR 'windows' (e.g., [20,50]) — never "
                "both, never more than 6, and only for kind=trend/"
                "momentum/volatility/volume."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["trend", "momentum", "volatility", "volume",
                                 "catalyst", "volume_comparison",
                                 "premarket", "quote", "raw"],
                        "description": "Which family of Finalysis endpoint to call.",
                    },
                    "indicator": {
                        "type": "string",
                        "description": (
                            "The specific indicator within the family "
                            "(sma, ema, rsi, macd, bollinger, obv, rvol, "
                            "gap-analysis, ...). Ignored for 'quote' and "
                            "'premarket'."
                        ),
                    },
                    "symbol": {"type": "string",
                                "description": "Uppercase ticker, e.g. TSLA"},
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "OPTIONAL. Use instead of 'symbol' to fan out "
                            "across multiple tickers for a multi-line "
                            "comparison chart (e.g., ['AMZN','MSFT']). "
                            "Max 6. Incompatible with 'symbol' and with "
                            "a multi-valued 'windows'."
                        ),
                    },
                    "start_date": {"type": "string",
                                    "description": "YYYY-MM-DD; default 6 months ago"},
                    "end_date":   {"type": "string",
                                    "description": "YYYY-MM-DD; default today"},
                    "window":     {"type": "integer"},
                    "windows": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "OPTIONAL. Use instead of 'window' to fan out "
                            "a single indicator across multiple window "
                            "sizes for a multi-line chart (e.g., [20,50] "
                            "for EMA-20 vs EMA-50). Max 6. Incompatible "
                            "with 'window' and with a multi-valued "
                            "'symbols'."
                        ),
                    },
                    "extra_params": {
                        "type": "object",
                        "description": "Any additional Finalysis params.",
                    },
                },
                "required": ["kind"],
            })},
        }
    },
    {
        "toolSpec": {
            "name": "transform_data",
            "description": (
                "Reshape Finalysis data for the chosen chart type. "
                "Returns a new handle (td-...). Call after fetch_data."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "target": {
                        "type": "string",
                        "enum": ["line_single", "line_multi",
                                 "bar_ranked", "column_categorical",
                                 "dual_axes", "histogram", "pie"],
                    },
                    "series_label": {"type": "string"},
                },
                "required": ["handle", "target"],
            })},
        }
    },
    {
        "toolSpec": {
            "name": "generate_chart",
            "description": (
                "Render the AntV chart for a transformed data handle. "
                "Returns an https:// image URL."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {
                    "handle":       {"type": "string"},
                    "tool_name":    {"type": "string"},
                    "title":        {"type": "string"},
                    "axis_x_title": {"type": "string"},
                    "axis_y_title": {"type": "string"},
                },
                "required": ["handle", "tool_name", "title"],
            })},
        }
    },
    {
        "toolSpec": {
            "name": "compose_summary",
            "description": (
                "Produce 3-5 es-419 executive-summary bullets grounded in "
                "the data handle's stats. Calls Claude Sonnet. Takes ~3 s."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {
                    "handle":        {"type": "string"},
                    "customer_name": {"type": "string"},
                    "description":   {"type": "string"},
                    "narrative":     {"type": "string"},
                },
                "required": ["handle", "customer_name", "description"],
            })},
        }
    },
    {
        "toolSpec": {
            "name": "render_report",
            "description": (
                "Write the two-slide HTML report to disk. The visor "
                "auto-swaps when the file lands."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "description":   {"type": "string"},
                    "chart_url":     {"type": "string"},
                    "chart_title":   {"type": "string"},
                    "bullets":       {"type": "array",
                                       "items": {"type": "string"}},
                    "slug":          {"type": "string"},
                },
                "required": ["customer_name", "description", "chart_url",
                              "chart_title", "bullets", "slug"],
            })},
        }
    },
    {
        "toolSpec": {
            "name": "end_session",
            "description": (
                "Signal to the session manager that the analysis is "
                "complete. Call exactly once, at the end of every run."
            ),
            "inputSchema": {"json": json.dumps({
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            })},
        }
    },
]


# ─────────────────────────────────────────────────────────────
# Handoff lines — per-locale, per-personality intro that Session A
# speaks immediately before Session B takes the floor.
# ─────────────────────────────────────────────────────────────

_HANDOFF_LINES: dict[str, dict[str, str]] = {
    "en": {
        "concise": "ok, Carlos up",
        "warm_brief": "ok, let me bring in Carlos",
        "charismatic": "ok, over to Carlos for the numbers",
        "professional": "ok, handing off to the analyst",
        "professional_detailed": "ok, passing the floor to the financial analyst",
    },
    "es": {
        "concise": "ok, Carlos",
        "warm_brief": "ok, Carlos te atiende",
        "charismatic": "ok, adelante Carlos con los números",
        "professional": "ok, paso al analista",
        "professional_detailed": "ok, paso la palabra al analista financiero",
    },
    "fr": {
        "concise": "ok, Carlos prend",
        "warm_brief": "ok, je passe à Carlos",
        "charismatic": "ok, place à Carlos pour les chiffres",
        "professional": "ok, transition à l'analyste",
        "professional_detailed": "ok, je passe la parole à l'analyste financier",
    },
    "de": {
        "concise": "ok, Carlos übernimmt",
        "warm_brief": "ok, ich hole Carlos dazu",
        "charismatic": "ok, Bühne frei für Carlos mit den Zahlen",
        "professional": "ok, Übergabe an den Analysten",
        "professional_detailed": "ok, ich übergebe an den Finanzanalysten",
    },
    "pt": {
        "concise": "ok, Carlos assume",
        "warm_brief": "ok, passo pro Carlos",
        "charismatic": "ok, e quem entra agora é o Carlos com os números",
        "professional": "ok, transição ao analista",
        "professional_detailed": "ok, passo a palavra ao analista financeiro",
    },
    "it": {
        "concise": "ok, Carlos",
        "warm_brief": "ok, faccio entrare Carlos",
        "charismatic": "ok, tocca a Carlos con i numeri",
        "professional": "ok, passo all'analista",
        "professional_detailed": "ok, lascio la parola all'analista finanziario",
    },
    "hi": {
        "concise": "ठीक है, अब कार्लोस",
        "warm_brief": "ठीक है, कार्लोस को लाता हूँ",
        "charismatic": "ठीक है, अब कार्लोस संख्याओं के साथ",
        "professional": "ठीक है, विश्लेषक को सौंपता हूँ",
        "professional_detailed": "ठीक है, वित्तीय विश्लेषक को बात देता हूँ",
    },
}


# ─────────────────────────────────────────────────────────────
# AGENT — the public export consumed by AgentRegistry.auto_discover
# ─────────────────────────────────────────────────────────────

# NOTE: `from src.specialists.base import SpecialistAgent` is intentionally
# deferred to below the module-level constants so the file stays cheap to
# import even if someone pokes at _TOOL_DEFS or _HANDOFF_LINES for docs.
from src.specialists.base import SpecialistAgent  # noqa: E402


AGENT: SpecialistAgent = SpecialistAgent(
    id="financial",
    display_name="Carlos",
    description="financial analyst (stocks, indicators, screeners, catalysts)",
    voice_id="carlos",
    locale="es-419",
    system_prompt_path=_REPO_ROOT / "src" / "prompts" / "specialists" / "financial.md",
    tool_defs=_TOOL_DEFS,
    visor_phases=[
        "Consultando Finalysis API",
        "Transformando series temporales",
        "Seleccionando y construyendo gráfica",
        "Componiendo resumen ejecutivo (Sonnet)",
        "Auditando resultados con Agente revisor",
        "Ensamblando reporte",
    ],
    terminator_phrases=[
        "reporte en pantalla",
        "report on screen",
        "listo, está en pantalla",
        "informe listo",
    ],
    report_template_path=_REPO_ROOT / "reports" / "templates" / "financial.html",
    toolkit_class_path="src.specialists.toolkits.financial.FinancialToolkit",
    trigger_examples={
        "en": [
            # Top 4 — these survive the [:4] truncation in the registry
            # catalog that Session A sees. Kept deliberately generic so
            # any phrasing involving a chart / report / compare / visor
            # maps to the financial specialist even when the user doesn't
            # name a ticker.
            "generate a report on [company/index]",
            "pull up a chart of [asset] vs [asset]",
            "compare the behavior of [X] and [Y]",
            "bring up the visor / open the web visor",
            # Ticker-specific examples (still useful as secondary cues).
            "pull up Tesla's SMA", "show RSI for Apple",
            "volume gainers today", "run the analysis on Microsoft",
            "show me how NVDA has behaved this week",
        ],
        "es": [
            # Top 4 — survive the catalog truncation.
            "genera un reporte de [empresa/índice]",
            "saca un gráfico de [activo] vs [activo]",
            "compara el comportamiento de [X] y [Y]",
            "trae el visor / abre el web visor",
            # Ticker-specific examples (secundarios).
            "saca el análisis de TSLA", "muéstrame el RSI de Apple",
            "top volume gainers de hoy", "corre el análisis de Microsoft",
            "muéstrame cómo se ha comportado el IPC esta semana",
        ],
        "fr": [
            "génère un rapport sur [entreprise]",
            "sors un graphique de [actif] vs [actif]",
            "compare le comportement de [X] et [Y]",
            "ouvre le visor",
            "sors l'analyse de Tesla", "montre le RSI d'Apple",
        ],
        "de": [
            "erstelle einen Bericht über [Unternehmen]",
            "zeig mir ein Chart von [Asset] vs [Asset]",
            "vergleiche [X] und [Y]",
            "öffne den Visor",
            "zeig mir die Analyse von Tesla", "zeig Apples RSI",
        ],
        "pt": [
            "gera um relatório de [empresa]",
            "mostra um gráfico de [ativo] vs [ativo]",
            "compara o comportamento de [X] e [Y]",
            "abre o visor",
            "saca a análise de Tesla", "mostra o RSI da Apple",
        ],
        "it": [
            "genera un report su [azienda]",
            "mostrami un grafico di [asset] vs [asset]",
            "confronta il comportamento di [X] e [Y]",
            "apri il visor",
            "mostra l'analisi di Tesla", "fammi vedere l'RSI di Apple",
        ],
        "hi": [
            "एक रिपोर्ट बनाओ",
            "[X] और [Y] की तुलना करो",
            "विज़र खोलो",
            "टेस्ला का विश्लेषण दिखाओ",
        ],
    },
    handoff_lines=_HANDOFF_LINES,
    typical_duration_seconds=25,
    concurrency_limit=1,
    supported_locales=frozenset({"es-419"}),
)


# ─────────────────────────────────────────────────────────────
# TOOLKIT_FACTORY — builds the concrete toolkit with shared clients
# ─────────────────────────────────────────────────────────────


def TOOLKIT_FACTORY(clients: dict[str, Any]):
    """Build a :class:`FinancialToolkit` using shared clients from app.state.

    Expected keys in ``clients``:

    - ``"finalysis"`` — :class:`~src.clients.finalysis.FinalysisClient`
    - ``"bedrock_router"`` — :class:`~src.clients.bedrock_router.BedrockRouterClient`
      (optional; required only for ``line_multi`` transforms).
    """
    from src.specialists.toolkits.financial import FinancialToolkit

    try:
        finalysis = clients["finalysis"]
    except KeyError:
        raise ValueError(
            "financial specialist requires a 'finalysis' client in "
            "AgentRegistry.attach_toolkits(clients=...)"
        ) from None

    return FinancialToolkit(
        finalysis=finalysis,
        bedrock_router=clients.get("bedrock_router"),
    )
