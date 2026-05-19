"""DataHandleStore — opaque, TTL'd handles for Session B tool chaining.

Session B's tools pass *handles* (short opaque strings like ``fn-a1b2c3d4``)
between each other so the raw Finalysis JSON never enters Nova Sonic's
conversation context. The full data lives here, keyed by handle, with an
expiration clock.

Typical lifetime is ≤ 30 s (one handoff). The 120 s default TTL gives
generous slack for slow Bedrock/Sonnet latency. Handles from a previous
handoff are GC'd on the next access — no background thread required.

The store is shared across all concurrent browser sessions; handle
collision probability is negligible (32-bit entropy, 120 s TTL).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


DEFAULT_TTL_S = int(os.environ.get("NOVA_DATA_HANDLE_TTL_S", "120"))


@dataclass
class _Entry:
    expires_at: float
    value: Any


class DataHandleStore:
    """Async-safe in-memory store with TTL and opportunistic GC.

    The public surface is small on purpose — put / get / stats. Anything
    fancier (e.g., explicit remove, pub-sub) adds failure modes we don't
    need for Session B's one-shot use.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_S) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._store: dict[str, _Entry] = {}
        self._ttl: int = ttl_seconds
        self._lock = asyncio.Lock()

    async def put(self, prefix: str, value: Any) -> str:
        """Store *value* under a new handle of the form ``<prefix>-<8-hex>``.

        Args:
            prefix: Short tag identifying the handle's origin
                (e.g., ``"fn"`` for a Finalysis fetch, ``"td"`` for a
                transform). Must be 2–6 chars of ``[a-z0-9-]``.
            value: Any Python object. Stored by reference — don't mutate
                it after putting.

        Returns:
            The handle string. Pass back to ``get`` within ``ttl_seconds``.
        """
        _validate_prefix(prefix)
        handle = f"{prefix}-{uuid.uuid4().hex[:8]}"
        async with self._lock:
            self._gc_locked()
            self._store[handle] = _Entry(
                expires_at=time.time() + self._ttl,
                value=value,
            )
        return handle

    async def get(self, handle: str) -> Any | None:
        """Return the stored value, or ``None`` if the handle is unknown
        or has expired. Never raises."""
        if not handle:
            return None
        async with self._lock:
            self._gc_locked()
            entry = self._store.get(handle)
            return entry.value if entry else None

    async def stats(self) -> dict[str, int | float | None]:
        """Snapshot for the ``/diagnose`` endpoint.

        Returns a small dict with the current count and the age (in
        seconds) of the oldest live entry, or ``None`` if the store is
        empty.
        """
        async with self._lock:
            self._gc_locked()
            if not self._store:
                return {"count": 0, "oldest_age_s": None, "ttl_s": self._ttl}
            now = time.time()
            oldest_expiry = min(e.expires_at for e in self._store.values())
            oldest_age_s = round(self._ttl - (oldest_expiry - now), 2)
            return {
                "count": len(self._store),
                "oldest_age_s": oldest_age_s,
                "ttl_s": self._ttl,
            }

    # ── internal ──────────────────────────────────────────────

    def _gc_locked(self) -> None:
        """Drop expired entries. Caller must hold ``self._lock``."""
        now = time.time()
        expired = [h for h, e in self._store.items() if e.expires_at < now]
        for h in expired:
            del self._store[h]


# ─────────────────────────────────────────────────────────────

_ALLOWED_PREFIX_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _validate_prefix(prefix: str) -> None:
    if not (2 <= len(prefix) <= 6):
        raise ValueError(
            f"prefix must be 2-6 chars, got {prefix!r} ({len(prefix)} chars)"
        )
    if not all(c in _ALLOWED_PREFIX_CHARS for c in prefix):
        raise ValueError(f"prefix must be lowercase alnum+dash, got {prefix!r}")
