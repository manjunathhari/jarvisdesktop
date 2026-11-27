import json

from jarvis_voice_helper import protocol
from jarvis_voice_helper.main import run
from jarvis_voice_helper.protocol import (
    Hello,
    ModelMissing,
    NoOutputDevice,
    Say,
    Settings,
    Shutdown,
    Stop,
)

SETTINGS = Settings("bm_george", 1.0, "/m/k.onnx", "/m/v.bin", None)
HELLO = protocol.encode(Hello(protocol=1, settings=SETTINGS))


class FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line + "\n"


class FakeSpeaker:
    def __init__(self, on_speaking, start_error=None):
        self.on_speaking = on_speaking
        self.start_error = start_error
        self.events = []
        self.said = []

    async def start(self, settings):
        self.events.append("start")
        if self.start_error:
            raise self.start_error

    async def say(self, turn, text):
        self.said.append((turn, text))
        self.on_speaking(True, turn)
        self.on_speaking(False, turn)

    async def stop(self):
        self.events.append("stop")

    async def close(self):
        self.events.append("close")


async def drive(lines, start_error=None):
    out, holder = [], []

    def factory(on_speaking):
        sp = FakeSpeaker(on_speaking, start_error)
        holder.append(sp)
        return sp

    code = await run(FakeReader(lines), lambda line: out.append(json.loads(line)), factory)
    return code, out, (holder[0] if holder else None)


async def test_ready_follows_hello_then_say_reaches_the_speaker():
    code, out, sp = await drive([HELLO, protocol.encode(Say(1, "Hello.")), '{"type":"shutdown"}'])
    assert code == 0
    assert sp.said == [(1, "Hello.")]
    assert [m["type"] for m in out] == ["ready", "speaking", "speaking"]
    assert out[0] == {
        "type": "ready",
        "protocol": 1,
        "helper_version": "1.0.0",
        "voice": "bm_george",
    }
    assert out[1] == {"type": "speaking", "on": True, "turn": 1}
    assert sp.events == ["start", "close"]


async def test_first_line_not_hello_exits_2_with_bad_hello():
    code, out, sp = await drive([protocol.encode(Say(1, "hi"))])
    assert code == 2 and [m["code"] for m in out] == ["bad_hello"] and sp is None


async def test_wrong_protocol_number_or_empty_input_is_bad_hello():
    bad = protocol.encode(Hello(protocol=2, settings=SETTINGS))
    code, out, _ = await drive([bad])
    assert code == 2 and out[0]["code"] == "bad_hello"
    code, out, _ = await drive([])
    assert code == 2 and out[0]["code"] == "bad_hello"


async def test_hello_missing_settings_is_bad_hello():
    code, out, _ = await drive(['{"type":"hello","protocol":1}'])
    assert code == 2 and out[0]["code"] == "bad_hello"


async def test_stdin_end_of_file_closes_the_speaker_and_exits_0():
    code, out, sp = await drive([HELLO])
    assert code == 0 and sp.events == ["start", "close"] and [m["type"] for m in out] == ["ready"]


async def test_blank_say_is_dropped():
    code, _, sp = await drive([HELLO, protocol.encode(Say(1, "   "))])
    assert code == 0 and sp.said == []


async def test_stop_reaches_the_speaker_and_junk_lines_are_ignored():
    code, out, sp = await drive([HELLO, "{oops", '{"type":"later"}', protocol.encode(Stop())])
    assert code == 0 and sp.events == ["start", "stop", "close"]
    assert [m["type"] for m in out] == ["ready"]


async def test_second_hello_after_ready_is_ignored():
    code, out, sp = await drive([HELLO, HELLO, protocol.encode(Shutdown())])
    assert code == 0 and [m["type"] for m in out] == ["ready"] and sp.events == ["start", "close"]


async def test_speaker_start_error_maps_to_its_exit_code():
    code, out, _ = await drive([HELLO], start_error=NoOutputDevice("no sound card"))
    assert code == 3 and [m["type"] for m in out] == ["error"]
    assert out[0]["code"] == "no_output_device"
    code, out, _ = await drive([HELLO], start_error=ModelMissing("gone"))
    assert code == 4 and out[0]["code"] == "model_missing"
    code, out, _ = await drive([HELLO], start_error=RuntimeError("boom"))
    assert code == 1 and out[0]["code"] == "internal"
