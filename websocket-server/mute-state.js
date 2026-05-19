/**
 * mute-state.js — pure logic for the cross-Space mute indicator.
 *
 * The browser is the source of truth for whether the mic is muted (the
 * audio worklet at ``browser/app.js`` gates frames on ``isMuted``).
 * This module mirrors that state into a small process-global object on
 * the Node side so two surfaces can read it:
 *
 *   1. The macOS mute helper (``src/platform/mute_helper.py``) polls
 *      ``GET /mute_state`` at ~250 ms to drive its floating cross-Space
 *      "🎤 Live" / "🔇 Muted" indicator (which is visible above
 *      PowerPoint slideshow + Chrome fullscreen via the
 *      ``NSWindowCollectionBehaviorFullScreenAuxiliary`` flag).
 *
 *   2. The same helper POSTs ``/toggle_mute`` when the global spacebar
 *      hotkey fires AND the user is not in PPT slideshow. The server
 *      broadcasts a ``toggle_mute`` JSON message back to every connected
 *      browser, where ``applyMuteState()`` does the actual mic-frame
 *      gate. The browser then re-emits ``mute_state`` so this module
 *      stays in sync with the (still authoritative) browser flag.
 *
 * The two route helpers below are PURE — no I/O, no globals — so the
 * test surface is tiny and the boot script in ``server.js`` only needs
 * to wire them into the createServer callback. See
 * ``tests/mute-state.test.js`` for the contract.
 */

/**
 * @typedef {Object} MuteState
 * @property {boolean} muted          - True iff the mic-frame gate is on.
 * @property {boolean} session_active - True iff a Nova session is live.
 * @property {number}  updated_at     - Unix-ms of the last update.
 */

/** @returns {MuteState} */
export function freshMuteState() {
  return { muted: false, session_active: false, updated_at: 0 };
}

/**
 * Apply an inbound browser WS message to the mute-state snapshot.
 *
 * Returns a NEW object when the message is a recognised ``mute_state``
 * payload, otherwise returns the prior state unchanged. The caller is
 * responsible for mutating the process-global reference (or replacing
 * it) — keeping this function pure makes the test surface trivial.
 *
 * @param {MuteState} state - Current snapshot.
 * @param {*} msg           - Decoded JSON from the browser.
 * @param {() => number} [now] - Injectable clock for tests; defaults to Date.now.
 * @returns {MuteState}
 */
export function applyBrowserMuteMessage(state, msg, now = Date.now) {
  if (!msg || typeof msg !== "object") return state;
  if (msg.type !== "mute_state") return state;
  return {
    muted: !!msg.muted,
    session_active: !!msg.session_active,
    updated_at: now(),
  };
}

/**
 * Compute the HTTP response (and any broadcast) for a request against
 * the mute-state surface.
 *
 * Returns:
 *   - ``null`` if the request doesn't match any mute route — the caller
 *     falls through to its existing route handlers.
 *   - ``{ status, body, broadcast? }`` with ``status`` as the HTTP code,
 *     ``body`` as a JSON string already serialised, and an OPTIONAL
 *     ``broadcast`` payload the caller should send to every connected
 *     WebSocket client.
 *
 * Why split broadcast from the response: the HTTP and WS surfaces have
 * different lifecycles and error modes; a partial broadcast (one client
 * is gone) shouldn't change the HTTP reply.
 *
 * @param {MuteState} state
 * @param {string} method
 * @param {string} url
 * @param {{ now?: () => number, clientCount?: () => number }} [opts]
 * @returns {null | {status: number, body: string, broadcast?: object}}
 */
export function handleMuteHttp(state, method, url, opts = {}) {
  const now = opts.now || Date.now;
  const clientCount = opts.clientCount || (() => 0);

  if (method === "GET" && url === "/mute_state") {
    return { status: 200, body: JSON.stringify(state) };
  }

  if (method === "POST" && url === "/toggle_mute") {
    if (!state.session_active) {
      return {
        status: 409,
        body: JSON.stringify({
          ok: false,
          reason: "no_active_session",
          session_active: false,
        }),
      };
    }
    return {
      status: 200,
      body: JSON.stringify({
        ok: true,
        broadcast_to: clientCount(),
      }),
      broadcast: {
        type: "toggle_mute",
        source: "global_hotkey",
        t: now(),
      },
    };
  }

  return null;
}
