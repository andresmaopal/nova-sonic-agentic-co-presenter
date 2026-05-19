"""Local development runner for the Presentation Assistant Agent.

This is the main entry point for running the agent locally with a
microphone and speakers. It:
1. Preprocesses the .pptx file (or loads pre-exported images)
2. Starts a NovaSonicSession with the configured voice/locale
3. Runs the audio I/O loop (mic capture → Nova Sonic → speaker playback)
4. Starts the keyboard hook in a background thread
5. Routes tool calls to analyze_slide

Usage:
    python -m src.main presentation.pptx
    python -m src.main presentation.pptx --images-dir ./slides --voice-id tiffany --region us-east-1
"""

import argparse
import asyncio
import json
import logging
import threading
import time

import pyaudio

from src.hooks.keyboard_hook import run_hook as run_keyboard_hook, run_hook_inprocess
from src.models import SessionConfig
from src.nova_sonic_session import NovaSonicSession
from src.pptx_preprocessor import convert_pptx, load_from_images
from src.slide_cache import load_cached, save_cache
from src.slide_store import SlideStore
from src.tools.analyze_slide import (
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    analyze_slide,
)
from src.tools.navigate_slide import (
    TOOL_NAME as NAV_TOOL_NAME,
    TOOL_DESCRIPTION as NAV_TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA as NAV_TOOL_INPUT_SCHEMA,
    navigate_slide,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio constants (task 9.2)
# ---------------------------------------------------------------------------
INPUT_SAMPLE_RATE = 16000   # mic capture: 16 kHz
OUTPUT_SAMPLE_RATE = 24000  # speaker playback: 24 kHz
CHANNELS = 1
FORMAT = pyaudio.paInt16    # PCM 16-bit
CHUNK_SIZE = 512


# ---------------------------------------------------------------------------
# System prompt & tool definition helpers
# ---------------------------------------------------------------------------


def build_system_prompt(slide_count: int) -> str:
    """Build system prompt with slide count and tool instructions."""
    return (
        f"You are a co-presenter helping deliver a live presentation with {slide_count} slides. "
        f"You are NOT describing slides to the presenter — you ARE presenting alongside them. "
        f"You have access to the {TOOL_NAME} tool which can see and analyze the current slide image. "
        f"ALWAYS use the {TOOL_NAME} tool when the presenter asks anything about a slide — "
        f"you cannot see the slides yourself, only the tool can. "
        f"Use the tool for: describing slides, giving talking points, answering questions about content, "
        f"reading text on slides, or any visual question. "
        f"You also have the {NAV_TOOL_NAME} tool to navigate slides. "
        f"Use it when the presenter says 'next slide', 'previous slide', or 'go back'. "
        f"NEVER mention slide numbers or say things like 'this slide shows' or 'on slide 3' — "
        f"just present the content directly and naturally as if speaking to the audience. "
        f"The slide tracking is automatic — the tool always analyzes whichever slide the presenter is currently on. "
        f"Keep your spoken responses brief — 1 to 4 sentences unless asked for more detail."
    )


def build_tool_definition() -> list:
    """Build tool definitions for Nova Sonic."""
    return [
        {
            "toolSpec": {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": {"json": json.dumps(TOOL_INPUT_SCHEMA)},
            }
        },
        {
            "toolSpec": {
                "name": NAV_TOOL_NAME,
                "description": NAV_TOOL_DESCRIPTION,
                "inputSchema": {"json": json.dumps(NAV_TOOL_INPUT_SCHEMA)},
            }
        },
    ]


# ---------------------------------------------------------------------------
# Audio I/O coroutines (task 9.2)
# ---------------------------------------------------------------------------


async def audio_playback(session: NovaSonicSession, audio_queue: asyncio.Queue) -> None:
    """Play audio from the queue through speakers at 24 kHz.
    
    Supports barge-in: when session.barge_in is set, clears the queue
    so the assistant stops speaking immediately.
    """
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=FORMAT, channels=CHANNELS, rate=OUTPUT_SAMPLE_RATE,
        output=True, frames_per_buffer=CHUNK_SIZE
    )
    try:
        while session.is_active:
            # Check barge-in flag
            if getattr(session, 'barge_in', False):
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                session.barge_in = False
                await asyncio.sleep(0.05)
                continue

            try:
                audio_data = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                if audio_data:
                    for i in range(0, len(audio_data), CHUNK_SIZE):
                        if not session.is_active or getattr(session, 'barge_in', False):
                            break
                        end = min(i + CHUNK_SIZE, len(audio_data))
                        chunk = audio_data[i:end]
                        await asyncio.get_event_loop().run_in_executor(None, stream.write, chunk)
                        await asyncio.sleep(0.001)
            except asyncio.TimeoutError:
                continue
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


async def audio_capture(session: NovaSonicSession) -> None:
    """Capture mic audio at 16 kHz and send to Nova Sonic.
    
    Runs concurrently with playback, enabling barge-in — the mic keeps
    streaming even while the assistant is speaking.
    """
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=FORMAT, channels=CHANNELS, rate=INPUT_SAMPLE_RATE,
        input=True, frames_per_buffer=CHUNK_SIZE
    )

    await session.start_audio_input()

    try:
        while session.is_active:
            audio_data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            await session.send_audio_chunk(audio_data)
            await asyncio.sleep(0.01)
    finally:
        if stream.is_active():
            stream.stop_stream()
        stream.close()
        pa.terminate()


# ---------------------------------------------------------------------------
# Main async session loop (task 9.4)
# ---------------------------------------------------------------------------


async def run_session(
    slide_store: SlideStore,
    config: SessionConfig,
    region: str,
    agent_url: str,
) -> None:
    """Run the full audio I/O session with Nova Sonic."""
    import boto3

    system_prompt = build_system_prompt(slide_store.total_slides)
    tool_def = build_tool_definition()

    session = NovaSonicSession(config=config, region=region)
    await session.start_session(system_prompt, tool_def)

    bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    audio_queue: asyncio.Queue = asyncio.Queue()

    # Start a local slide-tracking thread that updates SlideStore directly
    # (no HTTP server needed for local dev).
    hook_thread = threading.Thread(target=run_hook_inprocess, args=(slide_store,), daemon=True)
    hook_thread.start()

    print("Session active — speak into your microphone. Press Ctrl+C to stop.")

    # Start audio tasks — add a brief delay to let the stream stabilize
    # before sending audio chunks.
    await asyncio.sleep(0.5)
    playback_task = asyncio.create_task(audio_playback(session, audio_queue))
    capture_task = asyncio.create_task(audio_capture(session))

    try:
        async for event_type, payload in session.process_responses():
            if event_type == "audio":
                await audio_queue.put(payload)
            elif event_type == "tool_use":
                tool_name = payload.get("tool_name", "")
                logger.info(
                    "Tool call: %s on slide %d/%d",
                    tool_name,
                    slide_store.current_index + 1,
                    slide_store.total_slides,
                )
                if tool_name == NAV_TOOL_NAME:
                    tool_result = navigate_slide(
                        slide_store=slide_store,
                        tool_input=payload.get("content", {}),
                    )
                else:
                    tool_result = analyze_slide(
                        slide_store=slide_store,
                        tool_input=payload.get("content", {}),
                        bedrock_client=bedrock_client,
                        vision_model_id=config.vision_model_id,
                    )
                await session.send_tool_result(
                    tool_use_id=payload.get("tool_use_id", ""),
                    result=tool_result,
                )
            elif event_type == "text":
                logger.debug("Nova Sonic text: %s", payload)
            elif event_type == "session_end":
                break

            await session.check_and_renew()

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        capture_task.cancel()
        playback_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass
        try:
            await playback_task
        except asyncio.CancelledError:
            pass
        await session.end_session()
        print("Session ended.")


# ---------------------------------------------------------------------------
# CLI entry point (task 9.1, 9.3)
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point — ``presentation-agent`` console script."""
    parser = argparse.ArgumentParser(
        description="Presentation Assistant Agent — local dev runner"
    )
    parser.add_argument("pptx", nargs="?", help="Path to .pptx file")
    parser.add_argument(
        "--images-dir",
        help="Directory of pre-exported slide images (fallback when LibreOffice is unavailable)",
    )
    parser.add_argument(
        "--voice-id", default="tiffany",
        help="Nova Sonic voice (default: tiffany)",
    )
    parser.add_argument(
        "--language-locale", default="en-US",
        help="Language locale (default: en-US)",
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--agent-url", default="http://127.0.0.1:8000",
        help="Agent URL for keyboard hook (default: http://127.0.0.1:8000)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.pptx and not args.images_dir:
        parser.error("Provide a .pptx path or --images-dir")

    # Task 9.4: Preprocess PPTX (or load pre-exported images).
    if args.images_dir:
        slides = load_from_images(args.pptx, args.images_dir)
    else:
        # Try cache first
        slides = load_cached(args.pptx)
        if slides is None:
            slides = convert_pptx(args.pptx)
            save_cache(args.pptx, slides)

    slide_store = SlideStore()
    slide_store.load_slides(slides)
    print(f"Loaded {slide_store.total_slides} slides.")

    config = SessionConfig(
        voice_id=args.voice_id,
        language_locale=args.language_locale,
    )

    asyncio.run(run_session(slide_store, config, args.region, args.agent_url))


if __name__ == "__main__":
    main()
