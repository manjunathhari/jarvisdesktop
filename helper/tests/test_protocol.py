import json

from jarvis_voice_helper import protocol
from jarvis_voice_helper.protocol import (
    Error,
    Hello,
    Ready,
    Say,
    Settings,
    Shutdown,
    Speaking,
    Stop,
)

SETTINGS = Settings(
    voice="bm_george",
    tts_speed=1.0,
    model_path="/m/kokoro.onnx",
    voices_path="/m/voices.bin",
    output_device=None,
)


def test_say_round_trips_awkward_text():
    text = 'He said "hi".\nNew line 🙂 ' + "x" * 2000
    line = protocol.encode(Say(turn=7, text=text))
    assert "\n" not in line and protocol.parse(line) == Say(turn=7, text=text)


def test_encode_does_not_escape_non_ascii():
    assert "🙂" in protocol.encode(Say(turn=1, text="🙂"))


def test_unknown_type_and_bad_json_are_ignored():
    assert protocol.parse('{"type":"later"}') is None and protocol.parse("{oops") is None
    assert protocol.parse("[1]") is None and protocol.parse('{"type":"say"}') is None


def test_hello_round_trips_and_unknown_fields_are_ignored():
    line = protocol.encode(Hello(protocol=1, settings=SETTINGS))
    assert json.loads(line)["settings"]["output_device"] is None
    assert protocol.parse(line) == Hello(protocol=1, settings=SETTINGS)
    extra = '{"type":"stop","future":1}'
    assert protocol.parse(extra) == Stop()


def test_hello_without_settings_parses_as_none():
    assert protocol.parse('{"type":"hello","protocol":1}') is None


def test_shutdown_parses():
    assert protocol.parse('{"type":"shutdown"}') == Shutdown()


def test_helper_to_app_messages_encode_exactly():
    assert json.loads(protocol.encode(Ready(voice="bm_george"))) == {
        "type": "ready",
        "protocol": 1,
        "helper_version": "1.0.0",
        "voice": "bm_george",
    }
    assert json.loads(protocol.encode(Speaking(on=True, turn=3))) == {
        "type": "speaking",
        "on": True,
        "turn": 3,
    }
    assert json.loads(protocol.encode(Error(code="internal", message="x"))) == {
        "type": "error",
        "code": "internal",
        "message": "x",
    }
    assert json.loads(protocol.encode(Stop())) == {"type": "stop"}
    assert json.loads(protocol.encode(Shutdown())) == {"type": "shutdown"}
