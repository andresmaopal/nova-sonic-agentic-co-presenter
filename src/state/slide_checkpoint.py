"""Slide checkpoint — atomic persistent store for the current PowerPoint slide.

Purpose
-------
Track the slide the presenter is currently on so that when Nova's
``switch_window`` (or ``handoff_to_specialist``) flips focus to the
Chrome visor and then back, PowerPoint can be navigated to the slide
the audience was just looking at — **not** slide 1 (PowerPoint's
default when a slideshow is re-started) and **not** whatever slide
PowerPoint happens to be parked on when reactivated.

Two producers update this checkpoint:

1. **Keyboard hook** — polls PowerPoint every 500 ms in slideshow mode
   and calls :func:`save` on every slide change. Gives continuous
   tracking during a live presentation.
2. **WindowManager.switch_to_visor** — calls :func:`save` explicitly
   from whatever mode PowerPoint happens to be in (normal *or*
   slideshow) right before the handoff. Covers the case where the
   presenter is in normal view and the keyboard hook isn't updating
   anything.

One consumer reads:

- **WindowManager.switch_to_slides** — calls :func:`load` and, if a
  value exists, passes it to ``start_slideshow(from_slide=…)`` or
  ``goto(N)`` depending on the desired mode.

Storage format
--------------
``.slide_checkpoint.json`` at the project root (same directory that
holds ``.slide_cache/``), single JSON object:

    {"slide_number": 3, "updated_at": "2026-05-09T21:05:12.340000+00:00"}

``slide_number`` is 1-based (matches PowerPoint's AppleScript convention
and what the voice layer talks about); ``updated_at`` is ISO-8601 UTC
for debugging — a stale checkpoint is easy to spot in ``/diagnose``.

Atomicity
---------
Writes use tempfile + ``os.replace`` so a crash mid-write can't leave
a half-written file that ``load()`` would then fail to parse. Readers
silently tolerate missing / corrupt files and return ``None`` so
"no checkpoint yet" and "checkpoint unreadable" both degrade to
"start from wherever PowerPoint is" — which is the same behaviour as
before this module existed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default path is relative to the working directory (the project root
# when started via start.sh). Overridable via env var for tests and
# anyone who wants the file in a different spot (e.g. $TMPDIR on CI).
DEFAULT_CHECKPOINT_PATH = Path(
    os.environ.get("NOVA_SLIDE_CHECKPOINT_PATH", ".slide_checkpoint.json")
)


def save(
    slide_number: int,
    *,
    path: Path | None = None,
) -> bool:
    """Atomically persist ``slide_number`` to the checkpoint file.

    Args:
        slide_number: 1-based slide number. Non-int or < 1 are rejected
            (the caller is probably buggy; better to log and no-op than
            persist garbage that a future restart will then act on).
        path: Override the default location (used by tests).

    Returns:
        ``True`` on success, ``False`` if validation or I/O failed.
        Callers should treat failure as "checkpoint lost" — the next
        successful :func:`save` will overwrite it.

    The write is ``tempfile → fsync → os.replace`` so a reader can never
    observe a truncated file; either it sees the previous value or the
    new one, never a mix.
    """
    if not isinstance(slide_number, int) or isinstance(slide_number, bool):
        logger.debug(
            "slide_checkpoint.save: rejecting non-int slide_number=%r",
            slide_number,
        )
        return False
    if slide_number < 1:
        logger.debug(
            "slide_checkpoint.save: rejecting slide_number=%d (must be >= 1)",
            slide_number,
        )
        return False

    target = path or DEFAULT_CHECKPOINT_PATH
    payload = {
        "slide_number": slide_number,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # Ensure parent directory exists (no-op if it does). This makes
        # callers that pass a nested path "just work" without a separate
        # mkdir step — common in tests.
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write to a sibling tempfile so an interrupted write can never
        # corrupt ``target``. Keeping the tempfile in the same directory
        # guarantees ``os.replace`` is an atomic same-filesystem rename
        # on POSIX.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)

        os.replace(tmp_path, target)
        return True
    except OSError as exc:
        logger.info(
            "slide_checkpoint.save: I/O failed for %s (%s) — degrading to in-memory only",
            target, exc,
        )
        return False


def load(path: Path | None = None) -> Optional[int]:
    """Return the persisted 1-based slide number, or ``None`` if there
    is no checkpoint or the file is unreadable/invalid.

    Any JSON parsing error, missing file, or malformed payload returns
    ``None`` rather than raising. That lets callers treat
    "checkpoint unavailable" identically regardless of cause.
    """
    target = path or DEFAULT_CHECKPOINT_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("slide_checkpoint.load: %s read failed (%s)", target, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.info(
            "slide_checkpoint.load: %s is corrupt (%s) — ignoring",
            target, exc,
        )
        return None

    slide_number = payload.get("slide_number") if isinstance(payload, dict) else None
    if not isinstance(slide_number, int) or isinstance(slide_number, bool):
        return None
    if slide_number < 1:
        return None
    return slide_number


def clear(path: Path | None = None) -> bool:
    """Delete the checkpoint file. Returns True if removed or already
    absent; False only on unexpected I/O error.

    Mostly useful for tests and for a future "/reset" admin endpoint.
    """
    target = path or DEFAULT_CHECKPOINT_PATH
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.debug("slide_checkpoint.clear: %s unlink failed (%s)", target, exc)
        return False
