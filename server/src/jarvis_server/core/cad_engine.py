"""The server side of the CAD engine (CAD board slice 1, Task 3; plan
`.10x/plans/2026-11-28-cad-board-slice-1.md`).

- `step_from` turns one `cad_step` tool call into a `Step`, or refuses it (`BadStep`) with a
  reason Claude can act on.
- `replay` and `catalogue` ask the runner in the `cad` container (Task 2's protocol: one JSON line
  out, one line in, over plain TCP). The runner, and everything it returns, is untrusted: it
  carries the output of third-party CAD code. Every field of an answer is checked for type and
  size before use; anything unexpected is `EngineDown`. The whole exchange has one deadline,
  which a peer sending a byte at a time cannot extend. Nothing of a request or an answer is
  logged: only codes and sizes.
- `check_svg` is the picture check (Review Focus 2): the picture later reaches the Board page as
  an image, so only the shapes the engine is known to draw pass. Byte checks come first (they
  are what keeps entities, DTDs and other encodings away from the parser); then the standard
  library's parser and a walk of every element against allow-lists.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from jarvis_server import contracts as c

log = logging.getLogger("jarvis.core.cad_engine")

# server/config/cad_commands.txt (in the image: /app/config/cad_commands.txt), as prices.toml.
COMMANDS_FILE = Path(__file__).resolve().parents[3] / "config" / "cad_commands.txt"
_COMMAND_ID = re.compile(r"[a-z0-9._]{1,40}")


def _load_allowed(path: Path) -> frozenset[str]:
    """The runner's rule: one id per line, blank lines skipped, anything else is an error."""
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if not _COMMAND_ID.fullmatch(line):
            raise RuntimeError(f"{path.name}: not a command id")
        ids.add(line)
    return frozenset(ids)


ALLOWED: frozenset[str] = _load_allowed(COMMANDS_FILE)
STEP_MAX = 400  # commands in one step
RECIPE_MAX = 2000  # commands in a whole recipe (the runner's limit)
STEP_PARAMS_MAX_BYTES = 200_000  # JSON of one step's params, all commands together
PARAMS_DEPTH_MAX = 32  # nesting of one command's params (the runner's parser stops at 128)

DEFAULT_ADDRESS = "cad:7411"
CONNECT_TIMEOUT = 3  # seconds
EXCHANGE_TIMEOUT = 20  # seconds, the whole exchange: the runner may wait 5 s, then replay 10 s
ANSWER_MAX_BYTES = 8 * 1024 * 1024
REQUEST_MAX_BYTES = 1_000_000  # under the runner's 1,048,576, with room to spare
# The shape of an answer line, bounded before it is parsed (outside strings): a replay of 2,000
# commands has about 2,006 objects and lists and about 10,200 commas.
ANSWER_CONTAINERS_MAX = 5_000
ANSWER_COMMAS_MAX = 20_000
# A good answer for 2,000 commands has about 28,000 quotes.
ANSWER_QUOTES_MAX = 100_000
HOST_MAX = 253

RESULT_TEXT_MAX = 300
TEXTS_MAX = 80
TEXT_MAX = 80
COUNT_KINDS_MAX = 64
COUNT_NAME_MAX = 40
ITEMS_MAX = 100_000
NOTE_MAX = 2_000  # a catalogue parameter note (the runner cuts it to this)
PNG_B64_MAX = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SVG_B64_MAX = (c.BOARD_PICTURE_MAX_BYTES + 2) // 3 * 4

_ENGINE = re.compile(r"[a-z0-9-]{1,30}")
_ENGINE_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,30}")
# One line: no C0, DEL or C1 control character, no Unicode line or paragraph separator.
# A lone surrogate (which JSON's `\\udXXX` escapes can carry) cannot be encoded as UTF-8 later.
_NOT_ONE_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
_LABEL: TypeAdapter[str] = TypeAdapter(c.StepLabel)
_STRING = re.compile(rb'"[^"]*"')  # after escapes are removed: linear, never backtracks far

_REFUSED = frozenset({"too_many_commands", "too_large", "not_allowed", "timeout"})
_REPLAY_KEYS = frozenset(
    {"v", "ok", "engine", "engine_version", "results", "failed", "counts", "items", "extents",
     "texts", "png_b64", "svg_b64"}
)  # fmt: skip
_CATALOGUE_KEYS = frozenset({"v", "ok", "engine_version", "commands"})
_FAILURE_KEYS = frozenset({"v", "ok", "error"})
_RESULT_KEYS = frozenset({"step", "n", "command", "ok", "text"})


class BadStep(ValueError):
    """A `cad_step` that cannot be replayed; `reason` says why, in words for Claude."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EngineDown(Exception):
    """No answer that can be used: no connection, the deadline passed, the runner was busy or its
    engine failed, or the answer was malformed. The message is a fixed phrase, never content."""


class EngineRefused(Exception):
    """The recipe was refused: `code` is the runner's `too_many_commands`, `too_large` (the
    picture or the drawing came out too large), `not_allowed` or `timeout`, or the client's own
    `request_too_large` (the recipe's request line would pass `REQUEST_MAX_BYTES`; Task 5 answers
    it as an error result telling Claude the recipe is too long)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BadPicture(ValueError):
    """A picture that must not reach the Board. `reason` is a fixed code, never text from the
    picture: `type`, `size`, `encoding`, `markup`, `xml`, `element`, `attribute`, `value`,
    `text`, `depth` or `count`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Step:
    """One step of a recipe: a label and its commands, each `{"command": id, "params": {...}}`."""

    label: str
    commands: tuple[dict[str, Any], ...]

    def wire(self) -> dict[str, Any]:
        return {"label": self.label, "commands": list(self.commands)}


@dataclass(frozen=True)
class CommandResult:
    step: int
    n: int
    command: str
    ok: bool
    text: str


@dataclass(frozen=True)
class Replay:
    results: tuple[CommandResult, ...]
    failed: int
    counts: dict[str, int]
    items: int
    extents: tuple[float, float] | None
    texts: tuple[str, ...]
    png: bytes | None
    svg: bytes | None
    engine: str
    engine_version: str


# --- steps ---------------------------------------------------------------------------------------


def _depth_ok(value: Any, limit: int) -> bool:
    """True if `value` nests at most `limit` objects or lists deep (no recursion)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > limit:
                return False
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def step_from(args: dict[str, Any]) -> Step:
    """The step in a `cad_step` tool call's input (`{label, commands}`), as it came from Claude."""
    if not isinstance(args, dict):
        raise BadStep("a step is an object with a label and commands")
    if set(args) - {"label", "commands"}:
        raise BadStep("a step has only a label and commands")
    label = args.get("label")
    try:
        _LABEL.validate_python(label)
    except ValidationError:
        raise BadStep(
            f"the label must be one line of 1 to {c.DRAWING_STEP_LABEL_MAX} characters"
        ) from None
    assert isinstance(label, str)
    try:
        label.encode("utf-8")
    except UnicodeEncodeError:
        raise BadStep("the label is not valid text") from None
    commands = args.get("commands")
    if not isinstance(commands, list) or not commands:
        raise BadStep("a step needs a list of at least one command")
    if len(commands) > STEP_MAX:
        raise BadStep(f"a step has at most {STEP_MAX} commands; this one has {len(commands)}")
    kept: list[dict[str, Any]] = []
    size = 0
    for i, cmd in enumerate(commands, start=1):
        if not (isinstance(cmd, dict) and set(cmd) == {"command", "params"}):
            raise BadStep(f"command {i} must be an object of exactly `command` and `params`")
        name, params = cmd["command"], cmd["params"]
        if not isinstance(name, str) or name not in ALLOWED:
            raise BadStep(f"command {i} is not one of the allowed commands")
        if not isinstance(params, dict):
            raise BadStep(f"the params of command {i} must be an object")
        if not _depth_ok(params, PARAMS_DEPTH_MAX):
            raise BadStep(f"the params of command {i} are nested too deep")
        try:
            text = _json(params)
            size += len(text.encode("utf-8"))
        except (TypeError, ValueError, UnicodeEncodeError):
            raise BadStep(f"the params of command {i} are not plain JSON") from None
        if size > STEP_PARAMS_MAX_BYTES:
            raise BadStep("the params of one step are at most 200,000 bytes of JSON in all")
        kept.append({"command": name, "params": json.loads(text)})  # a copy of its own
    return Step(label, tuple(kept))


# --- the runner ----------------------------------------------------------------------------------


def address() -> str:
    """The runner's `host:port`: env `JARVIS_CAD_ADDR`, else `cad:7411`."""
    return os.environ.get("JARVIS_CAD_ADDR", DEFAULT_ADDRESS)


def _host_port(addr: str) -> tuple[str, int]:
    """`host:port` (`[v6]:port` too). Anything else, or a host that is over-long or cannot be
    looked up as a name, is `EngineDown`."""
    host, sep, port = addr.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not (sep and host and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise EngineDown("the engine address is not host:port")
    try:
        ok = len(host) <= HOST_MAX and len(host.encode("idna")) <= HOST_MAX
    except UnicodeError:
        ok = False
    if not ok:
        raise EngineDown("the engine address is not host:port")
    return host, int(port)


def _exchange(request: dict[str, Any], addr: str | None, op: str) -> Any:
    """Send one request line, read one answer line, and return it parsed (not yet checked).

    The answer is read up to its first newline, and no further: the runner closes after it, but
    the client does not wait for that. Bytes after the newline, in what was read, are
    `EngineDown`. One deadline covers connecting, sending and reading; the name lookup inside
    `socket.create_connection` is not bounded by it (the standard library has no timeout for
    `getaddrinfo` short of a thread; the runner's name is resolved by Docker's local DNS)."""
    line = _json(request).encode("utf-8") + b"\n"
    host, port = _host_port(addr if addr is not None else address())
    deadline = time.monotonic() + EXCHANGE_TIMEOUT
    buf = bytearray()
    try:
        with socket.create_connection(
            (host, port), timeout=min(CONNECT_TIMEOUT, EXCHANGE_TIMEOUT)
        ) as sock:
            _settimeout(sock, deadline)
            sock.sendall(line)  # the timeout bounds the whole send (Python 3.5 and later)
            while True:
                _settimeout(sock, deadline)
                got = sock.recv(min(65536, ANSWER_MAX_BYTES + 2 - len(buf)))
                if not got:
                    break
                buf += got
                if b"\n" in got:
                    break
                if len(buf) > ANSWER_MAX_BYTES + 1:
                    log.warning("cad %s: answer over %d bytes", op, ANSWER_MAX_BYTES)
                    raise EngineDown("the engine's answer is too long")
    except TimeoutError:
        log.warning("cad %s: no answer within %s s", op, EXCHANGE_TIMEOUT)
        raise EngineDown("the engine did not answer in time") from None
    except (OSError, UnicodeError) as e:
        log.warning("cad %s: connection failed (%s)", op, type(e).__name__)
        raise EngineDown("no connection to the engine") from None
    log.info("cad %s: sent %d bytes, got %d bytes", op, len(line), len(buf))
    # Exactly one line, ended by the first newline, within the limit.
    if buf.find(b"\n") != len(buf) - 1 or len(buf) > ANSWER_MAX_BYTES + 1:
        raise EngineDown("the engine's answer is not one line")
    raw = bytes(buf)
    del buf
    _check_shape(raw, op)
    try:
        return json.loads(
            raw[:-1].decode("utf-8"),
            parse_constant=_no_constant,
            object_pairs_hook=_no_repeats,
        )
    except (ValueError, RecursionError):
        raise EngineDown("the engine's answer is not JSON") from None


def _check_shape(raw: bytes, op: str) -> None:
    """Refuse, before parsing, a line with more objects, lists or commas outside strings than any
    good answer has: parsing millions of tiny values would cost hundreds of megabytes. Escapes
    go first (`\\\\` pairs, then `\\"`), so every `"` left opens or closes a string."""
    plain = raw.replace(b"\\\\", b"").replace(b'\\"', b"")
    # Millions of empty strings would make the substitution below build a piece for each one.
    quotes = plain.count(b'"')
    if quotes > ANSWER_QUOTES_MAX:
        log.warning("cad %s: answer of %d quotes", op, quotes)
        raise EngineDown("the engine's answer is malformed")
    outside = _STRING.sub(b"", plain)
    del plain
    containers = outside.count(b"{") + outside.count(b"[")
    commas = outside.count(b",")
    if containers > ANSWER_CONTAINERS_MAX or commas > ANSWER_COMMAS_MAX:
        log.warning("cad %s: answer of %d containers and %d commas", op, containers, commas)
        raise EngineDown("the engine's answer is malformed")


def _settimeout(sock: socket.socket, deadline: float) -> None:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    sock.settimeout(left)


def _no_constant(_name: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("a repeated key")
    return out


def _malformed(op: str, what: str) -> EngineDown:
    log.warning("cad %s: malformed answer (%s)", op, what)
    return EngineDown("the engine's answer is malformed")


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _line(value: Any, most: int, *, least: int = 0) -> bool:
    return (
        isinstance(value, str)
        and least <= len(value) <= most
        and _NOT_ONE_LINE.search(value) is None
    )


def _failure_or_none(answer: Any, op: str) -> None:
    """Raise for a failure answer (or one that is no answer); return for `ok: true`."""
    if not isinstance(answer, dict) or answer.get("v") != 1 or not _whole(answer.get("v")):
        raise _malformed(op, "version")
    ok = answer.get("ok")
    if ok is True:
        return
    if ok is not False or set(answer) != _FAILURE_KEYS or not isinstance(answer["error"], str):
        raise _malformed(op, "failure shape")
    code = answer["error"]
    if code in _REFUSED:
        log.info("cad %s: refused (%s)", op, code)
        raise EngineRefused(code)
    known = code in {"busy", "engine_failed", "bad_request"}
    log.warning("cad %s: engine down (%s)", op, code if known else "unknown code")
    raise EngineDown(code if known else "the engine failed")


def _b64(value: str, most_chars: int) -> bytes | None:
    if len(value) > most_chars or not value.isascii():
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def replay(steps: Sequence[Step], *, picture: bool, address: str | None = None) -> Replay:
    """Replay the whole recipe in a fresh engine. `picture`: ask for the SVG as well (it comes
    only when no command failed). Raises `EngineRefused` before connecting for a recipe over
    `RECIPE_MAX` commands (`too_many_commands`) or a request line over `REQUEST_MAX_BYTES`
    (`request_too_large`); otherwise as the runner answers."""
    sent = [(s, n, cmd["command"]) for s, step in enumerate(steps, 1)
            for n, cmd in enumerate(step.commands, 1)]  # fmt: skip
    if len(sent) > RECIPE_MAX:
        raise EngineRefused("too_many_commands")
    request = {"v": 1, "op": "replay", "steps": [s.wire() for s in steps], "picture": picture}
    if len(_json(request).encode("utf-8")) + 1 > REQUEST_MAX_BYTES:
        raise EngineRefused("request_too_large")
    answer = _exchange(request, address, "replay")
    _failure_or_none(answer, "replay")
    return _check_replay(answer, sent, picture)


def _check_replay(a: dict[str, Any], sent: list[tuple[int, int, str]], picture: bool) -> Replay:
    op = "replay"
    if set(a) != _REPLAY_KEYS:
        raise _malformed(op, "fields")
    engine, version = a["engine"], a["engine_version"]
    if not (isinstance(engine, str) and _ENGINE.fullmatch(engine)):
        raise _malformed(op, "engine")
    if not (isinstance(version, str) and _ENGINE_VERSION.fullmatch(version)):
        raise _malformed(op, "engine_version")

    raw = a["results"]
    if not isinstance(raw, list) or len(raw) != len(sent):
        raise _malformed(op, "results")
    results = []
    for r, (s, n, command) in zip(raw, sent, strict=True):
        if not (isinstance(r, dict) and set(r) == _RESULT_KEYS):
            raise _malformed(op, "result fields")
        if not (_whole(r["step"]) and _whole(r["n"]) and (r["step"], r["n"]) == (s, n)):
            raise _malformed(op, "result order")
        if r["command"] != command or not isinstance(r["ok"], bool):
            raise _malformed(op, "result command")
        if not _line(r["text"], RESULT_TEXT_MAX):
            raise _malformed(op, "result text")
        results.append(CommandResult(s, n, command, r["ok"], r["text"]))
    failed = a["failed"]
    if not _whole(failed) or failed != sum(1 for r in results if not r.ok):
        raise _malformed(op, "failed")

    counts, items = a["counts"], a["items"]
    if not isinstance(counts, dict) or len(counts) > COUNT_KINDS_MAX:
        raise _malformed(op, "counts")
    for name, n in counts.items():
        if not (_line(name, COUNT_NAME_MAX, least=1) and _whole(n) and 0 <= n <= ITEMS_MAX):
            raise _malformed(op, "counts")
    if not (_whole(items) and 0 <= items <= ITEMS_MAX and items == sum(counts.values())):
        raise _malformed(op, "items")

    extents: tuple[float, float] | None = None
    if a["extents"] is not None:
        e = a["extents"]
        if not (isinstance(e, list) and len(e) == 2):
            raise _malformed(op, "extents")
        if not all(
            isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
            for x in e
        ):
            raise _malformed(op, "extents")
        extents = (float(e[0]), float(e[1]))

    texts = a["texts"]
    if not (isinstance(texts, list) and len(texts) <= TEXTS_MAX):
        raise _malformed(op, "texts")
    if not all(_line(t, TEXT_MAX) for t in texts):
        raise _malformed(op, "texts")

    png: bytes | None = None
    if a["png_b64"] is not None:
        if not isinstance(a["png_b64"], str):
            raise _malformed(op, "png")
        png = _b64(a["png_b64"], PNG_B64_MAX)
        if png is None or not png.startswith(PNG_SIGNATURE):
            raise _malformed(op, "png")

    svg: bytes | None = None
    wanted = picture and failed == 0
    if (a["svg_b64"] is not None) != wanted:
        raise _malformed(op, "svg presence")
    if a["svg_b64"] is not None:
        if not isinstance(a["svg_b64"], str):
            raise _malformed(op, "svg")
        svg = _b64(a["svg_b64"], _SVG_B64_MAX)
        if svg is None or not 1 <= len(svg) <= c.BOARD_PICTURE_MAX_BYTES:
            raise _malformed(op, "svg")

    log.info(
        "cad replay: %d commands, %d failed, %d items, png %d bytes, svg %d bytes",
        len(results), failed, items, len(png or b""), len(svg or b""),
    )  # fmt: skip
    return Replay(
        results=tuple(results), failed=failed, counts=dict(counts), items=items,
        extents=extents, texts=tuple(texts), png=png, svg=svg, engine=engine,
        engine_version=version,
    )  # fmt: skip


def catalogue(address: str | None = None) -> dict[str, str]:
    """The engine's parameter note for each allowed command it knows. Every failure is
    `EngineDown` here: the refusals are about a recipe, and a catalogue has none."""
    answer = _exchange({"v": 1, "op": "catalogue"}, address, "catalogue")
    try:
        _failure_or_none(answer, "catalogue")
    except EngineRefused as e:
        raise EngineDown(e.code) from None
    op = "catalogue"
    if set(answer) != _CATALOGUE_KEYS:
        raise _malformed(op, "fields")
    version = answer["engine_version"]
    if not (isinstance(version, str) and _ENGINE_VERSION.fullmatch(version)):
        raise _malformed(op, "engine_version")
    commands = answer["commands"]
    if not isinstance(commands, dict) or len(commands) > len(ALLOWED):
        raise _malformed(op, "commands")
    for name, note in commands.items():
        if name not in ALLOWED or not _line(note, NOTE_MAX):
            raise _malformed(op, "commands")
    log.info("cad catalogue: %d commands", len(commands))
    return dict(commands)


# --- the picture check ---------------------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"
SVG_ELEMENTS = frozenset(
    {"svg", "g", "rect", "line", "polyline", "polygon", "path", "circle", "ellipse"}
)
SVG_ATTRIBUTES = frozenset(
    {"xmlns", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r",
     "rx", "ry", "d", "points", "fill", "stroke", "stroke-width", "stroke-linecap",
     "stroke-linejoin", "stroke-dasharray", "transform", "opacity", "fill-opacity",
     "stroke-opacity"}
)  # fmt: skip
SVG_DEPTH_MAX = 32  # the root is level 1
SVG_ELEMENTS_MAX = 400_000
SVG_LONG_VALUE_MAX = 1_000_000  # `d` and `points`: a whole outline may be one path
SVG_VALUE_MAX = 200  # every other attribute
_LONG_VALUES = frozenset({"d", "points"})
# A leading XML declaration that names UTF-8 or no encoding.
_XML_DECL = re.compile(
    rb"<\?xml[ \t\r\n]+version[ \t\r\n]*=[ \t\r\n]*(\"1\.[0-9]+\"|'1\.[0-9]+')"
    rb"(?:[ \t\r\n]+encoding[ \t\r\n]*=[ \t\r\n]*(\"[Uu][Tt][Ff]-8\"|'[Uu][Tt][Ff]-8'))?"
    rb"(?:[ \t\r\n]+standalone[ \t\r\n]*=[ \t\r\n]*(\"(?:yes|no)\"|'(?:yes|no)'))?"
    rb"[ \t\r\n]*\?>"
)
_BOMS = (b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe", b"\x00\x00\xfe\xff")
_BAD_IN_VALUE = ("url(", "javascript:", "data:", "&#", "\\")
_XML_SPACE = " \t\r\n"


def check_svg(svg: bytes) -> None:
    """Raise `BadPicture` unless this is a picture of plain shapes, safe to show as an image."""
    if not isinstance(svg, bytes):
        raise BadPicture("type")
    if not 1 <= len(svg) <= c.BOARD_PICTURE_MAX_BYTES:
        raise BadPicture("size")
    # Bytes first: no other encoding, no markup declaration, no entity, no processing instruction.
    if svg.startswith(_BOMS) or b"\x00" in svg:
        raise BadPicture("encoding")
    try:
        svg.decode("utf-8")
    except UnicodeDecodeError:
        raise BadPicture("encoding") from None
    if b"<!" in svg:  # DOCTYPE, ENTITY, CDATA, comments
        raise BadPicture("markup")
    body = svg
    if svg.startswith(b"<?"):
        decl = _XML_DECL.match(svg)
        if decl is None:  # an XML declaration other than a version and UTF-8
            raise BadPicture("markup")
        body = svg[decl.end() :]
    if b"<?" in body:  # a processing instruction
        raise BadPicture("markup")
    if b"&" in svg:  # an entity or character reference
        raise BadPicture("markup")
    if b"xmlns:" in svg:  # a namespace other than SVG's
        raise BadPicture("markup")
    _walk(svg)


def _walk(svg: bytes) -> None:
    """Parse in pieces and check each element as it starts and ends: an early refusal stops the
    parse. Depth is counted, never recursed. Memory stays flat: an element is let go of once its
    tail is known (when its next sibling starts, or its parent ends)."""
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("start", "end"))
    walk = _Walk()
    step = 65_536
    try:
        for at in range(0, len(svg), step):
            parser.feed(svg[at : at + step])
            walk.events(parser)
        parser.close()
        walk.events(parser)
    except ET.ParseError:
        raise BadPicture("xml") from None


class _Walk:
    """The open elements, root first, each with how many of its children have started and are
    still attached. When a child starts, the tail of the one before it is complete and checked;
    every `_LET_GO` children, those before the newest are detached in one go (a deletion at the
    front of a list moves the rest, so it is done in batches)."""

    def __init__(self) -> None:
        self.open: list[list[Any]] = []  # [element, children started and still attached]
        self.seen = 0

    def events(self, parser: ET.XMLPullParser[ET.Element]) -> None:
        for event, elem in cast(Iterator[tuple[str, ET.Element]], parser.read_events()):
            if event == "start":
                self.seen += 1
                if len(self.open) + 1 > SVG_DEPTH_MAX:
                    raise BadPicture("depth")
                if self.seen > SVG_ELEMENTS_MAX:
                    raise BadPicture("count")
                _check_element(elem, root=self.seen == 1)
                if self.open:
                    self._started_in(self.open[-1])
                self.open.append([elem, 0])
            else:
                self.open.pop()
                if not _blank(elem.text):
                    raise BadPicture("text")
                for child in elem:
                    if not _blank(child.tail):
                        raise BadPicture("text")
                del elem[:]

    @staticmethod
    def _started_in(entry: list[Any]) -> None:
        parent, attached = entry
        if attached and not _blank(parent[attached - 1].tail):
            raise BadPicture("text")
        attached += 1
        if attached >= _LET_GO:
            del parent[: attached - 1]  # all but the one that just started
            attached = 1
        entry[1] = attached


_LET_GO = 1024


def _blank(text: str | None) -> bool:
    return text is None or text.strip(_XML_SPACE) == ""


def _check_element(elem: ET.Element, *, root: bool) -> None:
    ns, _, name = elem.tag.rpartition("}")
    if ns != "{" + SVG_NS or name not in SVG_ELEMENTS or (root and name != "svg"):
        raise BadPicture("element")
    for attr, value in elem.attrib.items():
        if attr.startswith("{") or attr not in SVG_ATTRIBUTES:
            raise BadPicture("attribute")
        most = SVG_LONG_VALUE_MAX if attr in _LONG_VALUES else SVG_VALUE_MAX
        if len(value) > most or _NOT_ONE_LINE.search(value):
            raise BadPicture("value")
        squeezed = "".join(value.lower().split())
        if any(bad in squeezed for bad in _BAD_IN_VALUE):
            raise BadPicture("value")
