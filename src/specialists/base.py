"""Specialist-agent contracts (the modular extension).

Three types live here, forming the contract any Session B specialist
must implement:

- :class:`SpecialistAgent` — immutable declarative config (id,
  display_name, voice, prompt path, tool defs, trigger examples, …)
  loaded from ``src/specialists/agents/<id>.py``.
- :class:`SpecialistToolkit` — abstract base class with three required
  methods (``fetch_data``, ``transform_data``, ``compute_stats``) and
  three defaulted methods (``generate_chart``, ``compose_summary``,
  ``render_report``, ``end_session``) provided by
  :class:`SharedToolkitMixin` in :mod:`src.specialists.toolkits.shared`.
- :class:`ToolContext` — per-invocation bag of runtime handles passed
  to every toolkit method so the toolkit doesn't import app.state
  globals.

See README.md § "Customization: build your own specialist" for the
full walkthrough. v1 ships with a single registered specialist
(``financial`` / Carlos) but the whole Session B dispatcher is written
against this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models.financial import FetchResult, TransformResult  # re-exported


if TYPE_CHECKING:
    from src.clients.antv_chart import AntvChartClient
    from src.clients.bedrock_router import BedrockRouterClient
    from src.clients.visor import VisorClient
    from src.render.report import ReportRenderer
    from src.state.data_handles import DataHandleStore


__all__ = [
    "FetchResult",
    "TransformResult",
    "SpecialistAgent",
    "ToolContext",
    "SpecialistToolkit",
]


# ─────────────────────────────────────────────────────────────
# SpecialistAgent — declarative config
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpecialistAgent:
    """Declarative configuration for a subordinate voice agent (Session B).

    Immutable. One instance per registered specialist. The session
    manager, handoff tool, and Session A prompt catalog all read from
    these fields without writing.

    Attributes:
        id: Stable unique identifier (``"financial"``, ``"legal"``, …).
            Used in tool results, registry lookup, and the
            ``handoff_to_specialist(agent_id, …)`` enum.
        display_name: Persona name spoken and shown in UI (``"Carlos"``).
        description: One-line blurb rendered into Session A's prompt
            catalog (``"financial analyst (stocks, indicators, …)"``).
        voice_id: Nova Sonic voice for Session B. MUST differ from
            Session A's voice so the audience hears two speakers.
        locale: Output locale for Session B narration and report
            (``"es-419"`` in v1).
        system_prompt_path: Absolute path to the specialist's
            ``.md`` system prompt (loaded at startup).
        tool_defs: Nova Sonic ``toolConfiguration.tools[]`` list for
            Session B. Each specialist can customize tool names +
            schemas; the generic tool-name strategy (``fetch_data``,
            ``transform_data``, …) keeps Python handlers reusable.
        visor_phases: 5 phase labels in the specialist's locale,
            used when calling ``VisorClient.start(phases=…)``.
        terminator_phrases: Lowercase strings whose presence in
            Session B's text output triggers handback. The session
            manager does case-insensitive substring matching.
        report_template_path: Path to the HTML template for this
            specialist's reports (``reports/templates/<id>.html``).
        toolkit_class_path: Dotted import path of the
            :class:`SpecialistToolkit` subclass that implements this
            specialist's domain logic.
        trigger_examples: ``{locale_2char → [phrase, …]}`` — used to
            render Session A's specialist catalog per locale.
        handoff_lines: ``{locale_2char → {personality → "ok, Carlos …"}}``
            — the one-line intro Session A speaks during handoff.
        typical_duration_seconds: For observability dashboards only.
        concurrency_limit: Max concurrent instances of this
            specialist. v1 enforces the global 1-handoff-at-a-time cap
            so this is informational.
        supported_locales: The locales this specialist can output in.
            v1 uses just ``{locale}``; multi-locale is v1.1.
    """

    id: str
    display_name: str
    description: str
    voice_id: str
    locale: str
    system_prompt_path: Path
    tool_defs: list[dict]
    visor_phases: list[str]
    terminator_phrases: list[str]
    report_template_path: Path
    toolkit_class_path: str
    trigger_examples: dict[str, list[str]]
    handoff_lines: dict[str, dict[str, str]]
    typical_duration_seconds: int = 25
    concurrency_limit: int = 1
    supported_locales: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self._validate()

    # Validation kept as a method so subclasses / tests can reuse it.
    def _validate(self) -> None:
        # id — simple alnum + underscores/dashes
        if not self.id:
            raise ValueError("SpecialistAgent.id must be non-empty")
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
        if not all(c in allowed for c in self.id):
            raise ValueError(
                f"SpecialistAgent.id must be lowercase alnum+_/-, got {self.id!r}"
            )

        if not self.display_name.strip():
            raise ValueError("display_name must be non-empty")
        if not self.description.strip():
            raise ValueError("description must be non-empty")
        if not self.voice_id.strip():
            raise ValueError("voice_id must be non-empty")
        if not self.locale:
            raise ValueError("locale must be non-empty")

        if not self.system_prompt_path.exists():
            raise ValueError(f"system_prompt_path not found: {self.system_prompt_path}")
        if not self.report_template_path.exists():
            raise ValueError(f"report_template_path not found: {self.report_template_path}")

        if not self.tool_defs:
            raise ValueError("tool_defs must be non-empty")
        if len(self.visor_phases) < 3:
            raise ValueError(f"visor_phases must have ≥ 3 entries, got {len(self.visor_phases)}")
        if not self.terminator_phrases:
            raise ValueError("terminator_phrases must be non-empty")
        if any(p != p.lower() for p in self.terminator_phrases):
            raise ValueError("terminator_phrases must all be lowercase")

        if "." not in self.toolkit_class_path:
            raise ValueError(
                f"toolkit_class_path must be dotted, e.g. "
                f"'src.specialists.toolkits.financial.FinancialToolkit', got "
                f"{self.toolkit_class_path!r}"
            )

        if not self.trigger_examples:
            raise ValueError("trigger_examples must have at least one locale")

    # Convenience helpers used by the prompt catalog renderer and UI.

    def trigger_phrases_for_locale(self, locale: str) -> list[str]:
        """Return the trigger examples for the given locale, falling back
        to ``"en"`` then any available locale."""
        key = (locale or "en")[:2]
        if key in self.trigger_examples:
            return list(self.trigger_examples[key])
        if "en" in self.trigger_examples:
            return list(self.trigger_examples["en"])
        first = next(iter(self.trigger_examples.values()))
        return list(first)

    def handoff_line_for(self, locale: str, personality: str) -> str | None:
        """Pick the handoff line matching ``(locale, personality)`` with
        sensible fallbacks. Returns ``None`` when no entry exists."""
        key = (locale or "en")[:2]
        by_locale = self.handoff_lines.get(key) or self.handoff_lines.get("en") or {}
        if personality in by_locale:
            return by_locale[personality]
        # Fallback order inside a locale.
        for fallback in ("warm_brief", "concise", "professional"):
            if fallback in by_locale:
                return by_locale[fallback]
        if by_locale:
            return next(iter(by_locale.values()))
        return None


# ─────────────────────────────────────────────────────────────
# ToolContext — per-call bag of runtime handles
# ─────────────────────────────────────────────────────────────

@dataclass
class ToolContext:
    """Passed to every :class:`SpecialistToolkit` method.

    Keeping runtime handles in a context object (rather than having
    toolkits pull from a global ``app.state``) makes toolkits easy to
    unit-test in isolation.

    Fields are intentionally typed as optional-lookalikes on purpose —
    tests can build a ``ToolContext`` with only the fields they need.
    """

    agent_id: str
    specialist: SpecialistAgent

    # Shared runtime handles — the Python dispatcher fills these from
    # app.state before calling into the toolkit.
    visor: "VisorClient"
    data_handles: "DataHandleStore"
    bedrock_router: "BedrockRouterClient"
    antv_chart: "AntvChartClient"
    report_renderer: "ReportRenderer"

    # Correlation IDs (optional, filled by the dispatcher).
    browser_session_id: str | None = None
    tool_use_id: str | None = None

    # ── convenience helpers (keep toolkits terse) ────────────

    async def phase(
        self,
        index: int,
        *,
        substep: str | None = None,
        label: str | None = None,
        status: str = "active",
    ) -> None:
        """POST progress to the visor without importing VisorClient."""
        await self.visor.phase(
            index, label=label, substep=substep, status=status,
        )

    async def put_handle(self, prefix: str, value: Any) -> str:
        """Store ``value`` in ``data_handles`` and return the new handle."""
        return await self.data_handles.put(prefix, value)

    async def get_handle(self, handle: str) -> Any | None:
        """Retrieve a previously-stored value by handle. ``None`` on miss."""
        return await self.data_handles.get(handle)


# ─────────────────────────────────────────────────────────────
# SpecialistToolkit — abstract base class
# ─────────────────────────────────────────────────────────────

class SpecialistToolkit(ABC):
    """Abstract base for domain-specific logic.

    Subclasses MUST implement :meth:`fetch_data`, :meth:`transform_data`,
    and :meth:`compute_stats`. The other three tool methods
    (:meth:`generate_chart`, :meth:`compose_summary`, :meth:`render_report`)
    plus :meth:`end_session` have generic defaults supplied by
    :class:`SharedToolkitMixin` — inherit from both to get the defaults.
    """

    @abstractmethod
    async def fetch_data(
        self, *, params: dict[str, Any], ctx: ToolContext,
    ) -> FetchResult:
        """Domain-specific data fetch.

        Implementations must:

        1. POST ``phase(0)`` to the visor with a short substep.
        2. Call the domain's data source (HTTP, DB, file, …).
        3. Store the raw response via ``ctx.put_handle("fn", data)``.
        4. Return a compact ``FetchResult`` summary — NEVER inline the
           raw data into the return value (keeps Session B's context lean).
        """

    @abstractmethod
    async def transform_data(
        self, *, handle: str, target: str, ctx: ToolContext,
    ) -> TransformResult:
        """Shape the fetched data for the chosen chart type.

        Implementations must:

        1. POST ``phase(1)``.
        2. Read the raw data via ``ctx.get_handle(handle)``.
        3. Produce an AntV-compatible array.
        4. Store under a new ``"td-…"`` handle.
        """

    @abstractmethod
    async def compute_stats(
        self, *, handle: str, ctx: ToolContext,
    ) -> dict[str, Any]:
        """Compute the numeric facts ``compose_summary`` will ground on.

        Called by the shared ``compose_summary`` implementation before
        invoking Sonnet. Domain-specific — financial might return
        ``{first, last, high, low, pct_change}``; legal might return
        ``{clause_count, risk_score, avg_redline_depth}``.
        """

    # The remaining four methods — ``generate_chart``, ``compose_summary``,
    # ``render_report``, ``end_session`` — are intentionally NOT declared
    # here so that :class:`SharedToolkitMixin` can supply them via normal
    # Python MRO without being shadowed.  If you subclass
    # :class:`SpecialistToolkit` without :class:`SharedToolkitMixin`, you
    # are expected to implement them yourself — Python will surface the
    # usual ``AttributeError`` at call time.
