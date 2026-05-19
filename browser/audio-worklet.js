/**
 * audio-worklet.js — AudioWorkletProcessor for 16 kHz PCM capture.
 *
 * Runs on the dedicated audio thread. Downsamples from the AudioContext's
 * native sample rate to 16 kHz, converts Float32 → Int16, and posts
 * 1024-sample Int16Array buffers to the main thread.
 *
 * Also performs lightweight RMS-based voice activity detection: when the
 * incoming mic signal exceeds SPEAKING_THRESHOLD, a { type: 'speaking' }
 * control message is posted so the main thread can perform client-side
 * barge-in (silencing assistant playback locally without waiting for the
 * server round-trip). Events are debounced via SPEAKING_MIN_INTERVAL_MS.
 */

// Tuned for typical laptop mics with echoCancellation + autoGainControl on.
// Post-AEC residual echo is usually well below 0.01 RMS on laptop built-ins,
// but in a big-room setup with external speakers + mic (conference room,
// lavalier), Chrome's AEC can't use the external speaker as a reference
// and residual echo climbs into 0.03-0.05 territory. Typing, chair squeaks,
// and plosive mouth sounds also sit around 0.03-0.05. Setting this to 0.06
// leaves clear headroom above that noise floor while still triggering on
// a normal-volume "Nova" utterance.
// See: (internal postmortem 2026-05-08) § 4 (RC1).
const SPEAKING_THRESHOLD = 0.06;

// Don't spam the main thread — at most one 'speaking' event per interval.
const SPEAKING_MIN_INTERVAL_MS = 150;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(0);
    // sampleRate is a global in AudioWorkletGlobalScope
    this._ratio = sampleRate / 16000;
    this._targetSamples = 1024;
    this._lastSpeakingPostMs = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;

    const raw = input[0]; // mono channel

    // ---- Lightweight RMS-based VAD on the raw input ---- //
    let sumSq = 0;
    for (let i = 0; i < raw.length; i++) sumSq += raw[i] * raw[i];
    const rms = Math.sqrt(sumSq / raw.length);

    if (rms > SPEAKING_THRESHOLD) {
      // currentTime is a global in AudioWorkletGlobalScope (seconds)
      const nowMs = currentTime * 1000;
      if (nowMs - this._lastSpeakingPostMs >= SPEAKING_MIN_INTERVAL_MS) {
        this._lastSpeakingPostMs = nowMs;
        this.port.postMessage({ type: "speaking", rms });
      }
    }

    // ---- Downsample to 16 kHz ---- //
    const outputLen = Math.floor(raw.length / this._ratio);
    const downsampled = new Float32Array(outputLen);
    for (let i = 0; i < outputLen; i++) {
      downsampled[i] = raw[Math.floor(i * this._ratio)];
    }

    // Append to internal buffer
    const merged = new Float32Array(this._buffer.length + downsampled.length);
    merged.set(this._buffer);
    merged.set(downsampled, this._buffer.length);
    this._buffer = merged;

    // Emit 1024-sample Int16 chunks as raw ArrayBuffers (transferable).
    // The main thread differentiates PCM (ArrayBuffer) from control
    // messages (plain objects) via `instanceof ArrayBuffer`.
    while (this._buffer.length >= this._targetSamples) {
      const chunk = this._buffer.subarray(0, this._targetSamples);
      const int16 = new Int16Array(this._targetSamples);
      for (let i = 0; i < this._targetSamples; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(int16.buffer, [int16.buffer]);
      this._buffer = this._buffer.subarray(this._targetSamples);
    }

    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
