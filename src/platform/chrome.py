"""ChromeAdapter — focus Chrome tabs by URL prefix via CDP (or AppleScript).

Chrome is launched at project startup with ``--remote-debugging-port=9222``
and an isolated ``--user-data-dir`` (see ``scripts/ensure-chrome.sh``).
This adapter then uses Playwright to connect over the CDP endpoint so
we can:

- Find any tab by URL prefix in <100 ms.
- Bring a specific tab to the front within its window AND raise the
  Chrome window itself above PowerPoint.
- Open a new tab at a given URL when the expected one is missing.

If Playwright can't connect (not installed, wrong port, Chrome launched
without the flag), every operation **falls back to AppleScript** which
iterates ``tabs of window`` looking for a URL-prefix match. AppleScript
is slower (~300-600 ms) and brittle with multiple Chrome windows, but
ensures the demo degrades gracefully rather than failing hard.

Never raises. Every method returns a :class:`ChromeResult` with a
machine-readable ``code``. See :mod:`src.platform.powerpoint` for the
same ``_run()``-subprocess pattern used for ``osascript`` calls.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = int(os.environ.get("CHROME_CDP_PORT", "9222"))
DEFAULT_CDP_ENDPOINT = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}"

_OSASCRIPT_TIMEOUT = 5.0


# ─────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChromeResult:
    """Uniform return type for every Chrome operation.

    Parallels :class:`src.platform.powerpoint.PptResult` so callers can
    treat the two platforms through a single interface.
    """

    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view, safe for tool_result payloads."""
        return {"ok": self.ok, "code": self.code, "message": self.message,
                **self.data}


# ─────────────────────────────────────────────────────────────
# Playwright factory (swappable for tests)
# ─────────────────────────────────────────────────────────────


class _PlaywrightFactory(Protocol):
    """Callable returning an ``async_playwright()`` async-context instance."""

    def __call__(self) -> Any: ...


def _default_playwright_factory() -> Any:
    """Import + invoke ``playwright.async_api.async_playwright``.

    Kept behind a factory so tests can inject a fake without Playwright
    being installed.
    """
    from playwright.async_api import async_playwright   # type: ignore
    return async_playwright()


def _format_cdp_error(exc: BaseException) -> str:
    """Render a CDP connect/setup exception as a compact one-liner.

    Playwright exception messages include a multi-line "Call log:" tail
    with the websocket connect/disconnect trace for every failed
    attempt. That tail is useful when chasing a hard-to-reproduce CDP
    bug, but it's pure noise during normal startup when we expect to
    fall back to AppleScript (because our demo Chrome is launched
    without the features Playwright probes for, like
    ``Browser.setDownloadBehavior``).

    This helper:

    1. Pulls just the first line of the exception text (drops the
       whole "Call log:" block and any stack-like frames).
    2. Collapses whitespace so the remaining message fits on one
       ``logger.info`` line.
    3. Caps the length to 200 chars to guarantee readable logs even
       for exotic Playwright error shapes we haven't seen yet.

    The full exception is still available at DEBUG level for anyone
    who needs it.
    """
    logger.debug("chrome: full CDP error", exc_info=exc)
    raw = str(exc)
    first = raw.split("\n", 1)[0].strip()
    # Drop any ``Call log:``-ish trailer that survived the split
    # (happens when Playwright puts "Call log:" on the same line).
    for marker in ("Call log:", "Note: ", "==="):
        idx = first.find(marker)
        if idx > 0:
            first = first[:idx].rstrip(" :")
    # Normalise internal whitespace.
    first = " ".join(first.split())
    if len(first) > 200:
        first = first[:197] + "..."
    return first or exc.__class__.__name__


# ─────────────────────────────────────────────────────────────
# ChromeAdapter
# ─────────────────────────────────────────────────────────────


class ChromeAdapter:
    """Thin async adapter over Playwright+CDP with AppleScript fallback.

    Intended lifetime is one instance per FastAPI lifespan. Internally
    caches the Playwright ``Browser`` handle — call :meth:`close`
    during shutdown.
    """

    def __init__(
        self,
        *,
        cdp_endpoint: str | None = None,
        playwright_factory: _PlaywrightFactory | None = None,
    ) -> None:
        self._cdp_endpoint = cdp_endpoint or DEFAULT_CDP_ENDPOINT
        self._playwright_factory = playwright_factory or _default_playwright_factory

        # Cached Playwright handles, all set together when connect() succeeds.
        self._pw: Any = None      # the async_playwright() context instance
        self._browser: Any = None

    # ─── lifecycle ────────────────────────────────────────────

    async def connect(self) -> Any | None:
        """Connect to Chrome over CDP. Lazy + cached.

        Returns the Playwright ``Browser`` handle, or ``None`` if CDP
        isn't reachable / Playwright isn't installed / any other failure.
        Failure is not an error — callers fall back to AppleScript.
        """
        if self._browser is not None:
            return self._browser
        try:
            self._pw = await self._playwright_factory().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._cdp_endpoint
            )
            return self._browser
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "chrome: CDP connect failed (%s) — will use AppleScript fallback",
                _format_cdp_error(exc),
            )
            self._browser = None
            await self._stop_playwright()
            return None

    async def close(self) -> None:
        """Release cached Playwright resources. Safe to call multiple times."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("chrome: browser.close raised: %s", exc)
            self._browser = None
        await self._stop_playwright()

    async def _stop_playwright(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("chrome: pw.stop raised: %s", exc)
            self._pw = None

    # ─── high-level operations ────────────────────────────────

    async def find_tab_by_url_prefix(self, prefix: str) -> Any | None:
        """Return the first Playwright ``Page`` whose URL starts with
        ``prefix``, or ``None`` if CDP is unreachable or no tab matches.

        Iterates every context and every page so tabs in any Chrome
        window are considered.
        """
        browser = await self.connect()
        if browser is None:
            return None
        for ctx in browser.contexts:
            for page in ctx.pages:
                if (page.url or "").startswith(prefix):
                    return page
        return None

    async def bring_tab_to_front(self, prefix: str) -> ChromeResult:
        """Activate the tab matching ``prefix`` AND raise Chrome to the
        foreground.

        Tries Playwright first (fast, deterministic). On any failure
        falls back to AppleScript which iterates ``tabs of window`` by URL.
        """
        page = await self.find_tab_by_url_prefix(prefix)
        if page is not None:
            try:
                await page.bring_to_front()
                # Chrome's bring_to_front activates the page within its
                # window; the AppleScript below raises the window above
                # PowerPoint so the audience actually sees it.
                _run_applescript(_ACTIVATE_CHROME_APP)
                return ChromeResult(
                    ok=True, code="OK",
                    message=f"tab {page.url!r} raised via CDP",
                    data={"url": page.url, "via": "playwright"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "chrome: Playwright bring_to_front failed (%s) — using AppleScript",
                    exc,
                )
        return self._applescript_activate_tab(prefix)

    async def maximize_window_for_tab(self, prefix: str) -> ChromeResult:
        """Ask Chrome to put the window containing the tab matching
        ``prefix`` into the ``maximized`` window state.

        Why ``maximized`` and not ``fullscreen``:

        - ``maximized`` fills the available screen but keeps the window
          on the main macOS Space — no new Space, no return-trip race
          when switching focus back to PowerPoint.
        - ``fullscreen`` on macOS creates its own Space; the presenter
          would then experience a Spaces swipe back to PowerPoint,
          and PowerPoint's slideshow window can lose focus
          mid-transition (see
          ``(internal postmortem 2026-05-09)``).

        Uses the CDP Browser domain: ``getWindowForTarget`` to resolve
        the window id for the target page, then ``setWindowBounds``
        with ``{"windowState": "maximized"}``.

        Best-effort only. On CDP unreachable, missing tab, or Chrome
        rejecting the bounds change the adapter returns an ok=False
        ChromeResult without side effects — the caller logs and
        continues. Never raises.
        """
        page = await self.find_tab_by_url_prefix(prefix)
        if page is None:
            return ChromeResult(
                ok=False, code="NO_MATCHING_TAB",
                message=f"no tab found with URL prefix {prefix!r}",
                data={"via": "cdp"},
            )

        # Playwright exposes a CDP session tied to a specific page via
        # ``context.new_cdp_session(page)``. That gives us a channel
        # where ``Browser.getWindowForTarget`` resolves to the window
        # containing that page.
        session = None
        try:
            ctx = page.context
            session = await ctx.new_cdp_session(page)
            win_info = await session.send("Browser.getWindowForTarget")
            window_id = win_info.get("windowId")
            if not isinstance(window_id, int):
                return ChromeResult(
                    ok=False, code="NO_WINDOW_ID",
                    message="Browser.getWindowForTarget returned no windowId",
                    data={"via": "cdp"},
                )

            # Short-circuit if the window is already in the requested
            # state. Avoids an unnecessary setWindowBounds call (which
            # can trigger a window-server repaint on macOS).
            bounds = await session.send(
                "Browser.getWindowBounds",
                {"windowId": window_id},
            )
            current_state = (bounds.get("bounds") or {}).get("windowState")
            if current_state == "maximized":
                return ChromeResult(
                    ok=True, code="ALREADY_MAXIMIZED",
                    message=f"window {window_id} already maximized",
                    data={"via": "cdp", "window_id": window_id},
                )

            # setWindowBounds can reject a direct transition from
            # minimized/fullscreen to maximized; go via "normal" first
            # in those cases. See
            # https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setWindowBounds
            if current_state in ("minimized", "fullscreen"):
                try:
                    await session.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": window_id,
                            "bounds": {"windowState": "normal"},
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    # Non-fatal — the subsequent call may still work.
                    logger.debug(
                        "chrome: intermediate normal transition failed (%s)",
                        exc,
                    )

            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"windowState": "maximized"},
                },
            )
            return ChromeResult(
                ok=True, code="OK",
                message=f"window {window_id} maximized",
                data={"via": "cdp", "window_id": window_id,
                      "previous_state": current_state},
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "chrome: maximize_window_for_tab failed (%s) — continuing",
                exc,
            )
            return ChromeResult(
                ok=False, code="CDP_ERROR",
                message=str(exc),
                data={"via": "cdp"},
            )
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("chrome: cdp session.detach raised (%s)", exc)

    async def ensure_tab(self, prefix: str, url: str) -> ChromeResult:
        """Return the tab matching ``prefix`` if it exists; otherwise open
        a new tab at ``url``. Either way, the target tab ends up active.
        """
        page = await self.find_tab_by_url_prefix(prefix)
        if page is not None:
            try:
                await page.bring_to_front()
                _run_applescript(_ACTIVATE_CHROME_APP)
                return ChromeResult(
                    ok=True, code="OK",
                    message=f"existing tab at {page.url!r}",
                    data={"url": page.url, "via": "playwright", "created": False},
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("chrome: existing tab raise failed (%s) — fallback", exc)
                return self._applescript_activate_tab(prefix)

        # No matching tab — open a new one.
        browser = await self.connect()
        if browser is None:
            return self._applescript_open_tab(url)

        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            try:
                ctx = await browser.new_context()
            except Exception as exc:  # noqa: BLE001
                logger.info("chrome: new_context failed (%s) — fallback", exc)
                return self._applescript_open_tab(url)

        try:
            page = await ctx.new_page()
            await page.goto(url)
            await page.bring_to_front()
            _run_applescript(_ACTIVATE_CHROME_APP)
            return ChromeResult(
                ok=True, code="OK",
                message=f"opened new tab at {url!r}",
                data={"url": url, "via": "playwright", "created": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("chrome: new tab via CDP failed (%s) — fallback", exc)
            return self._applescript_open_tab(url)

    async def health_check(self) -> dict[str, Any]:
        """Used by ``/diagnose``. Reports reachability + list of tab URLs."""
        t0 = time.perf_counter()
        browser = await self.connect()
        if browser is None:
            return {
                "cdp_reachable": False,
                "chrome_running": _applescript_chrome_running(),
                "endpoint": self._cdp_endpoint,
                "tabs": [],
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }
        tabs: list[str] = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                if page.url:
                    tabs.append(page.url)
        return {
            "cdp_reachable": True,
            "chrome_running": True,
            "endpoint": self._cdp_endpoint,
            "tabs": tabs,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }

    # ─── AppleScript fallbacks ────────────────────────────────

    def _applescript_activate_tab(self, prefix: str) -> ChromeResult:
        """Iterate every tab of every Chrome window and activate the first
        whose URL starts with ``prefix``. Raises the window to front."""
        script = _APPLESCRIPT_ACTIVATE_TAB_TEMPLATE.format(
            prefix=_applescript_escape(prefix),
        )
        result = _run_applescript(script)
        if not result.ok:
            return ChromeResult(
                ok=False, code=result.code,
                message=result.message,
                data={"via": "applescript"},
            )
        out = result.stdout.strip()
        if out == "no_match":
            return ChromeResult(
                ok=False, code="NO_MATCHING_TAB",
                message=f"no tab found with URL prefix {prefix!r}",
                data={"via": "applescript"},
            )
        return ChromeResult(
            ok=True, code="OK",
            message=f"tab matching {prefix!r} raised via AppleScript",
            data={"via": "applescript", "prefix": prefix},
        )

    def _applescript_open_tab(self, url: str) -> ChromeResult:
        """Open a new tab at ``url`` in window 1 (creating one if needed)."""
        script = _APPLESCRIPT_OPEN_TAB_TEMPLATE.format(
            url=_applescript_escape(url),
        )
        result = _run_applescript(script)
        if not result.ok:
            return ChromeResult(
                ok=False, code=result.code,
                message=result.message,
                data={"via": "applescript"},
            )
        return ChromeResult(
            ok=True, code="OK",
            message=f"opened new tab at {url!r} via AppleScript",
            data={"via": "applescript", "url": url, "created": True},
        )


# ─────────────────────────────────────────────────────────────
# AppleScript helpers
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AppleScriptResult:
    ok: bool
    code: str
    message: str
    stdout: str = ""


# Every public path activates Chrome after the tab operation so the OS
# window server raises the window above PowerPoint.
_ACTIVATE_CHROME_APP = 'tell application "Google Chrome" to activate'


_APPLESCRIPT_ACTIVATE_TAB_TEMPLATE = '''tell application "Google Chrome"
    activate
    set target_prefix to "{prefix}"
    set found_it to false
    repeat with w in windows
        set t_idx to 0
        repeat with t in tabs of w
            set t_idx to t_idx + 1
            set u to URL of t
            if u starts with target_prefix then
                set active tab index of w to t_idx
                set index of w to 1
                set found_it to true
                exit repeat
            end if
        end repeat
        if found_it then exit repeat
    end repeat
    if found_it then
        return "ok"
    else
        return "no_match"
    end if
end tell'''


_APPLESCRIPT_OPEN_TAB_TEMPLATE = '''tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    end if
    tell window 1
        make new tab with properties {{URL:"{url}"}}
    end tell
    return "ok"
end tell'''


# Error codes mapped from osascript stderr (subset of powerpoint._ERROR_CODES).
_ERROR_CODES = {
    -1743: ("NO_PERMISSION",
            "macOS denied access to Chrome. Grant Automation in "
            "System Settings → Privacy & Security → Automation."),
    -600:  ("NOT_RUNNING", "Chrome isn't running — launch it and try again."),
    -609:  ("CONNECTION_LOST", "Lost connection to Chrome."),
    -1712: ("TIMED_OUT", "Chrome didn't respond in time."),
}

_ERR_NUM_RE = re.compile(r"\((-?\d+)\)")


def _applescript_escape(s: str) -> str:
    """Escape a string for safe interpolation into an AppleScript
    double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _parse_applescript_error(stderr: str) -> tuple[str, str]:
    stderr = stderr.strip() or "Unknown osascript error"
    m = _ERR_NUM_RE.search(stderr)
    if m:
        try:
            num = int(m.group(1))
        except ValueError:
            num = None
        if num is not None and num in _ERROR_CODES:
            return _ERROR_CODES[num]
    return "OSASCRIPT_ERROR", stderr


def _run_applescript(
    script: str, *, timeout: float = _OSASCRIPT_TIMEOUT,
) -> _AppleScriptResult:
    """Execute an AppleScript and wrap the outcome."""
    if sys.platform != "darwin":
        return _AppleScriptResult(
            ok=False, code="UNSUPPORTED_OS",
            message="Chrome control via AppleScript requires macOS.",
        )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _AppleScriptResult(
            ok=False, code="TIMED_OUT",
            message=f"osascript didn't respond within {timeout:.0f}s.",
        )
    except FileNotFoundError:
        return _AppleScriptResult(
            ok=False, code="NO_OSASCRIPT",
            message="`osascript` binary missing — not a supported macOS environment.",
        )
    except OSError as exc:
        return _AppleScriptResult(
            ok=False, code="OS_ERROR",
            message=f"Failed to invoke osascript: {exc}",
        )

    if result.returncode != 0:
        code, msg = _parse_applescript_error(result.stderr)
        return _AppleScriptResult(ok=False, code=code, message=msg)
    return _AppleScriptResult(
        ok=True, code="OK", message="ok", stdout=result.stdout,
    )


def _applescript_chrome_running() -> bool:
    """Cheap check via ``pgrep`` — avoids firing up the AppleEvent subsystem."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["pgrep", "-xi", "Google Chrome"],
            capture_output=True, timeout=1,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False
