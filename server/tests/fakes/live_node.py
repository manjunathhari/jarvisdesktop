"""A one-off live run of the fake node against a real gateway (Step 6; runbook §8.6).

From `server/`, on one of Manj's devices on the tailnet, with a fresh enrolment code:

    uv run python -m tests.fakes.live_node --gateway <jarvis-gw tailnet address> --code <code>

It enrols a new node with the code (§7.2: the gateway key is pinned by the code's fingerprint,
the core key by `welcome.enrolment`), then reconnects with `hello`, the path-A handshake a
deployed desktop makes every time it connects (§6.1). Then it sends one fictional utterance per
turn (`--repeat`, default 1), and prints for each the time from sending `utterance` to the
first SPEECH `reply.chunk` and to the final chunk, against the 3 s target (§6.2). Every gateway
message goes through `FakeNode`'s node-side checks (`node_rules`: challenge, welcome, seq,
signatures, node actions). Each reply chunk is acknowledged (`received`, as the desktop does);
outbox items are not acknowledged, so a real device still sees them. Then it closes and exits.

It prints the node ID and timings, never a key or the code. The node's key exists only in
memory, so the node can't be used again: revoke it afterwards (`jarvis-core node revoke`).
Fictional phrases only (§5.7): what is said is stored on the server.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .fake_node import FakeNode

PORT = 8443
TARGET_S = 3.0  # §6.2: the first spoken sentence within 3 s
PHRASES = (
    "What is the capital of the fictional country of Freedonia?",
    "Tell me one short fact about the fictional planet Zorbon.",
    "What colour is the fictional Blue Fern Lake?",
    "Name a fictional river in the land of Pellucidar.",
    "How many moons does the fictional planet Arrakis have?",
)
MAX_FRAME = 1 << 20


class LiveSocket:
    """The subset of a Starlette test WebSocket that `FakeNode` uses, over a real socket."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    def send_text(self, text: str) -> None:
        self._ws.send(text)

    def next_message(self, timeout: float) -> dict[str, Any]:
        """The next frame as an ASGI-style message; TimeoutError if none in `timeout`."""
        try:
            data = self._ws.recv(timeout=timeout)
        except ConnectionClosed as e:
            return {"type": "websocket.close", "code": e.rcvd.code if e.rcvd else 1006}
        text = data if isinstance(data, str) else data.decode("utf-8")
        return {"type": "websocket.send", "text": text}


class LiveClient:
    """Stands in for the TestClient: `websocket_connect` opens a real WebSocket to `base`
    (`ws://host:port`). Headers are ignored: the gateway takes the peer's identity from the
    tailnet itself."""

    def __init__(self, base: str) -> None:
        self.base = base

    @contextlib.contextmanager
    def websocket_connect(
        self, route: str, headers: dict[str, str] | None = None
    ) -> Iterator[LiveSocket]:
        with connect(self.base + route, open_timeout=10, max_size=MAX_FRAME) as ws:
            yield LiveSocket(ws)


@dataclass(frozen=True)
class Timing:
    first_chunk_s: float
    final_s: float
    chunks: int
    reply: str


def run_turn(
    node: FakeNode,
    phrase: str,
    *,
    timeout: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
) -> Timing:
    """Send one utterance; wait for its final reply chunk; return the timings."""
    node.send("utterance", {"text": phrase, "screen_summary": ""})
    start = clock()
    deadline = start + timeout
    first: float | None = None
    spoken: list[str] = []
    chunks = 0
    while True:
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError(f"no final reply.chunk within {timeout:.0f} s")
        try:
            env = node.recv(min(left, 1.0))
        except TimeoutError:
            continue
        if env["kind"] != "reply.chunk":
            continue
        chunk = env["payload"]
        chunks += 1
        if chunk["type"] == "SPEECH":
            if first is None:
                first = clock() - start
            spoken.append(chunk["text"] or "")
        if chunk["final"]:
            final = clock() - start
            return Timing(first if first is not None else final, final, chunks, "".join(spoken))


def run(
    node: FakeNode,
    code: str,
    phrases: Sequence[str],
    *,
    timeout: float = 30.0,
    out: Callable[[str], None] = print,
) -> list[Timing]:
    """Enrol `node` with `code`, reconnect with `hello` (path A), run one turn per phrase,
    close. Prints progress via `out`."""
    try:
        node_id = node.enrol(code)
        out(f"enrolled node {node_id}")
        node.reconnect()  # a new connection: challenge, hello, welcome
        out("connected again with hello (path A)")
        node.sync()
        timings: list[Timing] = []
        for i, phrase in enumerate(phrases, 1):
            t = run_turn(node, phrase, timeout=timeout)
            verdict = "OK" if t.first_chunk_s <= TARGET_S else "MISSED"
            out(
                f"turn {i}: first reply.chunk {t.first_chunk_s:.3f} s, final {t.final_s:.3f} s,"
                f" {t.chunks} chunks (target {TARGET_S:.0f} s: {verdict})"
            )
            timings.append(t)
        if timings:
            worst = max(t.first_chunk_s for t in timings)
            out(f"worst first reply.chunk {worst:.3f} s over {len(timings)} turn(s)")
        return timings
    finally:
        node.close()


def _base(gateway: str) -> str:
    host, _, port = gateway.partition(":")
    if not host:
        raise SystemExit("--gateway needs the gateway's tailnet address")
    return f"ws://{host}:{int(port or PORT)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_node", description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", required=True, help="tailnet address[:port] of jarvis-gw")
    parser.add_argument("--code", required=True, help="a fresh enrolment code (never printed)")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, len(PHRASES) + 1))
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per turn")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        node = FakeNode(
            LiveClient(_base(args.gateway)),  # type: ignore[arg-type]
            key=Ed25519PrivateKey.generate(),  # in memory only, never written or printed
            node_id=None,
            peer_ip="",
            state_path=Path(tmp) / "node.json",
        )
        try:
            run(node, args.code, PHRASES[: args.repeat], timeout=args.timeout)
        except Exception as e:  # report the failure plainly; nothing secret is in it
            print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
