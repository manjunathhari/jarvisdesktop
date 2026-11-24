"""The live fake-node runner (`tests/fakes/live_node.py`; runbook §8.6; Task 14 review M9).

Driven twice against a gateway in this process: through the TestClient (as every gateway test
does), and over a real WebSocket to the gateway served by uvicorn on 127.0.0.1, which is how
it runs against the deployed gateway. Core's side runs in a thread: each queued `conv.turn` is
dispatched with a fake brain. Every person and phrase is fictional.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server.brain import Speech
from jarvis_server.core import dispatch, turn
from jarvis_server.gateway.app import create_app
from jarvis_server.memory import checks
from jarvis_server.memory.engine import SYSTEM, MemoryEngine
from jarvis_server.signing import fingerprint, public_bytes

from .conftest import Dsns, fresh_database
from .fakes import live_node
from .fakes.fake_node import FakeWhoIs
from .helpers import flow
from .test_gateway_handshake import MANJ, FakeClock, Gw, install_trusted_keys, running_gateway
from .test_turn import Brains

REPLY = "Freedonia's capital is the fictional city of Fredville."


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[MemoryEngine]:
    directory = checks.StaticDirectory(people={MANJ: flow.HOUSEHOLD})
    eng = MemoryEngine(tmp_path / "memory", directory, checks.ListAudit(), flow.HOUSEHOLD).open()
    eng.add_person("manj", MANJ, "Manj (fictional)", by=SYSTEM)
    turn.set_memory(eng)
    turn.set_brain(Brains(*[[Speech("Freedonia's capital "), Speech("is the fictional city"),
                             Speech(" of Fredville.")] for _ in range(5)]))  # fmt: skip
    yield eng
    turn.set_memory(None)
    turn.set_brain(None)


class Core:
    """Core's job loop for `conv.turn` only, in a thread, until stopped."""

    def __init__(self, dsn: str) -> None:
        self.ran = 0
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(dsn,), daemon=True)
        self._thread.start()

    def _loop(self, dsn: str) -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                while not self._stop.is_set():
                    row = conn.execute(
                        "SELECT id FROM ops.job WHERE kind = 'conv.turn' AND status = 'QUEUED'"
                        " AND run_at <= now() ORDER BY id LIMIT 1"
                    ).fetchone()
                    if row is None:
                        time.sleep(0.02)
                        continue
                    dispatch.handle(conn, flow.claim(conn, int(row[0])))
                    self.ran += 1
        except BaseException as e:
            self.error = e

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(10)
        if self.error is not None:
            raise self.error


def _code(dsns: Dsns, core: Ed25519PrivateKey, gw: Ed25519PrivateKey) -> str:
    from jarvis_server import nodes

    with psycopg.connect(dsns.app, autocommit=True) as conn:
        return nodes.create_enrolment(
            conn, person_id=MANJ, kind="DESKTOP", name="live check pc",
            core_fp=fingerprint(public_bytes(core)), gw_fp=fingerprint(public_bytes(gw)),
        )  # fmt: skip


def _check(timings: list[live_node.Timing], turns: int) -> None:
    assert len(timings) == turns
    for t in timings:
        assert 0 < t.first_chunk_s <= t.final_s and t.chunks >= 2
        assert t.reply == REPLY


def test_runner_against_the_in_process_gateway(
    dsns: Dsns, tmp_path: Path, memory: MemoryEngine, core_key: Ed25519PrivateKey
) -> None:
    """Through the TestClient: enrol, utterance, every reply chunk checked and acknowledged."""
    with running_gateway(dsns, tmp_path, core_key=core_key, clock=FakeClock(datetime.now(UTC)),
                         tick_interval=0.02) as gw:  # fmt: skip
        g: Gw = gw
        node = g.node()
        lines: list[str] = []
        core = Core(dsns.app)
        try:
            timings = live_node.run(node, g.code(), live_node.PHRASES[:2], out=lines.append)
        finally:
            core.stop()
    _check(timings, 2)
    assert core.ran == 2 and not node.connected and node.rejected == []
    assert lines[0] == f"enrolled node {node.node_id}"
    assert lines[1].startswith("turn 1: first reply.chunk ") and lines[-1].startswith("worst")
    with psycopg.connect(dsns.app, autocommit=True) as conn:  # every chunk acknowledged
        row = conn.execute(
            "SELECT count(*) FROM conv.stream_cursor WHERE node_id = %s", (node.node_id,)
        ).fetchone()
    assert row == (2,)


@pytest.fixture
def served(dsns: Dsns, tmp_path: Path, core_key: Ed25519PrivateKey) -> Iterator[dict[str, Any]]:
    """The gateway app served by uvicorn on 127.0.0.1 (a free port), its peer mapped in whois."""
    gw_key = Ed25519PrivateKey.generate()
    secrets_dir = tmp_path / "gw-secrets"
    secrets_dir.mkdir()
    (secrets_dir / f"gateway_signing_key_{fingerprint(public_bytes(gw_key))}").write_bytes(
        gw_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    )  # fmt: skip
    install_trusted_keys(dsns, core=public_bytes(core_key), gateway=public_bytes(gw_key))
    app = create_app(
        dsn=dsns.gateway, whois=FakeWhoIs({"127.0.0.1": f"ts:pc-{secrets.token_hex(6)}"}),
        secrets_dir=secrets_dir, bind_check=False, tick_interval=0.02,
    )  # fmt: skip
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "uvicorn did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield {"port": port, "gw_key": gw_key}
    finally:
        server.should_exit = True
        thread.join(15)


def test_runner_cli_over_a_real_websocket(
    dsns: Dsns, memory: MemoryEngine, core_key: Ed25519PrivateKey, served: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """`python -m tests.fakes.live_node --gateway 127.0.0.1:<port> --code <code> --repeat 3`:
    a real socket, three turns, the timings printed, and no key or code on its output."""
    code = _code(dsns, core_key, served["gw_key"])
    core = Core(dsns.app)
    try:
        status = live_node.main(
            ["--gateway", f"127.0.0.1:{served['port']}", "--code", code, "--repeat", "3"]
        )
    finally:
        core.stop()
    captured = capsys.readouterr()
    assert status == 0, captured.err
    lines = captured.out.splitlines()
    assert lines[0].startswith("enrolled node ")
    assert [ln.split(":")[0] for ln in lines[1:4]] == ["turn 1", "turn 2", "turn 3"]
    assert all("(target 3 s: " in ln for ln in lines[1:4])
    assert lines[4].startswith("worst first reply.chunk ") and len(lines) == 5
    token = code.split(".")[0]
    assert token not in captured.out + captured.err
    assert "PRIVATE KEY" not in captured.out + captured.err
    assert core.ran == 3


def test_runner_reports_a_refused_code(
    dsns: Dsns, served: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A code the gateway refuses ends the run with status 1 and a plain reason."""
    bad = (
        "a" * 26 + "." + fingerprint(b"x" * 32) + "." + fingerprint(public_bytes(served["gw_key"]))
    )
    status = live_node.main(["--gateway", f"127.0.0.1:{served['port']}", "--code", bad])
    captured = capsys.readouterr()
    assert status == 1 and captured.err.startswith("FAILED: ")
    assert "a" * 26 not in captured.err
