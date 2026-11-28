"""A stand-in for the CAD runner (plan `.10x/plans/2026-11-28-cad-board-slice-1.md`, Tasks 2 and 3).

`FakeRunner` listens on 127.0.0.1 on an ephemeral port, in a thread, and speaks the runner's
protocol: one JSON request line in, one answer line out, then the connection closes. It records
every request it got (`requests`: the parsed request, or the raw bytes of one that is not JSON)
and answers from a script:

- `None` (the default): every request is answered as the real runner would answer a healthy
  engine (`answer_for`): the runner's own refusals (`bad_request`, `too_many_commands`,
  `not_allowed`), otherwise a good replay or catalogue.
- a list: one entry per connection, in order (after the last, `answer_for`). An entry is an
  answer (a dict, sent as one JSON line), a callable taking the request and returning an entry,
  or a misbehaviour: `NEVER_ANSWER`, `CLOSE_EARLY`, `OVERLONG`, `NOT_JSON`, `Raw(bytes)`,
  `HoldOpen(bytes)`, `Trickle(bytes, interval)`.
- a callable: called with each request, returns an entry.

Helpers build answers: `good_replay(request, failed=...)` (results in order, counts from the
commands, a tiny valid PNG, a small SVG that passes `cad_engine.check_svg`), `catalogue_answer`,
`failure(code)`. Every drawing here is fictional.
"""

from __future__ import annotations

import base64
import contextlib
import json
import socket
import socketserver
import struct
import threading
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from jarvis_server.core import cad_engine

REQUEST_LINE_MAX = 1_048_576  # as the runner
ENGINE_VERSION = "0.12.0"

# The engine's own shapes (measured: svg, rect, polyline, path), fictional coordinates.
SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm"'
    b' viewBox="0 0 420 297">\n'
    b'<rect width="420" height="297" fill="#ffffff"/>\n'
    b'<polyline points="10,10 410,10 410,287 10,287 10,10" fill="none" stroke="#000000"'
    b' stroke-width="0.5" stroke-linecap="round" stroke-linejoin="round"/>\n'
    b'<path d="M 20 20 L 60 20 M 40 20 L 40 40" fill="none" stroke="#000000"'
    b' stroke-width="0.35"/>\n'
    b"</svg>\n"
)


def _png(width: int = 2, height: int = 2) -> bytes:
    """A tiny valid PNG (white, 8-bit greyscale)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = _png()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --- misbehaviours ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Misbehave:
    name: str


NEVER_ANSWER = _Misbehave("never answer")  # reads the request, then holds the connection open
CLOSE_EARLY = _Misbehave("close early")  # closes after half an answer line, with no newline
OVERLONG = _Misbehave("overlong")  # a line longer than the client's 8 MiB limit
NOT_JSON = _Misbehave("not json")  # one line that is not JSON


@dataclass(frozen=True)
class Raw:
    """These bytes, as they are, then close."""

    data: bytes


@dataclass(frozen=True)
class HoldOpen:
    """These bytes, then the connection is held open (never closed by the runner)."""

    data: bytes


@dataclass(frozen=True)
class Trickle:
    """`data` one byte at a time, `interval` seconds apart (each read gets something, so only
    a deadline over the whole exchange stops it)."""

    data: bytes
    interval: float = 0.05


# --- answers ------------------------------------------------------------------------------------


def failure(code: str) -> dict[str, Any]:
    """`{"v":1,"ok":false,"error":code}` (bad_request, not_allowed, too_many_commands, timeout,
    busy, engine_failed, too_large; or any other text, for the unknown-code tests)."""
    return {"v": 1, "ok": False, "error": code}


def _commands(request: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    return [
        (s, n, cmd)
        for s, step in enumerate(request.get("steps") or [], start=1)
        for n, cmd in enumerate(step.get("commands") or [], start=1)
    ]


def good_replay(
    request: dict[str, Any],
    *,
    failed: Iterable[tuple[int, int]] | Callable[[dict[str, Any]], bool] = (),
    svg: bytes = SVG,
    png: bytes | None = PNG,
    extents: list[float] | None = None,
    engine_version: str = ENGINE_VERSION,
) -> dict[str, Any]:
    """A well-formed answer to this replay request. `failed`: the (step, n) pairs to report as
    failed, or a test on the command dict; a command named `fail` fails too. Counts are one kind
    per command id (`line` -> `Line`). The SVG comes only when `picture` was asked and nothing
    failed, as from the runner."""
    if callable(failed):
        is_failed = failed
        pairs: set[tuple[int, int]] = set()
    else:
        pairs = set(failed)

        def is_failed(_cmd: dict[str, Any]) -> bool:
            return False

    results = []
    counts: dict[str, int] = {}
    texts: list[str] = []
    for s, n, cmd in _commands(request):
        name = str(cmd.get("command", ""))
        bad = (s, n) in pairs or name == "fail" or is_failed(cmd)
        results.append(
            {
                "step": s,
                "n": n,
                "command": name,
                "ok": not bad,
                "text": f"Error: {name} failed" if bad else f"{name}: done",
            }
        )
        if not bad:
            kind = name.split(".")[0].capitalize()[:40] or "Item"
            counts[kind] = counts.get(kind, 0) + 1
            text = (cmd.get("params") or {}).get("text")
            if isinstance(text, str) and len(texts) < 80:
                texts.append(" ".join(text.split())[:80])
    n_failed = sum(1 for r in results if not r["ok"])
    picture = request.get("picture") is True and n_failed == 0
    return {
        "v": 1,
        "ok": True,
        "engine": "cadcraft",
        "engine_version": engine_version,
        "results": results,
        "failed": n_failed,
        "counts": counts,
        "items": sum(counts.values()),
        "extents": [420.0, 297.0] if extents is None and results else extents,
        "texts": texts,
        "png_b64": b64(png) if png is not None else None,
        "svg_b64": b64(svg) if picture else None,
    }


def catalogue_answer(
    commands: dict[str, str] | None = None, *, engine_version: str = ENGINE_VERSION
) -> dict[str, Any]:
    """The catalogue: a fictional parameter note for every allowed command by default."""
    if commands is None:
        commands = {c: f"{c} parameters (fictional note)" for c in sorted(cad_engine.ALLOWED)}
    return {"v": 1, "ok": True, "engine_version": engine_version, "commands": commands}


_REPLAY_KEYS = {"v", "op", "steps", "picture"}


def answer_for(request: Any) -> dict[str, Any]:
    """What the real runner answers with a healthy engine."""
    if not isinstance(request, dict) or request.get("v") != 1:
        return failure("bad_request")
    if request.get("op") == "catalogue":
        return catalogue_answer() if set(request) == {"v", "op"} else failure("bad_request")
    if request.get("op") != "replay" or not set(request) <= _REPLAY_KEYS:
        return failure("bad_request")
    steps = request.get("steps")
    if not isinstance(steps, list):
        return failure("bad_request")
    for step in steps:
        if not (isinstance(step, dict) and set(step) == {"label", "commands"}):
            return failure("bad_request")
        if not (isinstance(step["label"], str) and isinstance(step["commands"], list)):
            return failure("bad_request")
        for cmd in step["commands"]:
            if not (isinstance(cmd, dict) and set(cmd) == {"command", "params"}):
                return failure("bad_request")
            if not (isinstance(cmd["command"], str) and isinstance(cmd["params"], dict)):
                return failure("bad_request")
    commands = _commands(request)
    if len(commands) > cad_engine.RECIPE_MAX:
        return failure("too_many_commands")
    if any(cmd["command"] not in cad_engine.ALLOWED for _, _, cmd in commands):
        return failure("not_allowed")
    return good_replay(request)


# --- the server ---------------------------------------------------------------------------------

Entry = Any  # an answer dict, a misbehaviour, or a callable returning one
Script = list[Entry] | Callable[[Any], Entry] | None


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    fake: FakeRunner


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        self.server.fake._serve(self.request)


class FakeRunner:
    """`with FakeRunner(script) as runner:` ... `cad_engine.replay(..., address=runner.address)`."""

    def __init__(self, script: Script = None) -> None:
        self.script: Script = script
        self.requests: list[Any] = []
        self.connections = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        assert self._server is not None, "the runner is not started"
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def __enter__(self) -> FakeRunner:
        server = _Server(("127.0.0.1", 0), _Handler)
        server.fake = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _next(self, request: Any) -> Entry:
        with self._lock:
            script = self.script
            if isinstance(script, list):
                entry = script.pop(0) if script else None
            elif callable(script):
                entry = script
            else:
                entry = None
        if entry is None:
            return answer_for(request)
        while callable(entry):
            entry = entry(request)
        return entry

    def _serve(self, sock: socket.socket) -> None:
        sock.settimeout(10)
        line = _read_line(sock)
        if line is None:
            return
        try:
            request: Any = json.loads(line)
        except ValueError:
            request = line
        with self._lock:
            self.requests.append(request)
            self.connections += 1
        entry = self._next(request)
        with contextlib.suppress(OSError):  # the client went away first
            self._answer(sock, entry)

    def _answer(self, sock: socket.socket, entry: Entry) -> None:
        if entry is NEVER_ANSWER:
            self._stop.wait(60)
        elif entry is CLOSE_EARLY:
            sock.sendall(b'{"v":1,"ok":tr')
        elif entry is OVERLONG:
            sock.sendall(b'{"v":1,"x":"' + b"a" * (8 * 1024 * 1024 + 16) + b'"}\n')
        elif entry is NOT_JSON:
            sock.sendall(b"this is not json\n")
        elif isinstance(entry, Raw):
            sock.sendall(entry.data)
        elif isinstance(entry, HoldOpen):
            sock.sendall(entry.data)
            self._stop.wait(60)
        elif isinstance(entry, Trickle):
            for i in range(len(entry.data)):
                if self._stop.wait(entry.interval):
                    return
                sock.sendall(entry.data[i : i + 1])
        else:
            sock.sendall(json.dumps(entry).encode("utf-8") + b"\n")
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)


def _read_line(sock: socket.socket) -> bytes | None:
    buf = b""
    while b"\n" not in buf:
        if len(buf) > REQUEST_LINE_MAX:
            return None
        try:
            got = sock.recv(65536)
        except OSError:
            return None
        if not got:
            return None
        buf += got
    return buf.split(b"\n", 1)[0]
