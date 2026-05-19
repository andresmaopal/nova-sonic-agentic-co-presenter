/**
 * nova-sonic-client.js — Manages the Nova Sonic bidirectional stream.
 *
 * Handles the full lifecycle: opening the stream, sending the required
 * event sequence (sessionStart → promptStart → system prompt → audio input),
 * streaming PCM audio, routing tool results, and session renewal for the
 * 8-minute connection limit.
 *
 * Uses @aws-sdk/client-bedrock-runtime with InvokeModelWithBidirectionalStreamCommand.
 * All Bedrock calls authenticate via SigV4 through the default AWS credential
 * chain — no hardcoded keys (Requirements 9.1, 9.4).
 */

import { randomUUID } from "node:crypto";
import {
  BedrockRuntimeClient,
  InvokeModelWithBidirectionalStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";

/** @typedef {{ type: string, data?: string, toolUseId?: string, toolName?: string, toolInput?: object, text?: string }} NovaSonicEvent */

export class NovaSonicClient {
  /**
   * @param {object} opts
   * @param {string} [opts.region="us-east-1"]
   * @param {string} [opts.modelId="amazon.nova-2-sonic-v1:0"]
   * @param {string} [opts.voiceId="tiffany"]
   */
  constructor({ region = "us-east-1", modelId = "amazon.nova-2-sonic-v1:0", voiceId = "tiffany" } = {}) {
    this.region = region;
    this.modelId = modelId;
    this.voiceId = voiceId;

    /** @type {BedrockRuntimeClient} */
    this.client = new BedrockRuntimeClient({ region: this.region });

    this.isActive = false;
    this.promptName = randomUUID();
    this.contentName = randomUUID();
    this.audioContentName = randomUUID();

    /** @type {Function|null} — resolve callback for the input async iterable */
    this._inputResolve = null;
    /** @type {Array} — queued input events */
    this._inputQueue = [];
    /** @type {boolean} */
    this._inputDone = false;
    /** @type {AsyncIterable|null} — the output body from the SDK response */
    this._outputStream = null;

    this._sessionStartTime = null;
    // Nova Sonic session renewal threshold.
    //
    // When a session exceeds this age, the next tool-use / tool-done
    // cycle transparently replaces the prompt with a fresh one. The
    // cost of renewal is high: the system prompt (~6-8 k tokens) is
    // replayed, the model re-infers from scratch, and any in-flight
    // tool_use gets delayed until the new session is ready.
    //
    // Observed on 2026-05-13 live demo: Session A hit 450 s exactly
    // while the user had just asked Nova to navigate back to the
    // slideshow and explain the current slide. The ``analyze_slide``
    // tool call was caught in the renewal window and took 63 s
    // end-to-end (tool itself ran in <100 ms on the "notes path").
    // The 63 s of dead air was almost entirely Nova Sonic's internal
    // prompt replay.
    //
    // Bumping to 20 min covers any realistic live-demo session length
    // (typical demos run 5-15 min) so the renewal never fires mid-
    // presentation. Bedrock's own hard idle timeout is 55 s and the
    // session token ceiling is well above what a 20-min demo produces
    // (few hundred turns × 20-50 tokens each ≈ ~10 k tokens, an order
    // of magnitude under the cap), so this is safe from the upstream
    // perspective.
    //
    // If you're running a 20-min+ demo and see model-drift symptoms
    // (Nova forgetting earlier context), drop this back toward 10 min
    // — the tradeoff is exactly demo length vs. mid-demo hiccup risk.
    this._renewalThreshold = 20 * 60 * 1000; // 20 min in ms

    // Stored for session renewal
    this._systemPrompt = null;
    this._toolDefinitions = null;

    // P0-3: buffer for audio chunks that arrive during the Bedrock handshake
    // (before isActive = true). Without this, ~500-1000 ms of speech is
    // silently dropped every session start and reconnect.
    /** @type {string[]} base64 audio chunks queued before the stream was ready */
    this._pendingAudio = [];
    /** Hard cap — 16 kHz × 2 s / 1024 samples ≈ 32 chunks; keep slack. */
    this._pendingAudioMax = 64;

    // Option A additions
    /** Timestamp (Date.now()) of the last audioOutput event yielded from
     *  processResponses. waitForAudioIdle reads this to detect quiet
     *  gaps in the stream. 0 means no audio has been emitted yet. */
    this._lastAudioOutputAt = 0;
    /** Set by startSessionAudioOut. When true, sendAudioChunk refuses. */
    this._isAudioOutOnly = false;
    /** Interval handle for Session B's silent-audio pump. Null on Session A. */
    this._silentAudioTimer = null;
  }

  // ------------------------------------------------------------------ //
  // Input stream helpers
  // ------------------------------------------------------------------ //

  /**
   * Creates an async iterable that the SDK consumes as the input stream.
   * We push events into _inputQueue and signal the iterable via _inputResolve.
   */
  _createInputStream() {
    const self = this;
    return {
      [Symbol.asyncIterator]() {
        return {
          next() {
            if (self._inputQueue.length > 0) {
              return Promise.resolve({ value: self._inputQueue.shift(), done: false });
            }
            if (self._inputDone) {
              return Promise.resolve({ value: undefined, done: true });
            }
            return new Promise((resolve) => {
              self._inputResolve = () => {
                self._inputResolve = null;
                if (self._inputQueue.length > 0) {
                  resolve({ value: self._inputQueue.shift(), done: false });
                } else {
                  resolve({ value: undefined, done: true });
                }
              };
            });
          },
        };
      },
    };
  }

  /**
   * Enqueue a JSON event payload to be sent on the bidirectional stream.
   * @param {object} eventPayload
   */
  _enqueueEvent(eventPayload) {
    const chunk = {
      chunk: {
        bytes: Buffer.from(JSON.stringify(eventPayload)),
      },
    };
    this._inputQueue.push(chunk);
    if (this._inputResolve) {
      this._inputResolve();
    }
  }

  /**
   * Signal the input stream is done (no more events).
   */
  _closeInputStream() {
    this._inputDone = true;
    if (this._inputResolve) {
      this._inputResolve();
    }
  }

  // ------------------------------------------------------------------ //
  // Session start (Task 14.1)
  // ------------------------------------------------------------------ //

  /**
   * Open the bidirectional stream and send the full init sequence.
   *
   * Event order (MUST be exact):
   * 1. sessionStart (inferenceConfiguration: maxTokens, topP, temperature)
   * 2. promptStart (textOutputConfiguration, audioOutputConfiguration,
   *    toolUseOutputConfiguration, toolConfiguration)
   * 3. System prompt: contentStart (TEXT, SYSTEM, interactive:false) →
   *    textInput → contentEnd
   * 4. Audio input: contentStart (AUDIO, USER, interactive:true,
   *    audioInputConfiguration)
   *
   * @param {string} systemPrompt
   * @param {Array} toolDefinitions — array of {toolSpec: {name, description, inputSchema: {json: string}}}
   */
  async startSession(systemPrompt, toolDefinitions) {
    this._systemPrompt = systemPrompt;
    this._toolDefinitions = toolDefinitions;

    // Reset input stream state
    this._inputQueue = [];
    this._inputDone = false;
    this._inputResolve = null;

    // Generate fresh UUIDs
    this.promptName = randomUUID();
    this.contentName = randomUUID();
    this.audioContentName = randomUUID();

    const inputStream = this._createInputStream();

    // 1. sessionStart
    this._enqueueEvent({
      event: {
        sessionStart: {
          inferenceConfiguration: {
            maxTokens: 1024,
            topP: 0.9,
            temperature: 0.7,
          },
        },
      },
    });

    // 2. promptStart with tool and audio config
    const promptStartEvent = {
      event: {
        promptStart: {
          promptName: this.promptName,
          textOutputConfiguration: {
            mediaType: "text/plain",
          },
          audioOutputConfiguration: {
            mediaType: "audio/lpcm",
            sampleRateHertz: 24000,
            sampleSizeBits: 16,
            channelCount: 1,
            voiceId: this.voiceId,
            encoding: "base64",
            audioType: "SPEECH",
          },
        },
      },
    };

    if (toolDefinitions && toolDefinitions.length > 0) {
      promptStartEvent.event.promptStart.toolUseOutputConfiguration = {
        mediaType: "application/json",
      };
      // NOTE: Do NOT include toolChoice — it causes "Unable to parse input chunk" error
      promptStartEvent.event.promptStart.toolConfiguration = {
        tools: toolDefinitions,
      };
    }

    this._enqueueEvent(promptStartEvent);

    // 3. System prompt (3 events: contentStart → textInput → contentEnd)
    //    contentStart must use interactive: false for SYSTEM role
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName: this.contentName,
          type: "TEXT",
          interactive: false,
          role: "SYSTEM",
          textInputConfiguration: {
            mediaType: "text/plain",
          },
        },
      },
    });

    this._enqueueEvent({
      event: {
        textInput: {
          promptName: this.promptName,
          contentName: this.contentName,
          content: systemPrompt,
        },
      },
    });

    this._enqueueEvent({
      event: {
        contentEnd: {
          promptName: this.promptName,
          contentName: this.contentName,
        },
      },
    });

    // 4. Audio input: contentStart with interactive: true for USER role
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName: this.audioContentName,
          type: "AUDIO",
          interactive: true,
          role: "USER",
          audioInputConfiguration: {
            mediaType: "audio/lpcm",
            sampleRateHertz: 16000,
            sampleSizeBits: 16,
            channelCount: 1,
            audioType: "SPEECH",
            encoding: "base64",
          },
        },
      },
    });

    // Send the command with the async iterable input
    const command = new InvokeModelWithBidirectionalStreamCommand({
      modelId: this.modelId,
      body: inputStream,
    });

    const response = await this.client.send(command);
    this._outputStream = response.body;
    this.isActive = true;
    this._sessionStartTime = Date.now();

    // P0-3: drain any audio chunks that arrived during the Bedrock handshake.
    this._flushPendingAudio();

    console.log(
      `[nova-sonic] Session started (model=${this.modelId}, voice=${this.voiceId}, prompt=${this.promptName})`
    );
  }

  // ------------------------------------------------------------------ //
  // Audio input (Task 14.2)
  // ------------------------------------------------------------------ //

  /**
   * Forward browser audio into the Nova Sonic stream as an audioInput event.
   *
   * P0-3: if the session is still handshaking (isActive=false), buffer the
   * chunk instead of dropping it. The buffer is flushed from startSession()
   * once isActive flips true.
   *
   * @param {string} base64Audio — base64-encoded PCM 16-bit mono 16kHz audio
   */
  sendAudioChunk(base64Audio) {
    this._throwIfAudioOutOnly();
    if (!this.isActive) {
      if (this._pendingAudio.length < this._pendingAudioMax) {
        this._pendingAudio.push(base64Audio);
      } else {
        // Prevent unbounded growth if startup stalls — drop oldest chunk.
        this._pendingAudio.shift();
        this._pendingAudio.push(base64Audio);
      }
      return;
    }

    this._enqueueEvent({
      event: {
        audioInput: {
          promptName: this.promptName,
          contentName: this.audioContentName,
          content: base64Audio,
        },
      },
    });
  }

  /**
   * Drain any audio chunks that were buffered while isActive was false.
   * Called by startSession / renewSession immediately after isActive=true.
   * @private
   */
  _flushPendingAudio() {
    if (this._pendingAudio.length === 0) return;
    const count = this._pendingAudio.length;
    for (const base64Audio of this._pendingAudio) {
      this._enqueueEvent({
        event: {
          audioInput: {
            promptName: this.promptName,
            contentName: this.audioContentName,
            content: base64Audio,
          },
        },
      });
    }
    this._pendingAudio = [];
    console.log(`[nova-sonic] Flushed ${count} buffered audio chunk(s) after handshake`);
  }

  // ------------------------------------------------------------------ //
  // Response processing (Task 14.3)
  // ------------------------------------------------------------------ //

  /**
   * Async generator that reads the Nova Sonic output stream and yields
   * typed events:
   *   { type: "audio", data: string }        — base64 audio output (24kHz)
   *   { type: "tool_use", toolUseId, toolName, toolInput }
   *   { type: "text", text: string }
   *   { type: "session_end" }
   *
   * @yields {NovaSonicEvent}
   */
  async *processResponses() {
    if (!this._outputStream) {
      return;
    }

    try {
      for await (const item of this._outputStream) {
        if (!this.isActive) break;

        // The SDK wraps output in a chunk with bytes
        const bytes = item?.chunk?.bytes;
        if (!bytes) continue;

        let jsonData;
        try {
          const text = typeof bytes === "string" ? bytes : Buffer.from(bytes).toString("utf-8");
          jsonData = JSON.parse(text);
        } catch {
          continue;
        }

        const event = jsonData.event || {};

        // Audio output
        if (event.audioOutput) {
          const audioB64 = event.audioOutput.content;
          if (audioB64) {
            this._lastAudioOutputAt = Date.now();
            yield { type: "audio", data: audioB64 };
          }
        }
        // Tool use
        else if (event.toolUse) {
          const toolData = event.toolUse;
          let toolInput;
          try {
            toolInput =
              typeof toolData.content === "string"
                ? JSON.parse(toolData.content)
                : toolData.content || {};
          } catch {
            toolInput = { raw: toolData.content };
          }
          yield {
            type: "tool_use",
            toolUseId: toolData.toolUseId || "",
            toolName: toolData.toolName || "",
            toolInput,
          };
        }
        // Text output
        else if (event.textOutput) {
          const text = event.textOutput.content;
          if (text) {
            yield { type: "text", text };
          }
        }
        // Session / completion end
        else if (event.completionEnd) {
          yield { type: "session_end" };
        }
        // Any other event type — log at DEBUG level so we can see what
        // Bedrock is actually sending (useful for diagnosing "silent
        // Session B" issues). Uses _isAudioOutOnly as a proxy for Session B.
        else {
          const eventKey = Object.keys(event)[0];
          if (eventKey && eventKey !== "completionStart") {
            // completionStart is noisy and always present — skip it.
            console.log(
              `[nova-sonic] ${this._isAudioOutOnly ? "B" : "A"} event=${eventKey}`,
              JSON.stringify(event[eventKey]).slice(0, 200),
            );
          }
        }
      }
    } catch (err) {
      // Unexpected stream disconnection — dump full error context so
      // postmortems can identify the exact failure mode. Historically
      // "Invalid input request" from Bedrock could mean: (a) malformed
      // event order, (b) barge-in race sending audio to a closed
      // prompt, (c) system prompt over size, (d) stale toolUseId.
      const errName = err?.name || err?.constructor?.name || "Unknown";
      const errStack = err?.stack ? err.stack.split("\n").slice(0, 3).join(" | ") : "";
      const fault = err?.$fault || err?.fault || null;
      const metadata = err?.$metadata ? JSON.stringify(err.$metadata) : null;
      console.error(
        "[nova-sonic] Stream error (%s): %s%s%s%s",
        errName,
        err.message || String(err),
        fault ? ` fault=${fault}` : "",
        metadata ? ` meta=${metadata}` : "",
        errStack ? ` stack=${errStack}` : "",
      );
      this.isActive = false;
      // Emit a dedicated error event BEFORE session_end so session
      // manager can distinguish abnormal death from clean shutdown.
      yield {
        type: "stream_error",
        message: err.message || String(err),
        errorName: errName,
      };
      yield { type: "session_end" };
    }
  }

  // ------------------------------------------------------------------ //
  // Tool result (Task 14.4)
  // ------------------------------------------------------------------ //

  /**
   * Send a tool result back to Nova Sonic.
   *
   * Requires exactly 3 events:
   * 1. contentStart (type TOOL, role TOOL, toolResultInputConfiguration with toolUseId)
   * 2. toolResult (the actual result content)
   * 3. contentEnd
   *
   * @param {string} toolUseId
   * @param {object|string} resultJson
   */
  sendToolResult(toolUseId, resultJson) {
    if (!this.isActive) {
      console.warn(`[nova-sonic] sendToolResult called on inactive session (toolUseId=${toolUseId})`);
      return;
    }

    const toolContentName = randomUUID();
    const resultStr = typeof resultJson === "string" ? resultJson : JSON.stringify(resultJson);

    // 1. contentStart for tool result
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName: toolContentName,
          interactive: false,
          type: "TOOL",
          role: "TOOL",
          toolResultInputConfiguration: {
            toolUseId,
            type: "TEXT",
            textInputConfiguration: {
              mediaType: "text/plain",
            },
          },
        },
      },
    });

    // 2. toolResult with the actual content
    this._enqueueEvent({
      event: {
        toolResult: {
          promptName: this.promptName,
          contentName: toolContentName,
          content: resultStr,
        },
      },
    });

    // 3. contentEnd
    this._enqueueEvent({
      event: {
        contentEnd: {
          promptName: this.promptName,
          contentName: toolContentName,
        },
      },
    });
  }

  // ------------------------------------------------------------------ //
  // Session end (Task 14.5)
  // ------------------------------------------------------------------ //

  /**
   * Clean shutdown of the Nova Sonic stream.
   * Sends: contentEnd (audio) → promptEnd → sessionEnd → close input.
   */
  endSession() {
    if (!this.isActive && !this._outputStream) {
      return;
    }

    // Stop the silent-audio pump FIRST (Session B) so no more audioInput
    // events race the contentEnd below.
    this._stopSilentAudioPump();

    try {
      // 1. Close audio input content block
      this._enqueueEvent({
        event: {
          contentEnd: {
            promptName: this.promptName,
            contentName: this.audioContentName,
          },
        },
      });

      // 2. End the prompt
      this._enqueueEvent({
        event: {
          promptEnd: {
            promptName: this.promptName,
          },
        },
      });

      // 3. End the session
      this._enqueueEvent({
        event: {
          sessionEnd: {},
        },
      });

      // 4. Close the input stream
      this._closeInputStream();
    } catch (err) {
      console.error("[nova-sonic] Error during session shutdown:", err.message);
    } finally {
      this.isActive = false;
      this._outputStream = null;
      this._sessionStartTime = null;
      this._pendingAudio = []; // P0-3: drop any audio queued while inactive
      console.log(`[nova-sonic] Session ended (prompt=${this.promptName})`);
    }
  }

  // ------------------------------------------------------------------ //
  // Session renewal (Task 14.6)
  // ------------------------------------------------------------------ //

  /**
   * Check whether the current stream is approaching the 8-minute limit.
   * @returns {boolean}
   */
  needsRenewal() {
    if (!this._sessionStartTime) return false;
    return (Date.now() - this._sessionStartTime) > this._renewalThreshold;
  }

  /**
   * Replace the current stream with a fresh one near the 8-minute limit.
   *
   * Opens a new bidirectional stream, re-sends the full init sequence,
   * then swaps the output stream reference. The WebSocket connection to
   * the browser is preserved across renewals.
   */
  async renewSession() {
    if (!this.needsRenewal()) return;
    if (!this._systemPrompt || !this._toolDefinitions) return;

    console.log(
      `[nova-sonic] Session renewal triggered (elapsed=${((Date.now() - this._sessionStartTime) / 1000).toFixed(1)}s)`
    );

    const oldOutputStream = this._outputStream;

    // Reset input stream state for the new stream
    this._inputQueue = [];
    this._inputDone = false;
    this._inputResolve = null;

    // Generate fresh UUIDs
    this.promptName = randomUUID();
    this.contentName = randomUUID();
    this.audioContentName = randomUUID();

    try {
      const inputStream = this._createInputStream();

      // Re-send full init sequence
      // 1. sessionStart
      this._enqueueEvent({
        event: {
          sessionStart: {
            inferenceConfiguration: {
              maxTokens: 1024,
              topP: 0.9,
              temperature: 0.7,
            },
          },
        },
      });

      // 2. promptStart
      const promptStartEvent = {
        event: {
          promptStart: {
            promptName: this.promptName,
            textOutputConfiguration: { mediaType: "text/plain" },
            audioOutputConfiguration: {
              mediaType: "audio/lpcm",
              sampleRateHertz: 24000,
              sampleSizeBits: 16,
              channelCount: 1,
              voiceId: this.voiceId,
              encoding: "base64",
              audioType: "SPEECH",
            },
            toolUseOutputConfiguration: { mediaType: "application/json" },
            toolConfiguration: { tools: this._toolDefinitions },
          },
        },
      };
      this._enqueueEvent(promptStartEvent);

      // 3. System prompt
      this._enqueueEvent({
        event: {
          contentStart: {
            promptName: this.promptName,
            contentName: this.contentName,
            type: "TEXT",
            interactive: false,
            role: "SYSTEM",
            textInputConfiguration: { mediaType: "text/plain" },
          },
        },
      });
      this._enqueueEvent({
        event: {
          textInput: {
            promptName: this.promptName,
            contentName: this.contentName,
            content: this._systemPrompt,
          },
        },
      });
      this._enqueueEvent({
        event: {
          contentEnd: {
            promptName: this.promptName,
            contentName: this.contentName,
          },
        },
      });

      // 4. Audio input start
      this._enqueueEvent({
        event: {
          contentStart: {
            promptName: this.promptName,
            contentName: this.audioContentName,
            type: "AUDIO",
            interactive: true,
            role: "USER",
            audioInputConfiguration: {
              mediaType: "audio/lpcm",
              sampleRateHertz: 16000,
              sampleSizeBits: 16,
              channelCount: 1,
              audioType: "SPEECH",
              encoding: "base64",
            },
          },
        },
      });

      // Open new stream
      const command = new InvokeModelWithBidirectionalStreamCommand({
        modelId: this.modelId,
        body: inputStream,
      });

      const response = await this.client.send(command);
      this._outputStream = response.body;
      this._sessionStartTime = Date.now();

      // P0-3: drain any audio chunks that arrived during renewal handshake.
      this._flushPendingAudio();

      console.log(`[nova-sonic] Session renewed successfully (new prompt=${this.promptName})`);
    } catch (err) {
      // Renewal failed — restore old stream if still usable
      console.error("[nova-sonic] Session renewal failed:", err.message);
      if (oldOutputStream) {
        this._outputStream = oldOutputStream;
      } else {
        this.isActive = false;
        this._outputStream = null;
      }
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // Option A additions — used by the NovaSonicSessionManager:
  //   - startSessionAudioOut : Session B variant (no audio input)
  //   - waitForAudioIdle     : debounced "no audio for X ms" wait
  //   - sendSystemTextInput  : out-of-band SYSTEM TEXT triplet
  // ══════════════════════════════════════════════════════════════════

  /**
   * Open Session B — a genuine voice session (audio IN + audio OUT) where:
   *   - The audio IN is **silence** (we never send real microphone audio).
   *   - The query is injected as **cross-modal text input** (interactive:true,
   *     TEXT, USER) after the audio block is opened.
   *
   * Why? Nova Sonic's bidirectional stream does NOT support pure text-only
   * input. The `interactive: true` flag on TEXT is specifically for
   * "cross-modal input, allowing text messages during an active voice
   * session" (per AWS docs). Without an open AUDIO block, Bedrock raises
   * InternalErrorCode=532 "Timed out waiting for audio bytes or interactive
   * content" after ~55 s.
   *
   * The silent-audio pump emits a small amount of zero-valued PCM every
   * ~100 ms so the audio block has real content (avoids "Cannot end content
   * as no content data was received" on shutdown) and the stream stays
   * interactive. We disable VAD endpointing so Nova Sonic never thinks the
   * user is "done speaking" (which would make it generate filler).
   *
   * @param {object} opts
   * @param {string} opts.systemPrompt
   * @param {Array}  opts.toolDefinitions  Session B's tool_defs[].
   * @param {string} [opts.initialUserTextInput]  Optional text seed.
   */
  async startSessionAudioOut({ systemPrompt, toolDefinitions, initialUserTextInput } = {}) {
    if (!systemPrompt) throw new Error("startSessionAudioOut: systemPrompt is required");

    this._systemPrompt = systemPrompt;
    this._toolDefinitions = toolDefinitions;
    this._isAudioOutOnly = true;

    // Fresh input-stream state.
    this._inputQueue = [];
    this._inputDone = false;
    this._inputResolve = null;
    this._lastAudioOutputAt = 0;

    this.promptName = randomUUID();
    this.contentName = randomUUID();
    // Reused as the AUDIO content block (silent-audio pump writes to this).
    this.audioContentName = randomUUID();
    const initialUserContentName = randomUUID();

    const inputStream = this._createInputStream();

    // 1. sessionStart — disable endpointing so Nova Sonic never interprets
    //    silence as "user finished speaking" (which would trigger premature
    //    responses/interruptions). The cross-modal text input is what drives
    //    generation, not VAD.
    this._enqueueEvent({
      event: {
        sessionStart: {
          inferenceConfiguration: {
            maxTokens: 1024, topP: 0.9, temperature: 0.7,
          },
        },
      },
    });

    // 2. promptStart — text + audio output config + tools.
    const promptStart = {
      event: {
        promptStart: {
          promptName: this.promptName,
          textOutputConfiguration: { mediaType: "text/plain" },
          audioOutputConfiguration: {
            mediaType: "audio/lpcm",
            sampleRateHertz: 24000,
            sampleSizeBits: 16,
            channelCount: 1,
            voiceId: this.voiceId,
            encoding: "base64",
            audioType: "SPEECH",
          },
        },
      },
    };
    if (toolDefinitions && toolDefinitions.length > 0) {
      promptStart.event.promptStart.toolUseOutputConfiguration = {
        mediaType: "application/json",
      };
      promptStart.event.promptStart.toolConfiguration = { tools: toolDefinitions };
    }
    this._enqueueEvent(promptStart);

    // 3. System prompt triplet (interactive:false).
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName: this.contentName,
          type: "TEXT",
          interactive: false,
          role: "SYSTEM",
          textInputConfiguration: { mediaType: "text/plain" },
        },
      },
    });
    this._enqueueEvent({
      event: {
        textInput: {
          promptName: this.promptName,
          contentName: this.contentName,
          content: systemPrompt,
        },
      },
    });
    this._enqueueEvent({
      event: {
        contentEnd: {
          promptName: this.promptName,
          contentName: this.contentName,
        },
      },
    });

    // 4. AUDIO contentStart — opens the voice session. The silent-audio pump
    //    (below) keeps the block alive with zero-valued PCM frames.
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName: this.audioContentName,
          type: "AUDIO",
          interactive: true,
          role: "USER",
          audioInputConfiguration: {
            mediaType: "audio/lpcm",
            sampleRateHertz: 16000,
            sampleSizeBits: 16,
            channelCount: 1,
            audioType: "SPEECH",
            encoding: "base64",
          },
        },
      },
    });

    // 5. Cross-modal TEXT input (interactive:true, USER) — this is the
    //    query. Per AWS Nova Sonic docs, interactive:true on TEXT is
    //    specifically for "cross-modal input, allowing text messages during
    //    an active voice session." It's sent AFTER the audio block opens
    //    (step 4) so it can be "cross-modal."
    if (initialUserTextInput) {
      this._enqueueEvent({
        event: {
          contentStart: {
            promptName: this.promptName,
            contentName: initialUserContentName,
            type: "TEXT",
            interactive: true,
            role: "USER",
            textInputConfiguration: { mediaType: "text/plain" },
          },
        },
      });
      this._enqueueEvent({
        event: {
          textInput: {
            promptName: this.promptName,
            contentName: initialUserContentName,
            content: initialUserTextInput,
          },
        },
      });
      this._enqueueEvent({
        event: {
          contentEnd: {
            promptName: this.promptName,
            contentName: initialUserContentName,
          },
        },
      });
    }

    const command = new InvokeModelWithBidirectionalStreamCommand({
      modelId: this.modelId,
      body: inputStream,
    });
    const response = await this.client.send(command);
    this._outputStream = response.body;
    this.isActive = true;
    this._sessionStartTime = Date.now();

    // 6. Start the silent-audio pump — emits zero-valued PCM every ~100ms
    //    so the AUDIO block stays alive and Bedrock treats this as an
    //    active voice session. Runs until isActive=false (endSession).
    this._startSilentAudioPump();

    console.log(
      `[nova-sonic] Session B audio-OUT started (voice=${this.voiceId}, prompt=${this.promptName})`
    );
  }

  /**
   * Start pushing silent PCM frames into the AUDIO content block at ~100ms
   * cadence. Runs until isActive is false. Session B uses this to keep the
   * bidirectional stream's audio block alive without sending real microphone
   * audio.
   *
   * Frame size: 100ms @ 16kHz, 16-bit, mono = 3200 bytes of zeros.
   */
  _startSilentAudioPump() {
    if (this._silentAudioTimer) return;
    // 100ms frame = 1600 samples × 2 bytes = 3200 bytes of zeros.
    const SILENT_FRAME_BYTES = 3200;
    const silentFrame = Buffer.alloc(SILENT_FRAME_BYTES).toString("base64");

    const tick = () => {
      if (!this.isActive || !this._isAudioOutOnly) {
        clearInterval(this._silentAudioTimer);
        this._silentAudioTimer = null;
        return;
      }
      try {
        this._enqueueEvent({
          event: {
            audioInput: {
              promptName: this.promptName,
              contentName: this.audioContentName,
              content: silentFrame,
            },
          },
        });
      } catch (err) {
        console.error("[nova-sonic] silent-audio pump error:", err.message);
      }
    };
    this._silentAudioTimer = setInterval(tick, 100);
    // Don't block Node.js process exit on the timer.
    if (this._silentAudioTimer.unref) this._silentAudioTimer.unref();
  }

  _stopSilentAudioPump() {
    if (this._silentAudioTimer) {
      clearInterval(this._silentAudioTimer);
      this._silentAudioTimer = null;
    }
  }

  /**
   * Reject any attempt to send audio to a Session B (audio-OUT only) client.
   * Session A retains its original sendAudioChunk implementation (above).
   */
  _throwIfAudioOutOnly() {
    if (this._isAudioOutOnly) {
      throw new Error(
        "sendAudioChunk: this Nova Sonic session has no audio input (Session B)"
      );
    }
  }

  /**
   * Resolve when no audioOutput event has arrived for `debounceMs` ms,
   * or after `timeoutMs` ms — whichever comes first.
   *
   * Used by the NovaSonicSessionManager to wait for Session A's handoff
   * line to finish playing before flipping the audio mux to Session B.
   *
   * @param {object} [opts]
   * @param {number} [opts.debounceMs=150]
   * @param {number} [opts.timeoutMs=2500]
   * @returns {Promise<{reason: 'idle'|'timeout'|'inactive'}>}
   */
  async waitForAudioIdle({ debounceMs = 150, timeoutMs = 2500 } = {}) {
    if (!this.isActive) return { reason: "inactive" };

    const deadline = Date.now() + timeoutMs;
    // Poll with small sleeps. Cheap and accurate to ~10 ms.
    while (Date.now() < deadline) {
      const sinceLast = Date.now() - (this._lastAudioOutputAt || 0);
      if (this._lastAudioOutputAt === 0 || sinceLast >= debounceMs) {
        return { reason: "idle" };
      }
      const wait = Math.max(10, Math.min(debounceMs - sinceLast + 5, 50));
      await new Promise((r) => setTimeout(r, wait));
    }
    return { reason: "timeout" };
  }

  /**
   * Send an out-of-band "system-style" TEXT message into an open session.
   *
   * Used by the session manager for HANDBACK_NOTICE hints ("Carlos
   * finished — stay silent until the presenter speaks") and for
   * HANDOFF_FAILED announcements.
   *
   * ⚠️  Nova Sonic protocol: exactly ONE `role:SYSTEM` content block is
   * allowed per prompt. Sending a second one triggers the stream error
   * "Duplicate SYSTEM content. SYSTEM content can only be provided once
   * per prompt." and kills the bidirectional stream. So we wrap the
   * notice as a `role:USER`, `interactive:false` TEXT turn with a
   * `[SYSTEM_NOTICE] ` prefix that Session A's prompt teaches it to
   * recognise as an out-of-band directive (rather than a user utterance).
   *
   * @param {string} text
   */
  sendSystemTextInput(text) {
    if (!this.isActive) {
      console.warn("[nova-sonic] sendSystemTextInput called on inactive session");
      return;
    }
    if (!text) return;

    const contentName = randomUUID();
    this._enqueueEvent({
      event: {
        contentStart: {
          promptName: this.promptName,
          contentName,
          type: "TEXT",
          interactive: false,
          // role: USER (not SYSTEM) — see note above.
          role: "USER",
          textInputConfiguration: { mediaType: "text/plain" },
        },
      },
    });
    this._enqueueEvent({
      event: {
        textInput: {
          promptName: this.promptName,
          contentName,
          // Prefix marks this as a directive, not a user utterance.
          // Session A's system prompt should explain how to react.
          content: `[SYSTEM_NOTICE] ${String(text)}`,
        },
      },
    });
    this._enqueueEvent({
      event: {
        contentEnd: {
          promptName: this.promptName,
          contentName,
        },
      },
    });
  }
}
