"""Helper main loop: hello handshake, then say/stop/shutdown until shutdown or end of file."""

import argparse
import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import AsyncIterable, Callable
from pathlib import Path
from typing import BinaryIO

from jarvis_voice_helper import protocol
from jarvis_voice_helper.protocol import (
    BAD_HELLO,
    INTERNAL,
    MODEL_MISSING,
    NO_OUTPUT_DEVICE,
    Error,
    Hello,
    ModelMissing,
    NoOutputDevice,
    Ready,
    Say,
    Shutdown,
    Speaker,
    Speaking,
    Stop,
)

Writer = Callable[[str], None]
SpeakerFactory = Callable[[Callable[[bool, int], None]], Speaker]


def _fail(writer: Writer, code: str, message: str, exit_code: int) -> int:
    writer(protocol.encode(Error(code=code, message=message)))
    return exit_code


async def run(reader: AsyncIterable[str], writer: Writer, speaker_factory: SpeakerFactory) -> int:
    """Run the helper. `reader` yields text lines; `writer` takes one line without newline.

    The speaker's `say` should return promptly (queue the text) so `stop` stays responsive.
    """
    lines = aiter(reader)

    try:
        first = await anext(lines)
    except StopAsyncIteration:
        return _fail(writer, BAD_HELLO, "input ended before hello", 2)
    try:
        hello = protocol.parse(first.strip())
    except Exception:
        hello = None
    if not isinstance(hello, Hello) or hello.protocol != protocol.PROTOCOL_VERSION:
        return _fail(writer, BAD_HELLO, "first line must be a protocol 1 hello with settings", 2)

    def on_speaking(on: bool, turn: int) -> None:
        writer(protocol.encode(Speaking(on=on, turn=turn)))

    speaker = speaker_factory(on_speaking)
    try:
        await speaker.start(hello.settings)
    except NoOutputDevice as exc:
        await _close_quietly(speaker)
        return _fail(writer, NO_OUTPUT_DEVICE, str(exc), 3)
    except ModelMissing as exc:
        await _close_quietly(speaker)
        return _fail(writer, MODEL_MISSING, str(exc), 4)
    except Exception as exc:
        await _close_quietly(speaker)
        return _fail(writer, INTERNAL, type(exc).__name__, 1)
    writer(protocol.encode(Ready(voice=hello.settings.voice)))

    failure = asyncio.create_task(speaker.wait_failure())

    async def next_line() -> str:
        return await anext(lines)

    try:
        while True:
            pending = asyncio.create_task(next_line())
            await asyncio.wait({pending, failure}, return_when=asyncio.FIRST_COMPLETED)
            if failure.done():
                pending.cancel()
                reason = failure.result()
                await _close_quietly(speaker)
                return _fail(writer, INTERNAL, reason, 1)
            try:
                raw = pending.result()
            except StopAsyncIteration:
                break
            msg = protocol.parse(raw.strip())
            if isinstance(msg, Shutdown):
                break
            if isinstance(msg, Say) and msg.text.strip():
                await speaker.say(msg.turn, msg.text)
            elif isinstance(msg, Stop):
                await speaker.stop()
    except Exception as exc:
        failure.cancel()
        await _close_quietly(speaker)
        return _fail(writer, INTERNAL, type(exc).__name__, 1)
    failure.cancel()
    await speaker.close()
    return 0


async def _close_quietly(speaker: Speaker) -> None:
    try:
        await speaker.close()
    except Exception as exc:
        print(f"close failed: {type(exc).__name__}", file=sys.stderr)


class StdinLines:
    """Lines from a binary stream, read on a background thread and handed to the event loop.

    Splits on b"\\n" only (reply text may hold U+2028, U+2029 or U+0085) and ignores lines that
    are not valid UTF-8. Reading stdin through asyncio pipes is unreliable on Windows.
    """

    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def __aiter__(self):
        return self._lines()

    async def _lines(self):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def pump() -> None:
            try:
                for raw in iter(self._stream.readline, b""):
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    loop.call_soon_threadsafe(queue.put_nowait, text)
            except (OSError, ValueError):
                pass
            with contextlib.suppress(RuntimeError):  # the loop may already be closed
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=pump, name="stdin-reader", daemon=True).start()
        while (line := await queue.get()) is not None:
            yield line


def make_writer(stream: BinaryIO) -> Writer:
    """A writer that sends each protocol line as UTF-8 plus a newline and flushes at once."""
    lock = threading.Lock()

    def write(line: str) -> None:
        with lock:
            try:
                stream.write(line.encode("utf-8") + b"\n")
                stream.flush()
            except (OSError, ValueError):
                pass  # the app has gone; its end of stdin follows

    return write


def protect_stdout() -> BinaryIO:
    """Keep the real stdout for protocol lines only; everything else is sent to stderr."""
    sys.stdout.flush()
    protocol_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)  # C-level prints go to stderr too
    sys.stdout = sys.stderr
    return protocol_out


WARNING_NO = 30  # loguru's severity number for WARNING


_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - "
    "{message}\n{exception}"
)
_WITHHELD = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - "
    "(message withheld: it may contain spoken text)\n"
)


def _is_pipecat(record) -> bool:
    return record["name"].startswith("pipecat")


def _keep(record) -> bool:
    """Pipecat's DEBUG logs hold the spoken text, so its records below WARNING are dropped."""
    return not (_is_pipecat(record) and record["level"].no < WARNING_NO)


def _format(record) -> str:
    """Pipecat's WARNING and ERROR messages can carry the spoken text.

    Examples in 1.17.0: tts_service "not speaking [{text}]" and "produced no audio, not adding
    [{dropped_text}]" (WARNING); ai_service "exception processing {frame}" and the pipeline
    worker's "Something went wrong: {frame}" (frames and exceptions that hold the text). From
    WARNING up only where a Pipecat call came from is kept: never its message or traceback.
    """
    return _WITHHELD if _is_pipecat(record) else _FORMAT


def configure_logging(stream=None) -> None:
    """Logs go to stderr at INFO or above, and what is spoken is never in them."""
    from loguru import logger

    logger.remove()
    logger.add(
        stream if stream is not None else sys.stderr, level="INFO", filter=_keep, format=_format
    )


def _real_speaker_factory(on_speaking: Callable[[bool, int], None]) -> Speaker:
    from jarvis_voice_helper.pipeline import PipecatSpeaker

    return PipecatSpeaker(on_speaking)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis_voice_helper")
    parser.add_argument("--selftest", metavar="PATH", help="speak a test phrase into a WAV file")
    parser.add_argument("--model")
    parser.add_argument("--voices")
    parser.add_argument(
        "--require-audio-lib",
        action="store_true",
        help="with --selftest: also fail if the sound library (pyaudio) cannot be imported",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        if not (args.model and args.voices):
            parser.error("--selftest needs --model and --voices")
        configure_logging()
        from jarvis_voice_helper import selftest

        return selftest.run(
            Path(args.selftest), args.model, args.voices, require_audio_lib=args.require_audio_lib
        )

    protocol_out = protect_stdout()
    configure_logging()
    return asyncio.run(
        run(StdinLines(sys.stdin.buffer), make_writer(protocol_out), _real_speaker_factory)
    )
