/**
 * app.js — Browser client for Presentation Assistant.
 *
 * Captures microphone audio (PCM 16-bit mono 16kHz) via getUserMedia with
 * echo cancellation, streams it over WebSocket to the Node.js server, and
 * plays back agent audio (PCM 16-bit mono 24kHz) through AudioContext.
 *
 * Resilience: if the WebSocket drops unexpectedly (Nova Sonic error, network
 * blip, etc.), the browser auto-reconnects with exponential backoff and
 * re-sends session_start using the cached session config.
 */

import {
  confirmedBargeIn,
  resetBargeInHits,
  isInAudioWarmup,
  CLIENT_AUDIO_WARMUP_MS,
} from "./barge-in.js";

// ------------------------------------------------------------------ //
// DOM references
// ------------------------------------------------------------------ //

const startBtn   = document.getElementById("startBtn");
const stopBtn    = document.getElementById("stopBtn");
const muteBtn    = document.getElementById("muteBtn");
const statusDot  = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const statusBar  = document.getElementById("statusBar");
const logEl      = document.getElementById("log");

// ------------------------------------------------------------------ //
// State
// ------------------------------------------------------------------ //

/** @type {WebSocket|null} */
let ws = null;

/** @type {MediaStream|null} */
let micStream = null;

/** @type {AudioContext|null} */
let audioContext = null;

/** @type {ScriptProcessorNode|null} */
let scriptProcessor = null;

/** @type {AudioWorkletNode|null} */
let workletNode = null;

/** @type {AudioContext|null} */
let playbackCtx = null;

/** @type {GainNode|null} — gate between playback sources and the speakers.
 *  Setting its gain to 0 silences assistant audio instantly (used for
 *  server-driven barge-in and client-side local barge-in). */
let playbackGain = null;

/** @type {Set<AudioBufferSourceNode>} — active scheduled sources so we can
 *  stop them on barge-in. Sources auto-remove themselves on ended. */
const activeSources = new Set();

/** Next scheduled playback time (seconds in AudioContext timeline). */
let nextPlayTime = 0;

/** True while assistant audio is currently being scheduled/playing.
 *  Used by the client-side VAD path to know when to mute locally. */
let isAssistantPlaying = false;

/** Timer that clears isAssistantPlaying after scheduled audio ends. */
let playbackEndTimer = null;

/** Cached session config, set on Start, used to auto-reconnect. */
let sessionConfig = null;

/** True while the user wants an active session (Start clicked, Stop not yet). */
let sessionWanted = false;

/** Count of consecutive reconnect attempts (reset on successful connect). */
let reconnectAttempts = 0;

/** Pending setTimeout handle for the next reconnect attempt. */
let reconnectTimer = null;

const MAX_RECONNECT_ATTEMPTS = 10;

/**
 * When the server appears unreachable (connection refused / health check fail),
 * we stop after this many attempts instead of the full MAX_RECONNECT_ATTEMPTS
 * to surface a clear error to the user faster.
 */
const MAX_EARLY_FAIL_ATTEMPTS = 2;

/** Track whether the current reconnect cycle has ever successfully opened. */
let hadOpenConnection = false;

/** True when the mic is muted (audio frames are dropped client-side). */
let isMuted = false;

/**
 * True while Session B (a specialist, e.g. Carlos) owns the speaker floor.
 * The mic is HARD-GATED for the duration — no PCM frames are forwarded
 * and no VAD barge-in signals are sent. This is critical in big-room
 * setups (external speakers + lavalier/array mic) where Chrome's echo
 * cancellation cannot use the speaker as a reference, so the specialist's
 * own voice leaks back into the mic well above the VAD threshold and
 * would terminate Session B within seconds (false barge-in).
 *
 * Session B is, by design (design.md §6.8), audio-OUT only — it takes no
 * user speech input — so muting the mic for its lifetime loses nothing.
 * If the presenter legitimately wants to interrupt, the Mute button
 * toggle or a page reload is the escape hatch.
 *
 * Flipped on/off by `active_session` control messages from the server.
 * See: (internal postmortem 2026-05-08) § 7 P0-#3.
 */
let isMicGatedForSessionB = false;

/**
 * Rolling buffer of ``speaking`` event timestamps for the client-side
 * barge-in confirmation gate (Fix 2A). See ``browser/barge-in.js`` for
 * the full rationale. Reset after every confirmed fire and on every
 * state transition.
 */
const clientBargeInHits = [];

/**
 * Timestamp (``performance.now()`` ms) of the last ``session_started``
 * event, or ``null`` if no session has started yet. Used as the
 * anchor for the ``CLIENT_AUDIO_WARMUP_MS`` window during which VAD
 * hits are ignored wholesale (Fix 2B).
 */
let audioWarmupStartedAt = null;

// ------------------------------------------------------------------ //
// Helpers
// ------------------------------------------------------------------ //

function log(msg) {
  const line = `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  logEl.textContent += line;
  logEl.scrollTop = logEl.scrollHeight;
}

function setStatus(status, text) {
  statusDot.className = "status-dot " + status;
  statusText.textContent = text;
  if (statusBar) {
    statusBar.classList.toggle("active", status === "connected");
  }
}

function updateSlideIndicator(current, total) {
  let el = document.getElementById("slideIndicator");
  if (!el) {
    el = document.createElement("div");
    el.id = "slideIndicator";
    el.style.cssText = "text-align:center;padding:8px;font-size:13px;font-weight:500;color:var(--text-secondary);";
    const controls = document.querySelector(".controls");
    if (controls) controls.parentNode.insertBefore(el, controls.nextSibling);
  }
  el.textContent = `Slide ${current} of ${total}`;
}

/**
 * Render a small badge indicating which voice agent currently owns the
 * floor. Fires on `active_session` control messages from the server:
 *   { type: "active_session", who: "A"|"B",
 *     voice?: string, agent_id?: string, display_name?: string }
 * While Session A ("Presenter") is active the badge is muted. When
 * Session B takes over, the badge lights up in the accent color with
 * the specialist's display name.
 */
function updateActiveSessionBadge(msg) {
  let el = document.getElementById("activeSessionBadge");
  if (!el) {
    el = document.createElement("div");
    el.id = "activeSessionBadge";
    el.style.cssText =
      "text-align:center;padding:6px 12px;margin:4px auto;" +
      "font-size:12px;letter-spacing:.12em;text-transform:uppercase;" +
      "font-weight:500;border-radius:999px;display:inline-block;" +
      "transition:background-color .25s, color .25s, border-color .25s;" +
      "border:1px solid rgba(255,255,255,.06);";
    const statusBar = document.getElementById("statusBar");
    if (statusBar?.parentNode) {
      statusBar.parentNode.insertBefore(el, statusBar.nextSibling);
    }
    // Center the inline-block.
    const wrap = document.createElement("div");
    wrap.style.textAlign = "center";
    el.parentNode.insertBefore(wrap, el);
    wrap.appendChild(el);
  }

  if (msg.who === "B") {
    const name = msg.display_name || msg.agent_id || "Specialist";
    const voice = msg.voice ? ` · ${msg.voice}` : "";
    // Append a small 🔇 cue so the presenter knows the mic is silent on
    // purpose while the specialist has the floor (big-room safety — see
    // isMicGatedForSessionB + (internal postmortem 2026-05-08)).
    el.textContent = `Analyst: ${name}${voice} · 🔇 Mic gated`;
    el.style.color = "var(--aws-orange)";
    el.style.borderColor = "var(--border-focus)";
    el.style.background = "var(--aws-orange-glow)";
  } else {
    el.textContent = `Presenter${msg.voice ? ` · ${msg.voice}` : ""}`;
    el.style.color = "var(--text-muted)";
    el.style.borderColor = "var(--border)";
    el.style.background = "transparent";
  }
}

// ------------------------------------------------------------------ //
// Audio capture
// ------------------------------------------------------------------ //

function float32ToInt16(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return int16;
}

function downsampleTo16k(buffer, inputRate) {
  if (inputRate === 16000) return buffer;
  const ratio = inputRate / 16000;
  const outputLen = Math.floor(buffer.length / ratio);
  const output = new Float32Array(outputLen);
  for (let i = 0; i < outputLen; i++) {
    output[i] = buffer[Math.floor(i * ratio)];
  }
  return output;
}

/**
 * P0-1: if mic capture cannot start, tell the server to end the session so
 * Nova Sonic doesn't sit waiting 55 s for audio and trigger a reconnect storm.
 * @param {string} reason human-readable failure reason
 */
function abortSessionDueToMicFailure(reason) {
  log("ERROR: " + reason);
  setStatus("error", "Mic unavailable");
  // Prevent the auto-reconnect loop from re-opening the WebSocket.
  sessionWanted = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  // Best-effort: tell the server so it tears down Nova Sonic instead of
  // waiting for the 55-second audio-gap timeout.
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "session_end" }));
    } catch { /* ignore */ }
    try { ws.close(); } catch { /* ignore */ }
  }
  cleanup();
}

async function startMicCapture() {
  // Skip if already capturing (reconnect path)
  if (workletNode || scriptProcessor) {
    log("Mic capture already active, skipping.");
    return;
  }

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        sampleRate: 16000,
        channelCount: 1,
      },
    });
  } catch (err) {
    const reason =
      err.name === "NotAllowedError"
        ? "Microphone permission denied. Please allow microphone access and try again."
        : `Microphone error: ${err.message}`;
    abortSessionDueToMicFailure(reason);
    return;
  }

  // Sanity check: some browsers return a MediaStream whose audio track is
  // immediately muted (no physical mic, OS-level block). Catch it here
  // instead of letting Nova Sonic time out.
  const audioTrack = micStream.getAudioTracks()[0];
  if (!audioTrack || audioTrack.muted || !audioTrack.enabled) {
    abortSessionDueToMicFailure(
      "Microphone track is muted or disabled. Check your OS input settings."
    );
    return;
  }

  audioContext = new AudioContext({ sampleRate: 16000 });
  const source = audioContext.createMediaStreamSource(micStream);

  // Try AudioWorklet first
  try {
    if (!audioContext.audioWorklet) throw new Error("audioWorklet not supported");
    await audioContext.audioWorklet.addModule("audio-worklet.js");
    workletNode = new AudioWorkletNode(audioContext, "pcm-capture-processor");

    workletNode.port.onmessage = (event) => {
      // Control messages (plain objects) vs. PCM data (ArrayBuffer).
      if (!(event.data instanceof ArrayBuffer)) {
        if (event.data && event.data.type === "speaking") {
          // Client-side barge-in: user is speaking while assistant audio
          // is playing → silence playback immediately. The server-side
          // barge-in will still fire via Nova Sonic's own VAD a moment
          // later; this just removes the round-trip latency.
          //
          // GATES (must ALL be true):
          //   • isAssistantPlaying — if nothing is playing, there's
          //     nothing to barge in on; any RMS spike is ambient noise.
          //   • !isMuted — user explicitly silenced the mic.
          //   • !isMicGatedForSessionB — server-owned gate: during
          //     Session B we never accept client VAD (big-room echo
          //     loop); the server will handback on terminator phrase
          //     or end_session instead.
          //   • !isInAudioWarmup — the first CLIENT_AUDIO_WARMUP_MS
          //     after session_started, Chrome's AEC hasn't trained
          //     and the AudioContext startup "pop" lands in the mic
          //     above 0.06 RMS. Ignoring VAD here prevents Nova from
          //     cutting itself off mid-greeting (Fix 2B — see
          //     (internal postmortem 2026-05-09)).
          //
          // Beyond the gates, a SINGLE RMS spike is never enough:
          // confirmedBargeIn() requires ≥ CLIENT_BARGE_IN_MIN_HITS
          // within CLIENT_BARGE_IN_WINDOW_MS (Fix 2A). This mirrors
          // the server-side BARGE_IN_MIN_HITS=3/600ms gate in
          // session-manager.js and prevents single pops (chair squeak,
          // keyboard click, speaker pop) from destroying the utterance.
          const now = performance.now();
          if (!isAssistantPlaying || isMuted || isMicGatedForSessionB
              || isInAudioWarmup(audioWarmupStartedAt, now)) {
            return;
          }
          if (!confirmedBargeIn(clientBargeInHits, now)) {
            return;   // single spike — wait for sustained activity
          }
          // Confirmed: fire local mute + notify server, then reset.
          mutePlaybackNow();
          resetBargeInHits(clientBargeInHits);
          if (ws && ws.readyState === WebSocket.OPEN) {
            try {
              ws.send(JSON.stringify({
                type: "barge_in_detected",
                rms: event.data.rms,
              }));
            } catch { /* socket churn — ignore */ }
          }
        }
        return;
      }
      // PCM audio buffer → forward to WebSocket.
      // HARD GATE: during Session B, no mic frames are forwarded. This
      // prevents the specialist's own voice (leaking through speakers
      // into the mic in a big-room setup) from polluting Session A's
      // Nova Sonic input stream and from being relayed upstream.
      if (isMuted || isMicGatedForSessionB) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(event.data);
    };

    source.connect(workletNode).connect(audioContext.destination);
    log("Microphone capture started via AudioWorklet (echo cancellation enabled).");
  } catch (workletErr) {
    workletNode = null;
    try {
      const nativeRate = audioContext.sampleRate;
      scriptProcessor = audioContext.createScriptProcessor(512, 1, 1);

      scriptProcessor.onaudioprocess = (e) => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        const raw = e.inputBuffer.getChannelData(0);

        // Client-side VAD — mirrors the worklet. Gives us instant barge-in
        // on the fallback path too. Skipped entirely while the specialist
        // owns the floor (big-room echo loop — see isMicGatedForSessionB).
        //
        // Warm-up + confirmation gates identical to the AudioWorklet path
        // above — see there for the full rationale (Fix 2A + 2B).
        const now = performance.now();
        if (isAssistantPlaying && !isMuted && !isMicGatedForSessionB
            && !isInAudioWarmup(audioWarmupStartedAt, now)) {
          let sumSq = 0;
          for (let i = 0; i < raw.length; i++) sumSq += raw[i] * raw[i];
          const rms = Math.sqrt(sumSq / raw.length);
          // Threshold aligned with worklet's SPEAKING_THRESHOLD (0.06).
          // See: (internal postmortem 2026-05-08) § 4 RC1.
          if (rms > 0.06) {
            if (confirmedBargeIn(clientBargeInHits, now)) {
              mutePlaybackNow();
              resetBargeInHits(clientBargeInHits);
              // Also notify the server so it can handback from Session B.
              try {
                ws.send(JSON.stringify({ type: "barge_in_detected", rms }));
              } catch { /* socket churn — ignore */ }
            }
            // else: single spike — wait for sustained activity.
          }
        }

        // HARD GATE: during Session B, no mic frames are forwarded.
        if (isMuted || isMicGatedForSessionB) return;
        const downsampled = downsampleTo16k(raw, nativeRate);
        const pcm16 = float32ToInt16(downsampled);
        ws.send(pcm16.buffer);
      };

      source.connect(scriptProcessor);
      scriptProcessor.connect(audioContext.destination);
      log("Microphone capture started via ScriptProcessor fallback.");
    } catch (fallbackErr) {
      abortSessionDueToMicFailure(
        `Audio pipeline failed to start: ${fallbackErr.message}`
      );
      return;
    }
  }
}

// ------------------------------------------------------------------ //
// Audio playback
// ------------------------------------------------------------------ //

function playAudio(arrayBuffer) {
  if (!playbackCtx) {
    playbackCtx = new AudioContext({ sampleRate: 24000 });
    playbackGain = playbackCtx.createGain();
    playbackGain.gain.value = 1;
    playbackGain.connect(playbackCtx.destination);
    nextPlayTime = 0;
  }

  const int16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 0x8000;
  }

  const buffer = playbackCtx.createBuffer(1, float32.length, 24000);
  buffer.getChannelData(0).set(float32);

  const source = playbackCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(playbackGain);

  const now = playbackCtx.currentTime;
  if (nextPlayTime < now) nextPlayTime = now;
  source.start(nextPlayTime);
  nextPlayTime += buffer.duration;

  // Track so we can .stop() them on barge-in
  activeSources.add(source);
  source.onended = () => activeSources.delete(source);

  // Mark assistant as playing, and schedule a reset for after the queued
  // audio finishes (used by client-side VAD to know when to mute locally).
  isAssistantPlaying = true;
  if (playbackEndTimer) clearTimeout(playbackEndTimer);
  const msUntilDone = Math.max(0, (nextPlayTime - now) * 1000) + 50;
  playbackEndTimer = setTimeout(() => {
    isAssistantPlaying = false;
    playbackEndTimer = null;
  }, msUntilDone);
}

/**
 * Immediately silence any assistant audio currently playing or scheduled.
 * Used by both server-driven barge-in (from Nova Sonic's interrupted event)
 * and client-side barge-in (from the worklet's RMS detector).
 */
function mutePlaybackNow() {
  if (!playbackCtx || !playbackGain) return;
  // Ramp to 0 fast to avoid clicks
  const now = playbackCtx.currentTime;
  playbackGain.gain.cancelScheduledValues(now);
  playbackGain.gain.setValueAtTime(playbackGain.gain.value, now);
  playbackGain.gain.linearRampToValueAtTime(0, now + 0.02);
  // Stop all queued sources so they don't keep consuming time
  for (const src of activeSources) {
    try { src.stop(); } catch { /* already stopped */ }
  }
  activeSources.clear();
  nextPlayTime = 0;
  isAssistantPlaying = false;
  if (playbackEndTimer) { clearTimeout(playbackEndTimer); playbackEndTimer = null; }
  // Re-open the gate for the next utterance
  playbackGain.gain.setValueAtTime(0, now + 0.02);
  playbackGain.gain.linearRampToValueAtTime(1, now + 0.08);
}

// ------------------------------------------------------------------ //
// WebSocket connection + auto-reconnect
// ------------------------------------------------------------------ //

function openWebSocket() {
  if (!sessionConfig) return;

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}`;
  ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    hadOpenConnection = true;
    log("WebSocket open — sending session_start");
    ws.send(JSON.stringify({
      type: "session_start",
      voice_id: sessionConfig.voiceId,
      language_locale: sessionConfig.languageLocale,
      assistant_name: sessionConfig.assistantName,
      personality: sessionConfig.personality,
      region: "us-east-1",
      python_backend_url: "http://127.0.0.1:8000",
    }));
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      playAudio(event.data);
      return;
    }

    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "session_started":
        setStatus("connected", "Session active — speak!");
        stopBtn.disabled = false;
        muteBtn.disabled = false;
        startBtn.disabled = true;
        reconnectAttempts = 0;
        log("Session started. Speak into your microphone.");
        // Notify the cross-Space mute helper that a session is now
        // live so its overlay flips from hidden → "🎤 Live". The
        // helper also uses ``session_active`` to decide whether the
        // global spacebar hotkey should intercept (otherwise space
        // passes through to whatever app the user is in).
        emitMuteState();
        // Anchor the client-side VAD warm-up window. For the next
        // CLIENT_AUDIO_WARMUP_MS, any "speaking" events from the
        // worklet / ScriptProcessor are ignored so Chrome's AEC
        // can train on Nova's first playback buffer without us
        // false-positive-ing on the startup pop or ambient noise.
        // See browser/barge-in.js and Fix 2B in
        // (internal postmortem 2026-05-09).
        audioWarmupStartedAt = performance.now();
        resetBargeInHits(clientBargeInHits);
        log(`Audio warm-up: ${CLIENT_AUDIO_WARMUP_MS} ms (client VAD suppressed).`);
        startMicCapture();
        break;

      case "reconnected":
        setStatus("connected", "Session active — speak!");
        reconnectAttempts = 0;
        log("Nova Sonic session reconnected successfully.");
        break;

      case "session_end":
        log("Session ended by server.");
        if (sessionWanted) {
          log("Attempting auto-reconnect...");
          scheduleReconnect();
        } else {
          cleanup();
        }
        break;

      case "barge_in":
        // Server detected Nova's 'interrupted' text event. Silence the
        // playback instantly without tearing down the AudioContext so the
        // next utterance can start playing without setup latency.
        mutePlaybackNow();
        break;

      case "slide_change":
        updateSlideIndicator(msg.slide_index, msg.total_slides);
        log(`Slide ${msg.slide_index}/${msg.total_slides}`);
        break;

      case "active_session":
        updateActiveSessionBadge(msg);
        // HARD MIC GATE: during Session B, the specialist speaks through
        // the shared speaker. In big-room setups the specialist's own
        // voice leaks into the mic above any realistic VAD threshold and
        // would trigger a false barge-in within seconds. Session B takes
        // no audio input by design, so muting the mic for its lifetime
        // costs us nothing. On handback (who === "A") we re-open the mic.
        // See: (internal postmortem 2026-05-08) § 7.
        isMicGatedForSessionB = (msg.who === "B");
        log(`Active session → ${msg.who === "B"
            ? (msg.display_name || msg.agent_id || "Specialist")
            : "Presenter"} (${msg.voice || ""})${
            isMicGatedForSessionB ? " — mic gated" : ""}`);
        break;

      case "error":
        log(`ERROR: ${msg.message || "unknown"}`);
        setStatus("error", "Error");
        break;

      case "toggle_mute":
        // Global spacebar hotkey fired (the macOS helper's CGEventTap
        // POST'd /toggle_mute and the server broadcast it back to us).
        // Refuse if the session isn't live — the server already gates
        // POST /toggle_mute on session_active, but a stale broadcast
        // could still arrive between session-end and WS-close.
        if (muteBtn.disabled) {
          log(`Ignored toggle_mute (${msg.source || "?"}): session not active.`);
          break;
        }
        log(`Mute toggled via ${msg.source || "remote"}.`);
        isMuted = !isMuted;
        applyMuteState();
        break;

      default:
        log(`Unknown message type: ${msg.type}`);
    }
  };

  ws.onclose = () => {
    log("WebSocket disconnected.");
    if (sessionWanted) {
      scheduleReconnect();
    } else {
      cleanup();
    }
  };

  ws.onerror = () => {
    log("WebSocket error.");
  };
}

/**
 * Schedule the next reconnect attempt with exponential backoff.
 *
 * Fast-fail behavior: if the WebSocket has never successfully opened in this
 * session, we cap retries at MAX_EARLY_FAIL_ATTEMPTS and surface a clear
 * "server unreachable" error instead of cycling through all 10 attempts.
 */
function scheduleReconnect() {
  if (!sessionWanted) return;
  if (reconnectTimer) return;

  reconnectAttempts++;

  // Early fail path: server was never reachable — likely not running.
  if (!hadOpenConnection && reconnectAttempts > MAX_EARLY_FAIL_ATTEMPTS) {
    log(
      `Server unreachable after ${MAX_EARLY_FAIL_ATTEMPTS} attempts. ` +
      `Is the WebSocket server running on ${window.location.host}?`
    );
    setStatus("error", "Server unreachable");
    sessionWanted = false;
    cleanup();
    return;
  }

  if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
    log(`Reconnect failed after ${MAX_RECONNECT_ATTEMPTS} attempts. Giving up.`);
    setStatus("error", "Connection lost");
    sessionWanted = false;
    cleanup();
    return;
  }

  const backoffMs = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 15000);
  const maxAttempts = hadOpenConnection ? MAX_RECONNECT_ATTEMPTS : MAX_EARLY_FAIL_ATTEMPTS;
  setStatus("connecting", `Reconnecting in ${Math.round(backoffMs / 1000)}s (attempt ${reconnectAttempts}/${maxAttempts})...`);
  log(`Reconnect attempt ${reconnectAttempts}/${maxAttempts} in ${backoffMs}ms`);

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!sessionWanted) return;
    openWebSocket();
  }, backoffMs);
}

// ------------------------------------------------------------------ //
// Start / Stop buttons
// ------------------------------------------------------------------ //

startBtn.addEventListener("click", async () => {
  const pptxPath = document.getElementById("pptxPath").value;
  const voiceSelect = document.getElementById("voiceId").value;
  const assistantName = document.getElementById("assistantName").value.trim() || "Nova";
  const personality = document.getElementById("personality").value;
  const [voiceId, languageLocale] = voiceSelect.split("|");

  startBtn.disabled = true;
  setStatus("connecting", "Connecting...");
  log("Connecting to WebSocket server...");

  // Pre-flight: quick health check so we fail fast with a clear message
  // if the Node.js server isn't running, instead of letting the user watch
  // the WebSocket retry loop.
  try {
    const healthCtrl = new AbortController();
    const healthTimer = setTimeout(() => healthCtrl.abort(), 2000);
    const healthRes = await fetch("/healthz", { signal: healthCtrl.signal });
    clearTimeout(healthTimer);
    if (!healthRes.ok) throw new Error(`health check returned ${healthRes.status}`);
    const health = await healthRes.json();
    log(`Server healthy (uptime ${health.uptimeSec}s)`);
  } catch (err) {
    const msg = err.name === "AbortError"
      ? "Server not responding (2s timeout)"
      : `Server unreachable: ${err.message}`;
    log(`ERROR: ${msg}. Is the WebSocket server running?`);
    setStatus("error", "Server unreachable");
    startBtn.disabled = false;
    return;
  }

  // Preprocess the PPTX via Python backend first
  try {
    const prepRes = await fetch("http://127.0.0.1:8000/preprocess", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pptx_path: pptxPath }),
    });
    const prepData = await prepRes.json();
    if (!prepRes.ok) throw new Error(prepData.detail || "Preprocess failed");
    log(`Preprocessed: ${prepData.slide_count} slides loaded`);
  } catch (err) {
    log(`ERROR: ${err.message}`);
    setStatus("error", "Preprocess failed");
    startBtn.disabled = false;
    return;
  }

  // Cache config and mark session as wanted
  sessionConfig = { voiceId, languageLocale, assistantName, personality };
  sessionWanted = true;
  reconnectAttempts = 0;
  hadOpenConnection = false;

  openWebSocket();
});

stopBtn.addEventListener("click", () => {
  sessionWanted = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "session_end" }));
  }
  cleanup();
});

/**
 * Apply ``isMuted`` to the UI + worklet gate, log it once, and notify
 * the Node WS server so the cross-Space mute helper can mirror the
 * state in its menu-bar / floating overlay.
 *
 * Single source of truth for everything that flips ``isMuted``:
 *   • The Mute button click handler.
 *   • The in-tab spacebar keydown handler (browser-foreground path).
 *   • The ``toggle_mute`` WS message from the macOS helper's CGEventTap
 *     (global-hotkey path).
 *
 * The worklet/ScriptProcessor mic-frame gates already read ``isMuted``
 * directly (see lines 340 / 366 / 411), so flipping the variable
 * before this function returns is enough to actually silence the mic.
 */
function applyMuteState() {
  if (isMuted) {
    muteBtn.classList.add("muted");
    muteBtn.textContent = "🔇 Unmute";
    log("Microphone muted — Nova cannot hear you.");
  } else {
    muteBtn.classList.remove("muted");
    muteBtn.textContent = "🎤 Mute";
    log("Microphone unmuted.");
  }
  emitMuteState();
}

/**
 * Push the current mute / session-active state up to the Node WS
 * server. The server stashes the snapshot in a process-global so the
 * macOS mute helper can poll GET /mute_state at ~250 ms and update its
 * floating "🎤 Live" / "🔇 Muted" indicator without holding a WS open.
 *
 * Cheap (~tens of bytes), idempotent, and safe to call from anywhere
 * — if the WS isn't open yet (e.g. during cleanup), this no-ops.
 */
function emitMuteState() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify({
      type: "mute_state",
      muted: !!isMuted,
      session_active: !muteBtn.disabled,
    }));
  } catch {
    /* WS may have closed between the readyState check and send — ignore */
  }
}

muteBtn.addEventListener("click", () => {
  isMuted = !isMuted;
  applyMuteState();
});

/**
 * Spacebar inside the voice-UI tab toggles mute (browser-foreground
 * fallback for the global-hotkey path implemented in
 * src/platform/mute_helper.py via CGEventTap). When Chrome is on a
 * different macOS Space than the user's foreground app, the global
 * hotkey path takes over via POST /toggle_mute → ``toggle_mute`` WS
 * message → also routes through ``applyMuteState()``.
 *
 * Guards:
 * - Session must be active (the existing ``muteBtn.disabled`` flag is
 *   cleared in the ``session_started`` case).
 * - Don't hijack space when the user is typing — preserve normal
 *   space-bar behaviour inside any input/textarea/select/contentEditable.
 * - preventDefault() so space doesn't scroll the page or activate a
 *   focused button (Start/Stop are otherwise space-activatable while
 *   focused).
 */
window.addEventListener("keydown", (e) => {
  if (e.code !== "Space" && e.key !== " ") return;
  if (muteBtn.disabled) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
            || t.tagName === "SELECT" || t.isContentEditable)) {
    return;
  }
  e.preventDefault();
  isMuted = !isMuted;
  applyMuteState();
});

// ------------------------------------------------------------------ //
// Cleanup
// ------------------------------------------------------------------ //

function cleanup() {
  // Tell the cross-Space mute helper that the session is over BEFORE
  // we close the WS, otherwise the helper waits up to its 250 ms
  // poll cadence to notice. We explicitly send session_active=false
  // here rather than calling emitMuteState() because the muteBtn.disabled
  // flag is still false at this point (it's set later in this function),
  // so emitMuteState() would compute the wrong value.
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({
        type: "mute_state",
        muted: false,
        session_active: false,
      }));
    } catch { /* WS closing — server's ws.on('close') is the backstop */ }
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (workletNode) {
    workletNode.disconnect();
    workletNode = null;
  }
  if (scriptProcessor) {
    scriptProcessor.disconnect();
    scriptProcessor = null;
  }
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (playbackCtx) {
    playbackCtx.close().catch(() => {});
    playbackCtx = null;
  }
  playbackGain = null;
  activeSources.clear();
  nextPlayTime = 0;
  isAssistantPlaying = false;
  if (playbackEndTimer) { clearTimeout(playbackEndTimer); playbackEndTimer = null; }
  ws = null;
  sessionConfig = null;
  sessionWanted = false;
  reconnectAttempts = 0;
  isMuted = false;
  isMicGatedForSessionB = false;
  muteBtn.classList.remove("muted");
  muteBtn.textContent = "🎤 Mute";
  muteBtn.disabled = true;
  startBtn.disabled = false;
  stopBtn.disabled = true;
  setStatus("", "Disconnected");
}

// ------------------------------------------------------------------ //
// Auto-fill PPTX path from the active PowerPoint presentation
// ------------------------------------------------------------------ //
(async () => {
  try {
    const res = await fetch("http://127.0.0.1:8000/active_pptx");
    if (res.ok) {
      const { pptx_path } = await res.json();
      if (pptx_path) {
        document.getElementById("pptxPath").value = pptx_path;
      }
    }
  } catch { /* backend not up yet — keep default */ }
})();
