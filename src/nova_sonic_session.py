"""NovaSonicSession — bidirectional voice streaming with Amazon Nova 2 Sonic.

Manages the full lifecycle of a Nova Sonic session: opening the
bidirectional stream, sending the required event sequence (sessionStart →
promptStart → system prompt → audio input config), streaming PCM audio in
both directions, routing tool calls, and handling session renewal for the
8-minute connection limit.

The SDK used is ``aws_sdk_bedrock_runtime`` (NOT boto3) because boto3 does
not support bidirectional streaming.  All Bedrock calls authenticate via
SigV4 through :class:`EnvironmentCredentialsResolver` — no hardcoded keys
(Requirements 9.1, 9.4).

Session renewal (task 6.8 / Property 13):
    Nova 2 Sonic enforces an 8-minute maximum connection duration.  When the
    session approaches ~7 min 30 s the :meth:`_renew_session` helper opens a
    fresh stream in parallel, re-sends the init events, and swaps the stream
    reference.  The :class:`SlideStore` lives outside the session and is
    unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import (
    Config,
    HTTPAuthSchemeResolver,
    SigV4AuthScheme,
)
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
try:
    from smithy_aws_core.credentials_resolvers.environment import EnvironmentCredentialsResolver
except ImportError:
    from smithy_aws_core.identity import EnvironmentCredentialsResolver

from src.models import SessionConfig

logger = logging.getLogger(__name__)

# Type alias for events yielded by process_responses().
SessionEvent = Tuple[str, Any]


class NovaSonicSession:
    """Manages a bidirectional voice-streaming session with Nova 2 Sonic.

    Attributes:
        config: Session configuration (voice, locale, model IDs).
        region: AWS region for the Bedrock endpoint.
        client: The ``BedrockRuntimeClient`` (created lazily).
        stream: The active bidirectional stream handle.
        is_active: ``True`` while the stream is open and usable.
        prompt_name: UUID identifying the current prompt.
        content_name: UUID for the system-prompt content block.
        audio_content_name: UUID for the audio-input content block.
    """

    def __init__(self, config: SessionConfig, region: str = "us-east-1") -> None:
        self.config = config
        self.region = region
        self.client: Optional[BedrockRuntimeClient] = None
        self.stream: Any = None
        self.is_active: bool = False
        self.barge_in: bool = False
        self.prompt_name: str = str(uuid.uuid4())
        self.content_name: str = str(uuid.uuid4())
        self.audio_content_name: str = str(uuid.uuid4())
        self._session_start_time: Optional[float] = None
        self._renewal_threshold: float = 7.5 * 60  # 7 min 30 s
        # Stored for session renewal so we can re-send init events.
        self._system_prompt: Optional[str] = None
        self._tool_definition: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # Client initialisation
    # ------------------------------------------------------------------ #

    def _initialize_client(self) -> BedrockRuntimeClient:
        """Create a ``BedrockRuntimeClient`` with SigV4 auth over HTTPS.

        Uses :class:`EnvironmentCredentialsResolver` so credentials come from
        environment variables or the default chain — never hardcoded
        (Requirements 9.1, 9.4).
        """
        # Try both SDK versions' Config signatures.
        try:
            config = Config(
                endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
                region=self.region,
                aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            )
        except TypeError:
            config = Config(
                endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
                region=self.region,
                aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
                auth_scheme_resolver=HTTPAuthSchemeResolver(),
                auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")},
            )
        return BedrockRuntimeClient(config=config)

    # ------------------------------------------------------------------ #
    # Low-level event helpers
    # ------------------------------------------------------------------ #

    async def send_event(self, event_json: str) -> None:
        """Send a JSON-encoded event to the bidirectional stream.

        Args:
            event_json: A JSON string conforming to the Nova Sonic input
                event protocol.

        Raises:
            RuntimeError: If the stream is not open.
        """
        if self.stream is None:
            raise RuntimeError("NovaSonicSession.send_event: stream is not open")
        logger.debug("Sending event: %s", event_json[:200])
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )
        await self.stream.input_stream.send(event)

    # ------------------------------------------------------------------ #
    # Session start (task 6.2)
    # ------------------------------------------------------------------ #

    async def start_session(
        self, system_prompt: str, tool_definition: dict
    ) -> None:
        """Open the bidirectional stream and send the full init sequence.

        The event order is mandated by the Nova Sonic protocol:

        1. Open stream (``invoke_model_with_bidirectional_stream``)
        2. ``sessionStart`` with inference configuration
        3. ``promptStart`` with text/audio/tool output configs and tool config
        4. System prompt: ``contentStart`` (TEXT, SYSTEM) → ``textInput`` →
           ``contentEnd``
        5. Audio input start: ``contentStart`` (AUDIO, USER) with 16 kHz config

        Args:
            system_prompt: The system-level instructions for Nova Sonic.
            tool_definition: A dict describing the ``analyze_slide`` tool
                (name, description, inputSchema).
        """
        # Persist for renewal.
        self._system_prompt = system_prompt
        self._tool_definition = tool_definition

        if self.client is None:
            self.client = self._initialize_client()

        # 1. Open bidirectional stream.
        model_id = self.config.sonic_model_id
        self.stream = await self.client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
        )
        self.is_active = True
        self._session_start_time = time.time()

        # Generate fresh UUIDs for this stream.
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        # 2. sessionStart
        await self.send_event(json.dumps({
            "event": {
                "sessionStart": {
                    "inferenceConfiguration": {
                        "maxTokens": 1024,
                        "topP": 0.9,
                        "temperature": 0.7,
                    },
                }
            }
        }))

        # 3. promptStart — use tool config only if provided
        prompt_start: dict = {
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": {
                        "mediaType": "text/plain"
                    },
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": self.config.voice_id,
                        "encoding": "base64",
                        "audioType": "SPEECH"
                    }
                }
            }
        }
        if tool_definition:
            prompt_start["event"]["promptStart"]["toolUseOutputConfiguration"] = {
                "mediaType": "application/json"
            }
            prompt_start["event"]["promptStart"]["toolConfiguration"] = {
                "tools": tool_definition
            }
        await self.send_event(json.dumps(prompt_start))

        # 4. System prompt (3 events: contentStart → textInput → contentEnd)
        await self.send_event(json.dumps({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                    "type": "TEXT",
                    "interactive": False,
                    "role": "SYSTEM",
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                }
            }
        }))

        await self.send_event(json.dumps({
            "event": {
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                    "content": system_prompt,
                }
            }
        }))

        await self.send_event(json.dumps({
            "event": {
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": self.content_name,
                }
            }
        }))

        # NOTE: Audio input start is NOT sent here — call start_audio_input()
        # separately before sending audio chunks (matches AWS sample pattern).

        logger.info(
            "Nova Sonic session started (model=%s, voice=%s, prompt=%s)",
            model_id,
            self.config.voice_id,
            self.prompt_name,
        )

    async def start_audio_input(self) -> None:
        """Send the audio contentStart event to begin audio streaming.

        Must be called after start_session() and before send_audio_chunk().
        """
        await self.send_event(json.dumps({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 16000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            }
        }))

    # ------------------------------------------------------------------ #
    # Audio input (task 6.3)
    # ------------------------------------------------------------------ #

    async def send_audio_chunk(self, audio_bytes: bytes) -> None:
        """Stream a chunk of PCM 16-bit mono 16 kHz audio to Nova Sonic.

        The raw bytes are base64-encoded and wrapped in an ``audioInput``
        event before being sent to the stream.

        Args:
            audio_bytes: Raw PCM audio data (16-bit, mono, 16 kHz).

        Raises:
            RuntimeError: If the session is not active.
        """
        if not self.is_active:
            raise RuntimeError(
                "NovaSonicSession.send_audio_chunk: session is not active"
            )

        encoded = base64.b64encode(audio_bytes).decode("ascii")
        await self.send_event(json.dumps({
            "event": {
                "audioInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "content": encoded,
                }
            }
        }))

    # ------------------------------------------------------------------ #
    # Response processing (task 6.4)
    # ------------------------------------------------------------------ #

    async def process_responses(self) -> AsyncGenerator[SessionEvent, None]:
        """Async generator yielding typed events from the Nova Sonic stream.

        Yields tuples of ``(event_type, payload)``:

        * ``("audio", bytes)`` — decoded PCM audio at 24 kHz.
        * ``("tool_use", {"tool_use_id": str, "tool_name": str,
          "content": dict, "content_id": str})`` — a tool invocation request.
        * ``("text", str)`` — text output from the model.
        * ``("session_end", None)`` — the stream has closed normally.

        On any unexpected exception the session is marked inactive, the error
        is logged, and resources are cleaned up (task 6.7).
        """
        if self.stream is None:
            return

        try:
            while self.is_active:
                output = await self.stream.await_output()
                result = await output[1].receive()
                if not (result.value and result.value.bytes_):
                    continue

                response_data = result.value.bytes_.decode("utf-8")
                try:
                    json_data = json.loads(response_data)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON response from stream: %s", response_data[:200])
                    continue

                event = json_data.get("event", {})

                # --- Audio output ---
                if "audioOutput" in event:
                    audio_b64 = event["audioOutput"].get("content", "")
                    if audio_b64:
                        yield ("audio", base64.b64decode(audio_b64))

                # --- Tool use ---
                elif "toolUse" in event:
                    tool_data = event["toolUse"]
                    content_raw = tool_data.get("content", "{}")
                    try:
                        content_parsed = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                    except json.JSONDecodeError:
                        content_parsed = {"raw": content_raw}
                    yield ("tool_use", {
                        "tool_use_id": tool_data.get("toolUseId", ""),
                        "tool_name": tool_data.get("toolName", ""),
                        "content": content_parsed,
                        "content_id": tool_data.get("contentId", ""),
                    })

                # --- Text output ---
                elif "textOutput" in event:
                    text = event["textOutput"].get("content", "")
                    if text:
                        # Detect barge-in (user interrupted the assistant)
                        if '{ "interrupted" : true }' in text:
                            self.barge_in = True
                            logger.info("Barge-in detected — stopping audio output")
                        else:
                            yield ("text", text)

                # --- Completion / session end ---
                elif "completionEnd" in event:
                    yield ("session_end", None)

        except asyncio.CancelledError:
            logger.info("Response processing cancelled")
            raise
        except Exception as exc:
            # Task 6.7: unexpected disconnection handling.
            logger.error("Stream disconnection or error: %s", exc, exc_info=True)
            self.is_active = False
            await self._cleanup_stream()
            yield ("session_end", None)

    # ------------------------------------------------------------------ #
    # Tool result (task 6.5)
    # ------------------------------------------------------------------ #

    async def send_tool_result(self, tool_use_id: str, result: dict) -> None:
        """Send a tool result back to the Nova Sonic stream.

        Sends three events matching the AWS sample pattern:
        1. contentStart (type TOOL, role TOOL, with toolResultInputConfiguration)
        2. toolResult (the actual result content)
        3. contentEnd

        Args:
            tool_use_id: The ``toolUseId`` from the ``toolUse`` event.
            result: A JSON-serialisable dict with the tool output.
        """
        if not self.is_active:
            logger.warning(
                "send_tool_result called on inactive session (tool_use_id=%s)",
                tool_use_id,
            )
            return

        # Generate a unique content name for this tool response.
        tool_content_name = str(uuid.uuid4())

        # 1. contentStart for tool result
        await self.send_event(json.dumps({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": tool_content_name,
                    "interactive": False,
                    "type": "TOOL",
                    "role": "TOOL",
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {
                            "mediaType": "text/plain"
                        }
                    }
                }
            }
        }))

        # 2. toolResult with the actual content
        await self.send_event(json.dumps({
            "event": {
                "toolResult": {
                    "promptName": self.prompt_name,
                    "contentName": tool_content_name,
                    "content": json.dumps(result),
                }
            }
        }))

        # 3. contentEnd
        await self.send_event(json.dumps({
            "event": {
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": tool_content_name,
                }
            }
        }))

    # ------------------------------------------------------------------ #
    # Session end (task 6.6)
    # ------------------------------------------------------------------ #

    async def end_session(self) -> None:
        """Cleanly shut down the bidirectional stream.

        Sends the required closing events in order:

        1. ``contentEnd`` for the audio input content block
        2. ``promptEnd`` with the prompt name
        3. ``sessionEnd``
        4. Close the input stream

        Sets ``is_active`` to ``False`` regardless of success.
        """
        if not self.is_active and self.stream is None:
            return

        try:
            if self.stream is not None:
                # 1. Close audio input content block.
                await self.send_event(json.dumps({
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                        }
                    }
                }))

                # 2. End the prompt.
                await self.send_event(json.dumps({
                    "event": {
                        "promptEnd": {
                            "promptName": self.prompt_name,
                        }
                    }
                }))

                # 3. End the session.
                await self.send_event(json.dumps({
                    "event": {
                        "sessionEnd": {}
                    }
                }))

                # 4. Close the input stream.
                await self.stream.input_stream.close()

        except Exception as exc:
            logger.error("Error during session shutdown: %s", exc, exc_info=True)
        finally:
            self.is_active = False
            self.stream = None
            self._session_start_time = None
            logger.info("Nova Sonic session ended (prompt=%s)", self.prompt_name)

    # ------------------------------------------------------------------ #
    # Stream cleanup helper (task 6.7)
    # ------------------------------------------------------------------ #

    async def _cleanup_stream(self) -> None:
        """Best-effort cleanup of the stream after an unexpected error.

        Called internally when the response processor catches an exception.
        Does not raise — any secondary errors are logged and swallowed.
        """
        try:
            if self.stream is not None:
                await self.stream.input_stream.close()
        except Exception as exc:
            logger.debug("Ignoring error during stream cleanup: %s", exc)
        finally:
            self.stream = None
            self._session_start_time = None

    # ------------------------------------------------------------------ #
    # Session renewal (task 6.8)
    # ------------------------------------------------------------------ #

    def _needs_renewal(self) -> bool:
        """Check whether the current stream is approaching the 8-min limit.

        Returns:
            ``True`` if the session has been active longer than
            ``_renewal_threshold`` (default 7 min 30 s).
        """
        if self._session_start_time is None:
            return False
        return (time.time() - self._session_start_time) > self._renewal_threshold

    async def _renew_session(
        self,
        system_prompt: str,
        tool_definition: dict,
    ) -> None:
        """Replace the current stream with a fresh one near the 8-min limit.

        Opens a new bidirectional stream, re-sends the full init sequence
        (sessionStart → promptStart → system prompt → audio input config),
        then swaps ``self.stream`` to the new stream.

        The :class:`SlideStore` is external and completely unaffected by
        renewal (Property 13).

        Args:
            system_prompt: The system prompt to re-send.
            tool_definition: The tool definition to re-register.
        """
        if not self._needs_renewal():
            return

        logger.info(
            "Session renewal triggered (elapsed=%.1fs, threshold=%.1fs)",
            time.time() - (self._session_start_time or 0),
            self._renewal_threshold,
        )

        old_stream = self.stream

        # Generate fresh identifiers for the new stream.
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        try:
            # Open new stream.
            model_id = self.config.sonic_model_id
            self.stream = await self.client.invoke_model_with_bidirectional_stream(
                InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
            )
            self._session_start_time = time.time()

            # Re-send full init sequence on the new stream.
            # 1. sessionStart
            await self.send_event(json.dumps({
                "event": {
                    "sessionStart": {
                        "inferenceConfiguration": {
                            "maxTokens": 1024,
                            "topP": 0.9,
                            "temperature": 0.7,
                        },
                    }
                }
            }))

            # 2. promptStart
            await self.send_event(json.dumps({
                "event": {
                    "promptStart": {
                        "promptName": self.prompt_name,
                        "textOutputConfiguration": {
                            "mediaType": "text/plain",
                        },
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": 24000,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": self.config.voice_id,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                        "toolUseOutputConfiguration": {
                            "mediaType": "application/json",
                        },
                        "toolConfiguration": {
                            "tools": tool_definition,
                        },
                    }
                }
            }))

            # 3. System prompt
            await self.send_event(json.dumps({
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "type": "TEXT",
                        "interactive": False,
                        "role": "SYSTEM",
                        "textInputConfiguration": {
                            "mediaType": "text/plain",
                        },
                    }
                }
            }))

            await self.send_event(json.dumps({
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "content": system_prompt,
                    }
                }
            }))

            await self.send_event(json.dumps({
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                    }
                }
            }))

            # 4. Audio input start
            await self.send_event(json.dumps({
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": 16000,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }))

            # Close old stream gracefully.
            if old_stream is not None:
                try:
                    await old_stream.input_stream.close()
                except Exception as exc:
                    logger.debug("Error closing old stream during renewal: %s", exc)

            logger.info(
                "Session renewed successfully (new prompt=%s)", self.prompt_name
            )

        except Exception as exc:
            # Renewal failed — restore old stream if still usable.
            logger.error("Session renewal failed: %s", exc, exc_info=True)
            if old_stream is not None:
                self.stream = old_stream
            else:
                self.is_active = False
                await self._cleanup_stream()

    async def check_and_renew(self) -> None:
        """Public helper: renew the session if the time limit is near.

        Callers (e.g. the audio-send loop in the agent app) should call this
        periodically.  It is a no-op when renewal is not yet needed or when
        the stored system prompt / tool definition are unavailable.
        """
        if (
            self._needs_renewal()
            and self._system_prompt is not None
            and self._tool_definition is not None
        ):
            await self._renew_session(self._system_prompt, self._tool_definition)
