"""JSON-lines message format between the desktop app and the voice helper."""

import json
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


def parse(line: str) -> Hello | Say | Stop | Shutdown | None:
    """Parse an app-to-helper line; None for bad JSON, unknown type or malformed fields."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        kind = data.get("type")
        if kind == "hello":
            raw = data["settings"]
            settings = Settings(
                voice=str(raw["voice"]),
                tts_speed=float(raw["tts_speed"]),
                model_path=str(raw["model_path"]),
                voices_path=str(raw["voices_path"]),
                output_device=raw.get("output_device"),
            )
            return Hello(protocol=data["protocol"], settings=settings)
        if kind == "say":
            return Say(turn=int(data["turn"]), text=str(data["text"]))
        if kind == "stop":
            return Stop()
        if kind == "shutdown":
            return Shutdown()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return None


class Speaker(Protocol):
    """Plays speech. Reports activity through the on_speaking callback given to its factory."""

    async def start(self, settings: Settings) -> None: ...

    async def say(self, turn: int, text: str) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...
