"""slide_sync — keep SlideStore.current_index in sync with PowerPoint's actual slide.

The keyboard hook polls every 500 ms, so there's a ~500 ms window during which
SlideStore can lag behind PowerPoint's real current slide (e.g. the presenter
clicked to a new slide with their mouse just before invoking ``analyze_slide``).

This module provides a low-latency on-demand sync that reads PowerPoint's
current slide in a single AppleScript call (~50-100 ms) and corrects
SlideStore if it has drifted.  It's designed to be called:

* BEFORE ``analyze_slide`` — so the vision call always describes the slide the
  presenter is actually showing, not a stale one.
* AFTER ``navigate_slide`` — so post-animation or multi-step navigation ends
  up pointing at the correct slide in case PowerPoint didn't move exactly
  where we asked.

The overhead is small compared to the vision model call (2-5 s), and it never
raises — failures are silent and logged at DEBUG level.
"""

from __future__ import annotations

import logging

from src.platform import powerpoint as ppt
from src.slide_store import SlideStore

logger = logging.getLogger(__name__)


def resync_slide_store(slide_store: SlideStore) -> bool:
    """Read PowerPoint's current slide and update SlideStore if out of sync.

    Args:
        slide_store: The shared SlideStore whose current_index may be stale.

    Returns:
        True  — PowerPoint disagreed with SlideStore, store was updated.
        False — Store was already correct, or PowerPoint is unavailable /
                unreadable / reports a slide outside [0, total_slides).

    Never raises. PowerPoint-unavailable / AppleScript failures are logged
    at DEBUG level and treated as "no-op".
    """
    if slide_store.total_slides == 0:
        return False

    try:
        if not ppt.is_running():
            return False

        result = ppt.get_current_slide_number()
        if not result.ok:
            logger.debug("resync: get_current_slide_number failed: %s", result.code)
            return False

        try:
            ppt_index = int(result.data.get("slide_number", 0)) - 1  # 1-based → 0-based
        except (ValueError, TypeError):
            logger.debug("resync: non-integer slide_number in result: %r", result.data)
            return False

        if ppt_index < 0 or ppt_index >= slide_store.total_slides:
            # PowerPoint reports a slide outside our preprocessed range
            # (e.g. deck was modified) — don't blindly update.
            logger.debug(
                "resync: PPT slide %d out of range [1, %d]",
                ppt_index + 1,
                slide_store.total_slides,
            )
            return False

        if ppt_index != slide_store.current_index:
            old = slide_store.current_index
            slide_store.set_current_index(ppt_index)
            logger.info(
                "Slide sync: store was on %d, PowerPoint on %d — corrected",
                old + 1,
                ppt_index + 1,
            )
            return True

        return False

    except Exception as exc:  # noqa: BLE001 — never raise from a best-effort sync
        logger.debug("resync_slide_store unexpected error: %s", exc)
        return False
