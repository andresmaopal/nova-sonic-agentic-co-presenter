"""Data models for the financial pipeline (Session B tools + renderer).

These are plain frozen dataclasses — no runtime behavior, just typed
containers that the tool handlers, renderer, and tests pass around.

Separate from :mod:`src.models.slide_data` (which covers the
presentation layer) and :mod:`src.models.session_config` (the Nova
Sonic session config) so the financial pipeline's types stay isolated
and easy to swap if we later generalize to non-financial specialists
per ``modular-extension.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


# ─────────────────────────────────────────────────────────────
# Pipeline phases (canonical enum used by the visor + session B tools)
# ─────────────────────────────────────────────────────────────

class PipelinePhase(IntEnum):
    """The 6 canonical phases of a financial report pipeline.

    Index values are used when POSTing ``/api/phase {index, substep}``
    to the visor, so they MUST match the order of the phase labels in
    the specialist's ``visor_phases`` list.
    """

    FETCH = 0          # Consultando Finalysis API
    TRANSFORM = 1      # Transformando series temporales
    CHART = 2          # Seleccionando y construyendo gráfica
    SUMMARY = 3        # Componiendo resumen ejecutivo (Sonnet)
    AUDIT = 4          # Auditando resultados con Agente revisor (mock)
    RENDER = 5         # Ensamblando reporte


# ─────────────────────────────────────────────────────────────
# Handle wrappers (opaque references into DataHandleStore)
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DataHandle:
    """Typed wrapper over a raw ``DataHandleStore`` string.

    Only used at API boundaries where we want self-documenting types;
    internally the tool handlers still pass the plain string through.
    """

    handle: str

    def __post_init__(self) -> None:
        if "-" not in self.handle:
            raise ValueError(
                f"handle must be '<prefix>-<hex>', got {self.handle!r}"
            )

    @property
    def prefix(self) -> str:
        return self.handle.split("-", 1)[0]


# ─────────────────────────────────────────────────────────────
# Tool-result value types (returned by SpecialistToolkit methods)
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FetchResult:
    """Value returned by ``SpecialistToolkit.fetch_data``.

    On success: ``ok=True``, ``handle`` and ``summary`` populated.
    On failure: ``ok=False``, ``code`` and ``message`` populated.
    """

    ok: bool
    handle: str | None = None
    summary: dict[str, Any] | None = None
    count: int | None = None
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe serialization for Nova Sonic tool results."""
        out: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            out["handle"] = self.handle
            out["summary"] = self.summary
            out["count"] = self.count
        else:
            out["code"] = self.code
            out["message"] = self.message
        return out


@dataclass(frozen=True)
class TransformResult:
    """Value returned by ``SpecialistToolkit.transform_data``."""

    ok: bool
    handle: str | None = None
    points: int | None = None
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            out["handle"] = self.handle
            out["points"] = self.points
        else:
            out["code"] = self.code
            out["message"] = self.message
        return out


# ─────────────────────────────────────────────────────────────
# Report assembly bundle (passed to ReportRenderer)
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReportBundle:
    """Everything the ``ReportRenderer`` needs to materialize one report.

    Built by ``Session B``'s ``render_report`` tool from the
    accumulated tool results. The renderer validates and writes
    ``reports/<slug>-<YYYY-MM-DD>.html``.
    """

    customer_name: str
    description: str
    chart_url: str          # https:// or inline data:image/...;base64,...
    chart_title: str
    bullets: list[str]      # 3–5 strings
    slug: str
    report_date: str        # ISO YYYY-MM-DD
    footer_note: str = "Generado con Finalysis + AntV + Kiro"

    def __post_init__(self) -> None:
        if not self.customer_name.strip():
            raise ValueError("customer_name must be non-empty")
        if not self.description.strip():
            raise ValueError("description must be non-empty")
        # Accept either a remote https URL or an inlined ``data:image/…``
        # URI. The latter is the eviction-proof shape produced by
        # ``AntvChartClient._download_chart_as_data_uri`` since 2026-05-18
        # (see incident log: TSLA/NVDA chart evicted from Alipay CDN ~5
        # minutes after generation, leaving a broken <img> in the report).
        # Anything else (http://, file://, raw paths) is rejected to keep
        # the report HTML self-contained and free of mixed-content warnings.
        if not (
            self.chart_url.startswith("https://")
            or self.chart_url.startswith("data:image/")
        ):
            raise ValueError(
                "chart_url must start with https:// or data:image/, "
                f"got {self.chart_url[:60]!r}"
            )
        # 2026-05-10: relaxed from 3-5 to 3-8 to accommodate multi-symbol
        # comparisons. Carlos calls compose_summary once per symbol (3-5
        # bullets each) and concatenates into a single render_report call.
        # For a typical AMZN-vs-MSFT comparison that's 6-10 bullets. The
        # 3-5 cap was dropping any comparison report on the floor
        # ((internal postmortem 2026-05-10) forthcoming).
        # Upper bound of 8 keeps the two-slide template layout legible; the
        # shared toolkit can truncate if Sonnet returns more.
        if not (3 <= len(self.bullets) <= 8):
            raise ValueError(
                f"bullets must contain 3-8 entries, got {len(self.bullets)}"
            )
        if any(not b.strip() for b in self.bullets):
            raise ValueError("bullets must not contain empty/whitespace entries")
        if not self.slug.strip():
            raise ValueError("slug must be non-empty")
        # Lightweight ISO check — full parsing is the caller's job.
        if len(self.report_date) != 10 or self.report_date.count("-") != 2:
            raise ValueError(
                f"report_date must be YYYY-MM-DD, got {self.report_date!r}"
            )
