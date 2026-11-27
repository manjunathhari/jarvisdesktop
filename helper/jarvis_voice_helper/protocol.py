"""JSON-lines message format between the desktop app and the voice helper."""

import json
import math
from dataclasses import asdict, dataclass
from typing import Protocol

PROTOCOL_VERSION = 1
HELPER_VERSION = "1.0.0"

BAD_HELLO = "bad_hello"
MODEL_MISSING = "model_missing"
NO_OUTPUT_DEVICE = "no_output_device"
INTERNAL = "internal"


class NoOutputDevice(Exception):
    """No usable audio output device (exit code 3)."""


class ModelMissing(Exception):
    """The speech model or voices file is missing (exit code 4)."""


@dataclass(frozen=True)
class Settings:
    voice: str
    tts_speed: float
    model_path: str
    voices_path: str
    output_device: int | None


@dataclass(frozen=True)
class Hello:
    protocol: int
    settings: Settings


@dataclass(frozen=True)
class Say:
    turn: int
    text: str


@dataclass(frozen=True)
class Stop:
    pass


@dataclass(frozen=True)
class Shutdown:
    pass


@dataclass(frozen=True)
class Ready:
    voice: str
    protocol: int = PROTOCOL_VERSION
    helper_version: str = HELPER_VERSION


@dataclass(frozen=True)
class Speaking:
    on: bool
    turn: int


@dataclass(frozen=True)
class Error:
    code: str
    message: str


Message = Hello | Say | Stop | Shutdown | Ready | Speaking | Error

_TYPES: dict[type, str] = {
    Hello: "hello",
    Say: "say",
    Stop: "stop",
    Shutdown: "shutdown",
    Ready: "ready",
    Speaking: "speaking",
    Error: "error",
}


def encode(msg: Message) -> str:
    """Encode a message as one JSON line (no trailing newline)."""
    body = {"type": _TYPES[type(msg)], **asdict(msg)}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def parse(line: str) -> Hello | Say | Stop | Shutdown | None:
    """Parse an app-to-helper line; None for anything unusable. Never raises.

    Field types are strict: a wrong type makes the whole line unparseable.
    """
    try:
        return _parse(line)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        return None


def _parse(line: str) -> Hello | Say | Stop | Shutdown | None:
    data = json.loads(line)
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind == "hello":
        raw = data["settings"]
        speed = raw["tts_speed"]
        device = raw.get("output_device")
        protocol_number = data["protocol"]
        if not _is_int(protocol_number):
            return None
        if not _is_number(speed) or not math.isfinite(speed):
            return None
        if device is not None and not _is_int(device):
            return None
        settings = Settings(
            voice=_str(raw["voice"]),
            tts_speed=speed,
            model_path=_str(raw["model_path"]),
            voices_path=_str(raw["voices_path"]),
            output_device=device,
        )
        return Hello(protocol=protocol_number, settings=settings)
    if kind == "say":
        turn = data["turn"]
        if not _is_int(turn):
            return None
        return Say(turn=turn, text=_str(data["text"]))
    if kind == "stop":
        return Stop()
    if kind == "shutdown":
        return Shutdown()
    return None


class Speaker(Protocol):
    """Plays speech. Reports activity through the on_speaking callback given to its factory."""

    async def start(self, settings: Settings) -> None: ...

    async def say(self, turn: int, text: str) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...
