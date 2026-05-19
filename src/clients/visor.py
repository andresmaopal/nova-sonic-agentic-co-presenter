"""VisorClient — thin async wrapper around the visor's progress endpoints.

The visor (``visor/server.mjs``) already exposes three endpoints for the
progress overlay:

    POST /api/start  {phases: [...]}
    POST /api/phase  {index, substep?, label?, status?}
    POST /api/done   {}

All calls are **best-effort**: a visor outage must never crash the
financial pipeline or, worse, stall a Session B tool handler. We
swallow every network/HTTP error and log a warning at most — the rest
of the pipeline carries on.

This replaces ``scripts/visor-notify.sh`` in every code path that runs
inside Python. The shell helper is kept only for manual / dev use.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = f"http://127.0.0.1:{os.environ.get('VISOR_PORT', '3333')}"
DEFAULT_TIMEOUT_S = 2.0


class VisorClient:
    """Best-effort POST client for the visor overlay."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # One reused client for connection pooling.
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ─── endpoints ─────────────────────────────────────────

    async def start(self, phases: list[str | dict] | None = None) -> None:
        """Arm the overlay with a phase list (or the visor's defaults).

        Args:
            phases: Either a list of label strings, or a list of
                ``{"label": str, "substeps": [str, ...]}`` dicts. Pass
                ``None`` to use the visor's default labels.
        """
        body: dict[str, Any] = {"phases": phases} if phases else {}
        await self._post_best_effort("/api/start", body)

    async def phase(
        self,
        index: int,
        *,
        label: str | None = None,
        substep: str | None = None,
        status: str = "active",
    ) -> None:
        """Advance the overlay to phase ``index`` (0-based).

        The ``substep`` is free-form short text rendered under the
        active phase (e.g., ticker + window, chart URL host, filename).
        """
        body: dict[str, Any] = {"index": int(index), "status": status}
        if label is not None:
            body["label"] = label
        if substep is not None:
            body["substep"] = substep
        await self._post_best_effort("/api/phase", body)

    async def done(self) -> None:
        """Snap all phases to complete. Optional — the file-watcher on
        ``reports/`` will also auto-dismiss the overlay once a report
        HTML lands."""
        await self._post_best_effort("/api/done", {})

    async def aborted(self, reason: str | None = None) -> None:
        """Signal that a generation attempt was interrupted BEFORE a
        report was produced (barge-in, timeout, error). The visor uses
        this to show an 'interrupted' empty-state variant instead of
        the cold-boot welcome copy — so a presenter who just asked for
        a chart doesn't see 'Esperando el primer reporte…' and think
        nothing happened.

        Args:
            reason: Optional short string (e.g. 'barge_in', 'timeout',
                'finalysis_error') shown in the visor console log only.
        """
        body: dict[str, Any] = {"reason": reason} if reason else {}
        await self._post_best_effort("/api/aborted", body)

    # ─── health ────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Used by ``/diagnose``. Returns a small dict the caller can
        render in an admin view."""
        try:
            client = await self._client()
            resp = await client.get("/api/latest")
            return {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "base_url": self._base_url,
            }
        except Exception as exc:   # noqa: BLE001
            return {"ok": False, "error": str(exc), "base_url": self._base_url}

    # ─── internal ──────────────────────────────────────────

    async def _post_best_effort(self, path: str, body: dict[str, Any]) -> None:
        """POST and swallow all errors. Never raises."""
        try:
            client = await self._client()
            resp = await client.post(path, json=body)
            if resp.status_code >= 400:
                logger.warning(
                    "visor %s returned %d: %s",
                    path, resp.status_code, resp.text[:200],
                )
        except Exception as exc:   # noqa: BLE001
            # Typical: ConnectError when the visor is down. We don't want
            # pipeline tools to fail just because the overlay isn't up.
            logger.debug("visor %s best-effort failed: %s", path, exc)
