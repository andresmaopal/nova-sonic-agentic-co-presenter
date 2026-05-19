#!/usr/bin/env python3
"""Set the Chrome window containing a URL-prefix tab to a specific state.

Used by scripts/demo-setup-fullscreen.sh — we use CDP directly (not
Cmd+Ctrl+F keystroke injection) because the keystroke path is
unreliable when Chrome isn't strictly frontmost at the moment the
keystroke fires (observed silent no-op on 2026-05-10).

Usage:
    python scripts/chrome_set_window_state.py <url_prefix> <state>

Where <state> is one of: normal, minimized, maximized, fullscreen.

Exits 0 on success, 1 on CDP unreachable, 2 on no matching tab,
3 on a CDP protocol error.
"""
import asyncio
import sys

# Make the repo's src/ importable regardless of how this is called.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.platform.chrome import ChromeAdapter  # noqa: E402


VALID_STATES = {"normal", "minimized", "maximized", "fullscreen"}


async def main(prefix: str, target_state: str) -> int:
    if target_state not in VALID_STATES:
        print(f"invalid state: {target_state!r}; use one of {sorted(VALID_STATES)}",
              file=sys.stderr)
        return 2

    chrome = ChromeAdapter()
    try:
        browser = await chrome.connect()
        if browser is None:
            print("CDP not reachable at http://127.0.0.1:9222", file=sys.stderr)
            return 1

        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if (p.url or "").startswith(prefix):
                    page = p
                    break
            if page:
                break
        if page is None:
            print(f"no tab found matching prefix {prefix!r}", file=sys.stderr)
            return 2

        session = await page.context.new_cdp_session(page)
        try:
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if not isinstance(window_id, int):
                print("Browser.getWindowForTarget returned no windowId",
                      file=sys.stderr)
                return 3

            current = await session.send(
                "Browser.getWindowBounds", {"windowId": window_id},
            )
            current_state = (current.get("bounds") or {}).get("windowState")
            if current_state == target_state:
                print(f"already in state={target_state}")
                return 0

            # Direct transitions from maximized/minimized/fullscreen to
            # another non-normal state can be rejected by Chrome. Go via
            # "normal" first in those cases.
            if current_state in ("maximized", "minimized", "fullscreen") \
               and target_state != "normal":
                await session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
                # Give Chrome a chance to finalize the intermediate state.
                await asyncio.sleep(0.35)

            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": target_state}},
            )
            # Let the window manager animation complete before we report.
            await asyncio.sleep(1.0)

            verify = await session.send(
                "Browser.getWindowBounds", {"windowId": window_id},
            )
            final_state = (verify.get("bounds") or {}).get("windowState")
            if final_state != target_state:
                print(
                    f"WARN: requested state={target_state} but Chrome reports "
                    f"state={final_state}", file=sys.stderr,
                )
                return 3
            print(f"OK: windowId={window_id} state={final_state}")
            return 0
        finally:
            try:
                await session.detach()
            except Exception:   # noqa: BLE001
                pass
    finally:
        await chrome.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <url_prefix> <state>", file=sys.stderr)
        print(f"       state in: {sorted(VALID_STATES)}", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
