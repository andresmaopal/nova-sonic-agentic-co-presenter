"""AgentRegistry — lookup table for registered SpecialistAgents.

The registry is built once at FastAPI startup by scanning
``src/specialists/agents/`` for every module that exposes:

- ``AGENT`` — a :class:`SpecialistAgent` instance.
- ``TOOLKIT_FACTORY`` — a callable ``(clients: dict) -> SpecialistToolkit``
  that builds the specialist's toolkit with the app's shared clients
  (FinalysisClient, BedrockRouterClient, …).

After auto-discovery, ``AgentRegistry.attach_toolkits(clients=...)``
invokes every factory so the registry is ready to serve handoffs.

The registry also renders the per-locale "specialist catalog" block
that Session A's system prompt injects via ``{SPECIALIST_CATALOG}``.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable

from src.specialists.base import SpecialistAgent, SpecialistToolkit


logger = logging.getLogger(__name__)


ToolkitFactory = Callable[[dict[str, Any]], SpecialistToolkit]


class AgentRegistry:
    """In-memory registry of :class:`SpecialistAgent` + toolkit instances.

    Typical lifecycle:

        registry = AgentRegistry.auto_discover()
        registry.attach_toolkits(clients={"finalysis": fn_client, ...})

    After that, ``registry.agent(agent_id)`` and ``registry.toolkit(agent_id)``
    return the cached instances.
    """

    def __init__(self) -> None:
        self._agents: dict[str, SpecialistAgent] = {}
        self._toolkits: dict[str, SpecialistToolkit] = {}
        self._pending_factories: dict[str, ToolkitFactory] = {}

    # ─── registration ────────────────────────────────────────

    def register(
        self,
        agent: SpecialistAgent,
        toolkit: SpecialistToolkit,
    ) -> None:
        """Register an already-constructed agent + toolkit.

        Useful from tests; production wires things through
        :meth:`auto_discover` + :meth:`attach_toolkits`.
        """
        if agent.id in self._agents:
            raise ValueError(f"specialist {agent.id!r} already registered")
        self._agents[agent.id] = agent
        self._toolkits[agent.id] = toolkit

    def register_deferred(
        self, agent: SpecialistAgent, factory: ToolkitFactory,
    ) -> None:
        """Register an agent now and its toolkit later.

        Mirrors what :meth:`auto_discover` does internally. The toolkit
        is built on the first call to :meth:`attach_toolkits`.
        """
        if agent.id in self._agents:
            raise ValueError(f"specialist {agent.id!r} already registered")
        self._agents[agent.id] = agent
        self._pending_factories[agent.id] = factory

    def attach_toolkits(self, *, clients: dict[str, Any]) -> None:
        """Instantiate every pending toolkit factory with ``clients``."""
        while self._pending_factories:
            agent_id, factory = self._pending_factories.popitem()
            try:
                toolkit = factory(clients)
            except Exception as exc:   # noqa: BLE001
                logger.exception(
                    "specialist registry: toolkit factory for %r failed", agent_id,
                )
                # Drop the agent rather than leaving a half-registered one.
                self._agents.pop(agent_id, None)
                continue
            if not isinstance(toolkit, SpecialistToolkit):
                logger.error(
                    "factory for %r returned %s, expected SpecialistToolkit",
                    agent_id, type(toolkit).__name__,
                )
                self._agents.pop(agent_id, None)
                continue
            self._toolkits[agent_id] = toolkit
            logger.info("specialist registry: attached toolkit for %r", agent_id)

    # ─── lookup ──────────────────────────────────────────────

    def agent(self, agent_id: str) -> SpecialistAgent:
        if agent_id not in self._agents:
            raise KeyError(f"unknown specialist: {agent_id}")
        return self._agents[agent_id]

    def toolkit(self, agent_id: str) -> SpecialistToolkit:
        if agent_id not in self._toolkits:
            raise KeyError(
                f"no toolkit attached for specialist {agent_id!r} "
                f"(did you call attach_toolkits?)"
            )
        return self._toolkits[agent_id]

    def ids(self) -> list[str]:
        """Every registered agent's id, sorted for deterministic output."""
        return sorted(self._agents.keys())

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self):
        return iter(self._agents.values())

    # ─── prompt catalog rendering ────────────────────────────

    def describe_for_prompt(self, locale: str) -> str:
        """Render the specialist catalog block for Session A's prompt.

        One bullet per registered agent, with native-language trigger
        examples pulled from ``agent.trigger_examples``. Empty string
        when the registry has no agents.
        """
        if not self._agents:
            return ""
        lines: list[str] = []
        # Deterministic order for stable prompt rendering.
        for agent_id in sorted(self._agents.keys()):
            agent = self._agents[agent_id]
            triggers = agent.trigger_phrases_for_locale(locale)[:4]
            trigger_str = " / ".join(f'"{t}"' for t in triggers)
            lines.append(
                f'• agent_id="{agent.id}" — {agent.display_name}, '
                f'{agent.description}\n'
                f'    Trigger for: {trigger_str}'
            )
        return "\n\n".join(lines)

    # ─── auto-discovery ──────────────────────────────────────

    @classmethod
    def auto_discover(
        cls,
        agents_pkg: str = "src.specialists.agents",
    ) -> "AgentRegistry":
        """Walk ``agents_pkg`` and register every module exposing
        ``AGENT`` and ``TOOLKIT_FACTORY``.

        Returns a registry with all agents registered but toolkits NOT
        yet attached — call :meth:`attach_toolkits` next.
        """
        registry = cls()
        try:
            pkg = importlib.import_module(agents_pkg)
        except ImportError as exc:
            logger.warning(
                "specialist registry: agents package %r not importable: %s",
                agents_pkg, exc,
            )
            return registry

        pkg_path = getattr(pkg, "__path__", None)
        if pkg_path is None:
            logger.warning(
                "specialist registry: %r is not a package", agents_pkg,
            )
            return registry

        for _, name, _ in pkgutil.iter_modules(pkg_path):
            module_name = f"{agents_pkg}.{name}"
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:   # noqa: BLE001
                logger.exception(
                    "specialist registry: failed to import %r: %s",
                    module_name, exc,
                )
                continue

            agent = getattr(module, "AGENT", None)
            factory = getattr(module, "TOOLKIT_FACTORY", None)

            if agent is None or factory is None:
                logger.debug(
                    "specialist registry: skipping %r (no AGENT/TOOLKIT_FACTORY)",
                    module_name,
                )
                continue

            if not isinstance(agent, SpecialistAgent):
                logger.error(
                    "specialist registry: %r exports AGENT of wrong type %s",
                    module_name, type(agent).__name__,
                )
                continue

            if not callable(factory):
                logger.error(
                    "specialist registry: %r exports non-callable TOOLKIT_FACTORY",
                    module_name,
                )
                continue

            try:
                registry.register_deferred(agent, factory)
            except ValueError as exc:
                logger.error(
                    "specialist registry: could not register %r: %s",
                    agent.id, exc,
                )
                continue

            logger.info(
                "specialist registry: discovered %r (voice=%s, locale=%s)",
                agent.id, agent.voice_id, agent.locale,
            )

        return registry
