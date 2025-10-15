# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "py-tgcalls",
#   "telethon",
# ]
# ///
"""
Captures audio from a Telegram voice chat into a local file,
or hand that audio off live to an FFmpeg pipeline
(for example, to restream a voice chat to YouTube).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shlex
import signal
import sys
from asyncio.subprocess import PIPE as SUBPROCESS_PIPE
from datetime import UTC, datetime
from os import environ
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.sessions import StringSession

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Import after loop is set to support python 3.14 as PyTgCalls uses asyncio.get_event_loop()
from pytgcalls import PyTgCalls, filters  # noqa: E402
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError  # noqa: E402
from pytgcalls.types import (  # noqa: E402
    AudioQuality,
    ChatUpdate,
    Device,
    Direction,
    GroupCallConfig,
    RecordStream,
    StreamFrames,
)


def parse_args() -> argparse.Namespace:
    qualities = [quality.name.lower() for quality in AudioQuality]

    parser = argparse.ArgumentParser(
        description=(
            "Join an existing Telegram group call and record its audio stream to a local file "
            "using the modern PyTgCalls/NTgCalls stack."
        )
    )
    parser.add_argument(
        "chat", help="Username, invite link, or numeric ID of the target chat"
    )
    parser.add_argument(
        "--api-id",
        type=int,
        default=int(environ["TG_API_ID"]) if environ.get("TG_API_ID") else None,
        required="TG_API_ID" not in environ,
        help="Telegram API ID; defaults to $TG_API_ID if set",
    )
    parser.add_argument(
        "--api-hash",
        default=environ.get("TG_API_HASH"),
        required="TG_API_HASH" not in environ,
        help="Telegram API hash; defaults to $TG_API_HASH if set",
    )
    parser.add_argument(
        "--session",
        default=environ.get("TG_SESSION", "pytgcalls_session"),
        help="Session name or file path. Defaults to $TG_SESSION or 'pytgcalls_session'",
    )
    parser.add_argument(
        "--string-session",
        action="store_true",
        help="Interpret --session as a Telethon StringSession instead of a file name",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output filename. Defaults to ./call-<timestamp>.mp3 when no live handoff is requested",
    )
    parser.add_argument(
        "--quality",
        choices=qualities,
        default=environ.get("OUTPUT_AUDIO_QUALITY", "low").lower(),
        help="Audio quality preset (determines channels/bitrate and codec)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        metavar="SECONDS",
        help="Stop recording automatically after the given number of seconds",
    )
    parser.add_argument(
        "--join-as",
        help="Join the call as the provided peer (username, channel ID, etc.)",
    )
    parser.add_argument(
        "--invite-hash",
        help="Speaker invite hash to join as a presenter when required",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Start the voice chat if none is active (default: fail if no call is running)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists",
    )
    parser.add_argument(
        "--live-cmd",
        default=None,
        help=(
            "Shell command to execute for live handoff. Use {sample_rate} and {channels} placeholders "
            "to inject the chosen audio parameters. The command must read PCM s16le from stdin."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def build_client(
    session: str, api_id: int, api_hash: str, use_string_session: bool
) -> TelegramClient:
    if use_string_session:
        return TelegramClient(StringSession(session), api_id, api_hash, loop=loop)

    session_path = Path(session).expanduser()
    if session_path.suffix:
        return TelegramClient(str(session_path), api_id, api_hash, loop=loop)
    return TelegramClient(session, api_id, api_hash, loop=loop)


async def schedule_duration(duration: int, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.sleep(duration)
        if not stop_event.is_set():
            logging.info("Requested duration elapsed; stopping recording.")
            stop_event.set()
    except asyncio.CancelledError:  # pragma: no cover - timer cancelled on shutdown
        pass


async def record_call(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)8s | %(message)s",
    )

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    default_output = f"call-{timestamp}.mp3"
    output_target = args.output or environ.get("OUTPUT_PATH")
    live_cmd_template = args.live_cmd or environ.get("LIVE_CMD")

    if live_cmd_template and output_target:
        raise RuntimeError(
            "Simultaneous file recording and live handoff is not supported. "
            "Omit --output (and OUTPUT_PATH) or run a second process to capture from the live command."
        )

    output_path: Path | None = None
    if output_target:
        output_path = Path(output_target).expanduser().resolve()
    elif not live_cmd_template:
        output_path = Path(default_output).expanduser().resolve()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.exists() and not args.overwrite:
            raise RuntimeError(
                f"Output file {output_path} already exists. Use --overwrite to replace it."
            )

    client = build_client(args.session, args.api_id, args.api_hash, args.string_session)
    stop_event = asyncio.Event()
    duration_task: asyncio.Task[None] | None = None

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "The supplied session is not authorized. Log in using Telethon first and rerun the recorder."
            )

        call = PyTgCalls(client)
        await call.start()

        def request_stop(reason: str) -> None:
            if not stop_event.is_set():
                logging.info(reason)
                stop_event.set()

        signal_loop = asyncio.get_running_loop()

        for signame in ("SIGINT", "SIGTERM"):
            if hasattr(signal, signame):
                try:
                    signal_loop.add_signal_handler(
                        getattr(signal, signame),
                        lambda s=signame: request_stop(f"Received {s}; stopping..."),
                    )
                except (
                    NotImplementedError
                ):  # pragma: no cover - unsupported on some platforms
                    pass

        if args.duration and args.duration > 0:
            duration_task = asyncio.create_task(
                schedule_duration(args.duration, stop_event)
            )

        @call.on_update(
            filters.chat(args.chat) & filters.chat_update(ChatUpdate.Status.LEFT_CALL)
        )
        async def _on_left(_: PyTgCalls, update: ChatUpdate):  # noqa: ANN001 - signature fixed by library
            request_stop(f"Voice chat ended (status={update.status}).")

        try:
            entity = await client.get_entity(args.chat)
            chat_label = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or str(entity.id)
            )
        except (RPCError, ValueError):
            chat_label = str(args.chat)

        quality = AudioQuality[args.quality.upper()]
        sample_rate, channels = quality.value
        if output_path is not None:
            record_stream = RecordStream(
                audio=str(output_path),
                audio_parameters=quality,
            )
        else:
            record_stream = RecordStream(True, quality)

        live_proc: asyncio.subprocess.Process | None = None
        live_queue: asyncio.Queue[bytes | None] | None = None
        live_task: asyncio.Task[None] | None = None
        live_monitor: asyncio.Task[None] | None = None
        live_active = False

        if live_cmd_template:
            formatted_cmd = live_cmd_template.format(
                sample_rate=sample_rate,
                channels=channels,
            )
            logging.info("Starting live handoff command: %s", formatted_cmd)
            try:
                live_proc = await asyncio.create_subprocess_exec(
                    *shlex.split(formatted_cmd),
                    stdin=SUBPROCESS_PIPE,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Failed to start live command '{formatted_cmd}': {exc}"
                ) from exc

            live_queue = asyncio.Queue(maxsize=256)
            live_queue_warned = False
            live_active = True

            async def pump_live() -> None:
                assert live_queue is not None
                try:
                    while True:
                        chunk = await live_queue.get()
                        if chunk is None:
                            break
                        if (
                            not live_proc
                            or live_proc.stdin is None
                            or live_proc.stdin.is_closing()
                        ):
                            break
                        try:
                            live_proc.stdin.write(chunk)
                            await live_proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            request_stop(
                                "Live command input closed unexpectedly; stopping..."
                            )
                            break
                finally:
                    if (
                        live_proc
                        and live_proc.stdin
                        and not live_proc.stdin.is_closing()
                    ):
                        with contextlib.suppress(Exception):
                            live_proc.stdin.close()

            async def monitor_live() -> None:
                assert live_proc is not None
                returncode = await live_proc.wait()
                if live_active and not stop_event.is_set():
                    request_stop(
                        f"Live command exited with return code {returncode}; stopping..."
                    )
                if live_queue is not None and live_active:
                    await live_queue.put(None)

            live_task = asyncio.create_task(pump_live())
            live_monitor = asyncio.create_task(monitor_live())

            stream_filter = filters.chat(args.chat) & filters.stream_frame(
                Direction.INCOMING,
                Device.MICROPHONE,
            )

            @call.on_update(stream_filter)
            async def _forward_stream(_: PyTgCalls, update: StreamFrames) -> None:  # noqa: ANN001
                nonlocal live_queue_warned
                if not live_active or live_queue is None:
                    return
                for frame in update.frames:
                    if (
                        live_proc is None
                        or live_proc.stdin is None
                        or live_proc.stdin.is_closing()
                    ):
                        return
                    try:
                        live_queue.put_nowait(frame.frame)
                    except asyncio.QueueFull:
                        if not live_queue_warned:
                            live_queue_warned = True
                            logging.warning(
                                "Dropping audio frame: live consumer is too slow (queue size=%d).",
                                live_queue.maxsize,
                            )
                    else:
                        if (
                            live_queue_warned
                            and live_queue.qsize() < live_queue.maxsize // 2
                        ):
                            logging.info(
                                "Live handoff consumer caught up; queue depth %d.",
                                live_queue.qsize(),
                            )
                            live_queue_warned = False

        config = GroupCallConfig(
            invite_hash=args.invite_hash,
            join_as=args.join_as,
            auto_start=args.auto_start,
        )

        destinations = []
        if output_path is not None:
            destinations.append(str(output_path))
        if live_cmd_template:
            destinations.append("live handoff")
        destination_label = ", ".join(destinations) if destinations else "live handoff"

        logging.info(
            "Connecting to voice chat in %s (quality=%s) and recording to %s",
            chat_label,
            quality.name.lower(),
            destination_label,
        )
        try:
            await call.record(args.chat, record_stream, config=config)
        except NoActiveGroupCall as exc:
            raise RuntimeError(
                "No active voice chat found. Start the call (or pass --auto-start) and try again."
            ) from exc
        except FileNotFoundError as exc:
            target = str(output_path) if output_path is not None else "<stream>"
            raise RuntimeError(f"Unable to create output file {target}: {exc}") from exc

        logging.info("Recording started. Press Ctrl+C to stop.")
        await stop_event.wait()

        logging.info("Stopping recording...")
        live_active = False

        with contextlib.suppress(NotInCallError):
            await call.leave_call(args.chat)

        if live_queue is not None:
            await live_queue.put(None)
        if live_task is not None:
            with contextlib.suppress(Exception):
                await live_task
        if live_monitor is not None:
            live_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live_monitor
        if live_proc is not None:
            if live_proc.stdin and not live_proc.stdin.is_closing():
                with contextlib.suppress(Exception):
                    live_proc.stdin.close()
            if live_proc.returncode is None:
                with contextlib.suppress(Exception):
                    live_proc.terminate()
                try:
                    await asyncio.wait_for(live_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(Exception):
                        live_proc.kill()
                    await live_proc.wait()

        # Allow ffmpeg subprocess to flush if necessary.
        await asyncio.sleep(0.5)

    finally:
        if duration_task is not None:
            duration_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await duration_task
        if client.is_connected():
            await client.disconnect()

    if output_path is not None:
        if output_path.exists():
            size_mib = output_path.stat().st_size / (1024 * 1024)
            logging.info(
                "Recording finished. Saved %.2f MiB to %s", size_mib, output_path
            )
        else:
            logging.warning("Recording finished but %s was not created.", output_path)
    elif live_cmd_template:
        logging.info("Live handoff completed.")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(record_call(args))
    except (
        KeyboardInterrupt
    ):  # pragma: no cover - handled via signals but keep as guard
        pass
    except Exception as exc:  # pragma: no cover - propagate meaningful errors
        logging.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
