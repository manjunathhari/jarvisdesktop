"""Helper main loop: hello handshake, then say/stop/shutdown until shutdown or end of file."""

import sys
from collections.abc import AsyncIterable, Callable

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
    hello = protocol.parse(first.strip())
    if not isinstance(hello, Hello) or hello.protocol != protocol.PROTOCOL_VERSION:
        return _fail(writer, BAD_HELLO, "first line must be a protocol 1 hello with settings", 2)

    def on_speaking(on: bool, turn: int) -> None:
        writer(protocol.encode(Speaking(on=on, turn=turn)))

    speaker = speaker_factory(on_speaking)
    try:
        await speaker.start(hello.settings)
    except NoOutputDevice as exc:
        return _fail(writer, NO_OUTPUT_DEVICE, str(exc), 3)
    except ModelMissing as exc:
        return _fail(writer, MODEL_MISSING, str(exc), 4)
    except Exception as exc:
        return _fail(writer, INTERNAL, str(exc), 1)
    writer(protocol.encode(Ready(voice=hello.settings.voice)))

    try:
        async for raw in lines:
            msg = protocol.parse(raw.strip())
            if isinstance(msg, Shutdown):
                break
            if isinstance(msg, Say) and msg.text.strip():
                await speaker.say(msg.turn, msg.text)
            elif isinstance(msg, Stop):
                await speaker.stop()
    except Exception as exc:
        await _close_quietly(speaker)
        return _fail(writer, INTERNAL, str(exc), 1)
    await speaker.close()
    return 0


async def _close_quietly(speaker: Speaker) -> None:
    try:
        await speaker.close()
    except Exception as exc:
        print(f"close failed: {exc}", file=sys.stderr)
