/**
 * barge-in.js — client-side barge-in confirmation + warm-up helpers.
 *
 * Pure, dependency-free logic extracted from app.js so it can be
 * unit-tested directly from Node (via ES-module import) without
 * needing a full browser context, an AudioContext, or a mock mic.
 *
 * Why this exists
 * ---------------
 * The original browser VAD path fired ``mutePlaybackNow()`` on the
 * FIRST RMS sample above ``SPEAKING_THRESHOLD``. That one-spike
 * behavior is safe during steady-state playback (Chrome's AEC has
 * trained and the noise floor is well-known), but it is NOT safe
 * during the first ~1–2 seconds after a session starts:
 *
 *   - The AEC has zero history on the first playback buffer ever
 *     queued, so its reference signal is uncalibrated and residual
 *     echo spills into the mic well above 0.06 RMS.
 *   - The ``new AudioContext({sampleRate: 24000})`` allocation itself
 *     causes a brief speaker "pop" that lands back in the mic.
 *   - The click/tap on "Start Session" and any ambient room noise
 *     at t=0 are still arriving in the mic when Nova's first audio
 *     chunk starts playing.
 *
 * Net effect, repeatedly observed (internal postmortem 2026-05-09 §
 * 5): one RMS spike in the first 600
 * ms cut ``mutePlaybackNow()`` → every queued audio source was
 * .stop()'d → the user heard Nova utter one syllable and go silent.
 *
 * Policy
 * ------
 *  - Require at least ``CLIENT_BARGE_IN_MIN_HITS`` separate
 *    ``speaking`` events within ``CLIENT_BARGE_IN_WINDOW_MS`` before
 *    ``mutePlaybackNow()`` is called. Mirrors the SERVER-SIDE
 *    confirmation already in ``session-manager.js`` for the
 *    ``barge_in_detected`` → handback path (§ 3 P0-#4 of the
 *    2026-05-08 postmortem).
 *  - Ignore ``speaking`` events entirely during a
 *    ``CLIENT_AUDIO_WARMUP_MS`` window after ``session_started`` —
 *    this is the interval during which AEC is measurably unreliable.
 *
 * Both numbers are intentionally conservative. 3 hits in 600 ms is
 * ~200 ms of sustained energy, which is short enough that a real
 * "Nova stop" utterance still triggers a handback during Session B,
 * but long enough that a single pop / chair squeak / keyboard click
 * never will.
 */

// ─────────────────────────────────────────────────────────────
// Tunables
// ─────────────────────────────────────────────────────────────

/**
 * Minimum number of ``speaking`` events inside
 * ``CLIENT_BARGE_IN_WINDOW_MS`` before the client considers it a
 * confirmed user voice. Matches ``BARGE_IN_MIN_HITS`` in
 * ``session-manager.js``.
 */
export const CLIENT_BARGE_IN_MIN_HITS = 3;

/**
 * Rolling window (ms) inside which hits are counted. Matches
 * ``BARGE_IN_CONFIRM_WINDOW_MS`` in ``session-manager.js``.
 */
export const CLIENT_BARGE_IN_WINDOW_MS = 600;

/**
 * Time after ``session_started`` during which any ``speaking`` event
 * is ignored wholesale. Gives Chrome's AEC a chance to train on the
 * first playback buffer and prevents the "pop" on AudioContext
 * startup from triggering a self-interrupt.
 *
 * Chosen empirically: 1500 ms is long enough to cover Chrome's AEC
 * warm-up on the MacBook Air M1 + built-in speaker combination used
 * for the live demo, and still far shorter than Nova's shortest
 * opening greeting.
 */
export const CLIENT_AUDIO_WARMUP_MS = 1500;

// ─────────────────────────────────────────────────────────────
// Pure helpers
// ─────────────────────────────────────────────────────────────

/**
 * Record a hit and decide whether the barge-in should be confirmed.
 *
 * Mutates ``hits`` in place: appends ``now`` and drops entries older
 * than ``windowMs``. Returns ``true`` iff the trimmed list has at
 * least ``minHits`` entries.
 *
 * @param {number[]} hits   Rolling buffer of hit timestamps (ms).
 *   Shared across calls for the current session; reset via
 *   ``resetBargeInHits`` after a confirmed fire so the next
 *   utterance starts fresh.
 * @param {number} now      Current time in ms (usually
 *   ``performance.now()``). Parameterized so tests can feed
 *   deterministic values.
 * @param {number} [windowMs=CLIENT_BARGE_IN_WINDOW_MS]
 * @param {number} [minHits=CLIENT_BARGE_IN_MIN_HITS]
 * @returns {boolean} ``true`` iff barge-in is confirmed.
 */
export function confirmedBargeIn(
  hits,
  now,
  windowMs = CLIENT_BARGE_IN_WINDOW_MS,
  minHits = CLIENT_BARGE_IN_MIN_HITS,
) {
  if (!Array.isArray(hits)) {
    throw new TypeError("confirmedBargeIn: hits must be an array");
  }
  // Trim expired entries in place (O(n); n is tiny — at most ~10).
  const cutoff = now - windowMs;
  let i = 0;
  while (i < hits.length && hits[i] < cutoff) i++;
  if (i > 0) hits.splice(0, i);
  hits.push(now);
  return hits.length >= minHits;
}

/**
 * Clear the rolling hit buffer. Call this after ``mutePlaybackNow()``
 * fires so the next utterance starts from zero hits — otherwise
 * residual hits from the current one would lower the bar for the next.
 *
 * @param {number[]} hits
 */
export function resetBargeInHits(hits) {
  if (!Array.isArray(hits)) return;
  hits.length = 0;
}

/**
 * Whether we are still inside the post-session-started audio warm-up
 * window during which client-side VAD must be ignored.
 *
 * @param {number|null} warmupStartedAtMs  timestamp of
 *   ``session_started`` in the same clock as ``now``, or ``null`` if
 *   no session has started yet.
 * @param {number} now  current time (ms).
 * @param {number} [warmupMs=CLIENT_AUDIO_WARMUP_MS]
 * @returns {boolean}
 */
export function isInAudioWarmup(
  warmupStartedAtMs,
  now,
  warmupMs = CLIENT_AUDIO_WARMUP_MS,
) {
  if (warmupStartedAtMs === null || warmupStartedAtMs === undefined) {
    // No session yet → treat as warm-up (suppress VAD) to be safe.
    return true;
  }
  return (now - warmupStartedAtMs) < warmupMs;
}
