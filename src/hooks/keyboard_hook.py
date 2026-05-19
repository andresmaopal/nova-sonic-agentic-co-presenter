"""Keyboard hook — macOS-only slide transition detector for PowerPoint.

Polls Microsoft PowerPoint's current slide number via AppleScript at 500ms
intervals and sends HTTP POST to the agent's /slide_update endpoint when
the slide changes.  Windows support is out of scope for MVP.

Communication is localhost-only (Requirement 9.5).
"""

import argparse
import json
import logging
import sys
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.platform import powerpoint as ppt
from src.state import slide_checkpoint

logger = logging.getLogger(__name__)

DEFAULT_AGENT_URL = "http://127.0.0.1:8000"
POLL_INTERVAL_SECONDS = 0.5  # 500 ms  (Req 17.2)
MAX_BACKOFF_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_current_slide_index() -> int | None:
    """Return the 0-based slide index from PowerPoint, or ``None``.

    ``None`` covers three cases (all handled silently by callers):
      * PowerPoint not installed / not running
      * No presentation open
      * No slideshow running (we only care about slideshow-mode transitions)
    """
    if not ppt.is_running() or not ppt.is_slideshow_active():
        return None
    res = ppt.get_current_slide_number()
    if not res.ok:
        logger.debug("get_current_slide_number: %s — %s", res.code, res.message)
        return None
    try:
        return int(res.data["slide_number"]) - 1  # 1-based → 0-based
    except (KeyError, ValueError, TypeError):
        return None


def _post_slide_update(agent_url: str, slide_index: int) -> bool:
    """Send HTTP POST to the agent's ``/slide_update`` endpoint.

    Args:
        agent_url: Base URL of the agent (must be localhost per Req 9.5).
        slide_index: 0-based slide index.

    Returns:
        ``True`` if the POST succeeded, ``False`` otherwise.
    """
    url = f"{agent_url}/slide_update"
    data = json.dumps({"slide_index": slide_index}).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (URLError, OSError, TimeoutError) as exc:
        logger.error("Failed to reach agent at %s: %s", url, exc)
        return False


def _validate_localhost(url: str) -> None:
    """Raise if *url* does not point to localhost (Req 9.5)."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"KeyboardHook must communicate with the agent over localhost only. "
            f"Got: {hostname}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_hook_inprocess(slide_store, poll_interval_seconds: float = POLL_INTERVAL_SECONDS) -> None:
    """In-process polling loop — updates SlideStore directly (no HTTP)."""
    current_index: int | None = None
    logger.info("In-process keyboard hook started (poll interval: %.1fs)", poll_interval_seconds)
    while True:
        new_index = _get_current_slide_index()
        if new_index is not None and new_index != current_index:
            if 0 <= new_index < slide_store.total_slides:
                current_index = new_index
                slide_store.set_current_index(current_index)
                # Persist the 1-based slide number so WindowManager can
                # navigate PowerPoint back here after a visor round-trip
                # — and so the checkpoint survives across server
                # restarts. Failure is non-fatal: the hook keeps
                # polling and the next successful save overwrites it.
                slide_checkpoint.save(current_index + 1)
                logger.info("Slide updated → slide %d/%d", current_index + 1, slide_store.total_slides)
        time.sleep(poll_interval_seconds)


def run_hook(
    agent_url: str = DEFAULT_AGENT_URL,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Main polling loop — runs indefinitely.

    * Polls AppleScript at *poll_interval* seconds  (Req 6.3, 17.2)
    * Debounces: only POSTs when the index changes  (Req 6.2)
    * Handles PowerPoint not running  (Req 6.6 → log warning, retry)
    * Handles agent unreachable  (Req 6.5 → log error, backoff)
    * Localhost-only  (Req 9.5)
    """
    _validate_localhost(agent_url)

    current_index: int | None = None
    backoff = 0.0

    logger.info(
        "Keyboard hook started — polling PowerPoint, agent at %s (interval: %.1fs)",
        agent_url,
        poll_interval,
    )

    while True:
        new_index = _get_current_slide_index()

        if new_index is None:
            # PowerPoint not running or no slideshow active (Req 6.6)
            logger.debug("PowerPoint slideshow not active; waiting…")
            time.sleep(poll_interval)
            continue

        if new_index != current_index:
            current_index = new_index
            logger.info("Slide change detected → slide %d", current_index + 1)
            success = _post_slide_update(agent_url, current_index)

            if success:
                backoff = 0.0
                logger.info("Slide updated → index %d", current_index)
            else:
                # Backoff on agent-unreachable (Req 6.5)
                backoff = min(backoff + 0.5, MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Agent unreachable — backing off %.1fs", backoff
                )
                time.sleep(backoff)

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# CLI entry point  (Task 8.7)
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for standalone hook execution."""
    if sys.platform != "darwin":
        print(
            "Error: The keyboard hook is macOS-only "
            "(Windows support is out of scope for MVP)."
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="PowerPoint slide-transition detector (macOS)",
    )
    parser.add_argument(
        "--agent-url",
        default=DEFAULT_AGENT_URL,
        help=f"Agent base URL (default: {DEFAULT_AGENT_URL})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        run_hook(agent_url=args.agent_url, poll_interval=args.poll_interval)
    except KeyboardInterrupt:
        logger.info("Keyboard hook stopped.")


if __name__ == "__main__":
    main()
