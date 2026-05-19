"""HandoffRateLimiter — guardrails for Session B handoffs.

Per ``requirements.md R10.7`` + ``design.md § 13``, we enforce three
independent caps on ``handoff_to_specialist`` tool calls so a flaky
Session A (duplicate tool calls on misheard audio, future demo modes,
multi-tenant abuse) can't hammer Bedrock or the visor:

- **Concurrency** — at most ``max_concurrent`` Session B instances
  open at once (default ``1``; the session manager already allows only
  one active Session B per browser session, this is an additional
  belt-and-braces).
- **Rate window** — at most ``max_per_window`` handoffs per
  ``window_seconds`` sliding window (default ``1`` per ``60 s``).
- **Per-session cap** — at most ``max_per_session`` handoffs total
  before a browser session must reconnect (default ``20``).

``check(agent_id=...)`` returns ``(ok, reason)``. Call ``record(...)``
immediately after a successful ``check`` to count the handoff, and
``release(...)`` when the session manager fires handback so the
concurrency counter stays accurate.

All counters are tracked **per agent_id** plus global. A rate-limited
specialist can't starve other specialists (a future ``legal`` agent
keeps working even if someone spams ``financial`` handoffs).

Config via environment variables (read at construction time):

    NOVA_HANDOFF_MAX_CONCURRENT       default 1
    NOVA_HANDOFF_WINDOW_S             default 60
    NOVA_HANDOFF_MAX_PER_WINDOW       default 1
    NOVA_HANDOFF_MAX_PER_SESSION      default 20
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque


logger = logging.getLogger(__name__)


# Result codes that the handoff tool surfaces verbatim to Session A.
CODE_OK = "OK"
CODE_CONCURRENCY = "HANDOFF_IN_PROGRESS"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_SESSION_LIMIT = "SESSION_LIMIT_REACHED"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class HandoffRateLimiter:
    """Per-agent + global rate limiter for handoff tool calls."""

    max_concurrent: int = field(
        default_factory=lambda: _env_int("NOVA_HANDOFF_MAX_CONCURRENT", 1),
    )
    window_seconds: int = field(
        default_factory=lambda: _env_int("NOVA_HANDOFF_WINDOW_S", 60),
    )
    max_per_window: int = field(
        default_factory=lambda: _env_int("NOVA_HANDOFF_MAX_PER_WINDOW", 1),
    )
    max_per_session: int = field(
        default_factory=lambda: _env_int("NOVA_HANDOFF_MAX_PER_SESSION", 20),
    )

    # Sliding window of recent handoff timestamps (monotonic seconds).
    # Global counter lives in _recent[None]; per-agent counters live in
    # _recent[agent_id].
    _recent: dict[str | None, Deque[float]] = field(
        default_factory=lambda: defaultdict(deque),
    )
    _active: dict[str | None, int] = field(
        default_factory=lambda: defaultdict(int),
    )
    _session_total: int = 0

    def __post_init__(self) -> None:
        if self.max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be >0, got {self.max_concurrent}")
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be >0, got {self.window_seconds}")
        if self.max_per_window <= 0:
            raise ValueError(f"max_per_window must be >0, got {self.max_per_window}")
        if self.max_per_session <= 0:
            raise ValueError(f"max_per_session must be >0, got {self.max_per_session}")

    # ─── public API ──────────────────────────────────────────

    def check(self, *, agent_id: str | None = None) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a new handoff.

        Args:
            agent_id: When provided, ``max_per_window`` is enforced
                per-agent rather than globally. A global cap still
                applies via ``agent_id=None`` on concurrency.

        Returns:
            ``(True, "OK")`` if the handoff is allowed; otherwise
            ``(False, <CODE>)`` where ``CODE`` is one of
            ``HANDOFF_IN_PROGRESS`` / ``RATE_LIMITED`` / ``SESSION_LIMIT_REACHED``.
        """
        now = time.monotonic()
        self._gc(now)

        # Concurrency — check GLOBAL first (the session manager also
        # enforces one active Session B per browser session).
        if self._active[None] >= self.max_concurrent:
            return False, CODE_CONCURRENCY

        # Session-total cap (global across agents).
        if self._session_total >= self.max_per_session:
            return False, CODE_SESSION_LIMIT

        # Rate window — check per-agent when agent_id is given.
        window_bucket = agent_id if agent_id is not None else None
        if len(self._recent[window_bucket]) >= self.max_per_window:
            return False, CODE_RATE_LIMITED

        return True, CODE_OK

    def record(self, *, agent_id: str | None = None) -> None:
        """Count a just-started handoff. Must only be called after a
        successful :meth:`check`. Bumps both global and per-agent counters.

        .. deprecated::
            Prefer :meth:`check_and_record` which closes the
            check→record race window when the caller yields (via
            ``await``) between the two calls. See
            (internal postmortem 2026-05-09).
        """
        now = time.monotonic()
        self._gc(now)
        self._recent[None].append(now)
        if agent_id is not None:
            self._recent[agent_id].append(now)
        self._active[None] += 1
        if agent_id is not None:
            self._active[agent_id] += 1
        self._session_total += 1

    def check_and_record(self, *, agent_id: str | None = None) -> tuple[bool, str]:
        """Atomic :meth:`check` + :meth:`record`.

        Callers **must** use this method rather than separate ``check``
        and ``record`` calls when any ``await`` can occur between them.
        Without atomicity two concurrent handoffs can both pass the
        concurrency check at ``active=0`` before either records, and
        end up double-counting — a failure mode that leaves ``active``
        stuck above zero after a single ``release`` and blocks every
        future handoff with ``HANDOFF_IN_PROGRESS``.

        The method runs entirely inside one synchronous function body
        (no ``await``), so under asyncio + CPython the GIL guarantees
        the check and the counter bump happen together from the
        perspective of any other coroutine.

        Returns:
            ``(True, "OK")`` and the handoff is counted. The caller
            **must** :meth:`release` on any downstream failure to roll
            the reservation back.

            ``(False, <CODE>)`` with the rejection reason; no counter
            change occurred.
        """
        allowed, reason = self.check(agent_id=agent_id)
        if allowed:
            self.record(agent_id=agent_id)
        return allowed, reason

    def release(self, *, agent_id: str | None = None) -> None:
        """Mark an in-flight handoff as finished (success, barge-in, error).

        Decrements the concurrency counter. Idempotent-ish — can't go
        below zero even if called twice; the extra call is logged at
        DEBUG.
        """
        if self._active[None] <= 0:
            logger.debug("handoff_rate.release: already at zero (global)")
            return
        self._active[None] -= 1
        if agent_id is not None and self._active[agent_id] > 0:
            self._active[agent_id] -= 1

    def snapshot(self) -> dict:
        """Read-only view for ``/diagnose`` endpoint."""
        now = time.monotonic()
        self._gc(now)
        return {
            "max_concurrent": self.max_concurrent,
            "window_seconds": self.window_seconds,
            "max_per_window": self.max_per_window,
            "max_per_session": self.max_per_session,
            "active": {k: v for k, v in self._active.items() if v > 0},
            "recent_count": {k: len(v) for k, v in self._recent.items() if v},
            "session_total": self._session_total,
        }

    def reset(self) -> None:
        """Clear all state. Intended for tests / ``stop.sh`` teardown."""
        self._recent.clear()
        self._active.clear()
        self._session_total = 0

    # ─── internal ────────────────────────────────────────────

    def _gc(self, now: float) -> None:
        """Drop expired entries from every sliding window."""
        cutoff = now - self.window_seconds
        for deq in self._recent.values():
            while deq and deq[0] < cutoff:
                deq.popleft()
