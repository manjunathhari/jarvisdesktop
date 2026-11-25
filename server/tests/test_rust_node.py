"""The Rust desktop node end to end (plan B Task 9, decision D-3).

`jarvis-node-probe` is the real `jarvis-node` runtime (node::start, session, journal, inbox,
approval cards, executor) with a JSON-lines UI and an in-memory test folder. It talks to the
real gateway served by uvicorn on 127.0.0.1 and to the real core (every queued job dispatched
in a thread, with a fake brain). Nothing replaces the Rust protocol code. Every person, phrase
and file is fictional.

Skipped unless `JARVIS_NODE_PROBE` names the built probe:
`cargo build -p jarvis-node --features test-support --bin jarvis-node-probe`.
"""

# The fixtures imported from test_live_node are pytest parameters here, not redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server.brain import Speech, ToolCall
from jarvis_server.core import dispatch, turn
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.signing import fingerprint, public_bytes

from .conftest import Dsns
from .helpers import flow
from .test_gateway_delivery import item
from .test_gateway_handshake import MANJ, make_authz
from .test_live_node import REPLY, _code, dsns, memory, served  # noqa: F401 (fixtures)
from .test_turn import Brains

PROBE = os.environ.get("JARVIS_NODE_PROBE")
pytestmark = pytest.mark.skipif(not PROBE, reason="set JARVIS_NODE_PROBE to the built probe")

Event = dict[str, Any]


class AllJobs:
    """Core's job loop for every kind, in a thread, until stopped (the approval flow needs
    conv.turn, action.receipt and approval.decision)."""

    def __init__(self, dsn: str) -> None:
        self.ran: list[str] = []
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(dsn,), daemon=True)
        self._thread.start()

    def _loop(self, dsn: str) -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                while not self._stop.is_set():
                    row = conn.execute(
                        "SELECT id, kind FROM ops.job WHERE status = 'QUEUED' AND run_at <= now()"
                        " ORDER BY id LIMIT 1"
                    ).fetchone()
                    if row is None:
                        time.sleep(0.02)
                        continue
                    dispatch.handle(conn, flow.claim(conn, int(row[0])))
                    self.ran.append(str(row[1]))
        except BaseException as e:
            self.error = e

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(20)
        if self.error is not None:
            raise self.error


@pytest.fixture
def core(dsns: Dsns) -> Iterator[AllJobs]:
    c = AllJobs(dsns.app)
    yield c
    c.stop()


class Probe:
    """One run of the probe: its JSON-line events as they arrive, and its stderr log."""

    def __init__(self, port: int, state: Path, *steps: str) -> None:
        assert PROBE is not None
        args = [PROBE, "--gateway", f"127.0.0.1:{port}", "--state-dir", str(state), *steps]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "RUST_LOG": "info"},
        )  # fmt: skip
        self.events: list[Event] = []
        self._q: queue.Queue[Event | None] = queue.Queue()
        self.stderr = ""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._q.put(json.loads(line))
        self._q.put(None)

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr += line

    def wait_for(self, pred: Callable[[Event], bool], timeout: float = 30) -> Event:
        for ev in self.events:
            if pred(ev):
                return ev
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            assert left > 0, f"timed out; events so far: {self.events}\n{self.stderr[-3000:]}"
            try:
                ev = self._q.get(timeout=left)
            except queue.Empty:
                continue
            assert ev is not None, f"the probe exited; events: {self.events}\n{self.stderr[-3000:]}"
            self.events.append(ev)
            if pred(ev):
                return ev

    def finish(self, timeout: float = 60) -> list[Event]:
        """Wait for the probe to end (its steps done) and return every event."""
        self.proc.wait(timeout)
        while True:
            ev = self._q.get(timeout=5)
            if ev is None:
                break
            self.events.append(ev)
        time.sleep(0.1)  # the stderr reader catches up
        return self.events

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(10)


def kinds(events: list[Event]) -> list[str]:
    return [e["event"] for e in events]


def _enrolled(
    dsns: Dsns,
    core_key: Any,
    served: dict[str, Any],
    tmp_path: Path,
) -> tuple[Path, uuid.UUID]:
    """Enrol a fresh probe state folder; its path and the node's ID (the probe then exits)."""
    state = tmp_path / "probe"
    code = _code(dsns, core_key, served["gw_key"])
    p = Probe(served["port"], state, "--enrol", code)
    events = p.finish()
    assert kinds(events)[-1] == "done", events
    assert "enrolled" in kinds(events)
    token = code.split(".")[0]
    assert token not in json.dumps(events) + p.stderr, "the code is never printed or logged"
    return state, uuid.UUID(next(e for e in events if e["event"] == "enrolled")["node_id"])


def test_rust_node_turn(
    dsns: Dsns, memory: MemoryEngine, core_key: Any, served: dict[str, Any],
    core: AllJobs, tmp_path: Path,
) -> None:  # fmt: skip
    """Enrol → say a fictional phrase → SPEECH chunks in order → final, through the real
    gateway and core, then reconnect with hello on a second run."""
    code = _code(dsns, core_key, served["gw_key"])
    state = tmp_path / "probe"
    p = Probe(served["port"], state, "--enrol", code, "--say", "What is the capital of Freedonia?")
    events = p.finish()
    assert kinds(events)[-1] == "done", events
    spoken = "".join(e["text"] for e in events if e["event"] == "speech")
    assert spoken == REPLY
    chunks = [e for e in events if e["event"] == "reply"]
    assert [c["seq"] for c in chunks] == list(range(1, len(chunks) + 1)), "in order"
    assert not any(c["final"] for c in chunks[:-1]), "final only on the last"
    # ServerBrain ends a turn only on the final chunk (the probe's separate reply printer may
    # be stopped before it prints that last line).
    assert "turn_done" in kinds(events)
    assert "conv.turn" in core.ran
    # A second run of the same node: hello (no enrolment), another turn.
    p = Probe(served["port"], state, "--say", "What is the capital of Freedonia?")
    events = p.finish()
    assert "".join(e["text"] for e in events if e["event"] == "speech") == REPLY
    assert "enrolled" not in kinds(events)


def test_rust_node_approval_flow(
    dsns: Dsns, memory: MemoryEngine, core_key: Any, served: dict[str, Any],
    core: AllJobs, tmp_path: Path,
) -> None:  # fmt: skip
    """The fake brain proposes deleting old-notes.txt → the probe answers the listing from its
    memory folder → the card arrives → approved → the delete runs and its receipt is sent →
    core verifies with a fresh listing → a verified ledger.outcome, and Jarvis says so."""
    turn.set_brain(Brains([Speech("Let me check. "),
                           ToolCall("pc_delete_test_file", {"name": "old-notes.txt"}),
                           Speech("I've asked for your approval.")]))  # fmt: skip
    code = _code(dsns, core_key, served["gw_key"])
    p = Probe(
        served["port"], tmp_path / "probe",
        "--memory-folder", "old-notes.txt=fictional notes, safe to delete",
        "--enrol", code, "--say", "Please delete old-notes.txt", "--approve-next", "--wait", "8",
    )  # fmt: skip
    card = p.wait_for(lambda e: e["event"] == "card", timeout=40)
    assert "old-notes.txt" in card["summary"]
    p.wait_for(lambda e: e["event"] == "decided")
    p.wait_for(lambda e: e["event"] == "card_cleared" and e["reason"] == "SENT")
    done = p.wait_for(lambda e: e["event"] == "reply" and "Recycle Bin" in e["text"], timeout=40)
    assert done["in_reply_to"] is None, "core's own result answers no utterance"
    events = p.finish()
    assert kinds(events)[-1] == "done", events
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        approval_id = uuid.UUID(card["approval_id"])
        row = conn.execute(
            "SELECT o.verified FROM ledger.outcome o JOIN ledger.action a ON a.id = o.action_id"
            " JOIN ledger.approval p ON p.task_id = a.task_id WHERE p.id = %s"
            " ORDER BY o.created_at DESC LIMIT 1",
            (approval_id,),
        ).fetchone()
        assert row == (True,), "a verified outcome"
        assert flow.task_state(conn, approval_id) == "COMPLETED"


def test_rust_node_reconnects_and_never_reshows(
    dsns: Dsns, core_key: Any, served: dict[str, Any], tmp_path: Path,
) -> None:  # fmt: skip
    """Kill the probe once it has shown an item, make the gateway offer it again, restart the
    probe: only the node's seen-list keeps it from being shown twice."""
    state, _ = _enrolled(dsns, core_key, served, tmp_path)
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        outbox_id = item(conn)
        conn.execute("SELECT pg_notify('jarvis_outbox', %s)", (str(MANJ),))
    p = Probe(served["port"], state, "--wait", "30")
    p.wait_for(lambda e: e["event"] == "notice" and e["outbox_id"] == str(outbox_id))
    p.kill()
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        # As if the `received` never arrived: the gateway's sweep offers the item again.
        conn.execute("DELETE FROM comm.delivery WHERE outbox_id = %s", (outbox_id,))
    p = Probe(served["port"], state, "--wait", "3")
    events = p.finish()
    assert {"event": "link", "status": "connected"} in events
    shown = [e for e in events if e["event"] == "notice" and e["outbox_id"] == str(outbox_id)]
    assert shown == [], events


def test_rust_node_refuses_gateway_only_action(
    dsns: Dsns, core_key: Any, served: dict[str, Any], tmp_path: Path,
) -> None:  # fmt: skip
    """An action signed only by the gateway (its key, under core's key_id) reaches the node as
    node.action and is refused: nothing is broadcast, nothing receipted, the link stays up."""
    state, node = _enrolled(dsns, core_key, served, tmp_path)
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        session = flow.open_session(conn, MANJ)
        now = flow.db_now(conn)
        reply = uuid.uuid4()
        obj = {
            "v": 1, "kind": "node.action", "action_kind": "module.news",
            "args": {"tool": "show_news", "input": {}}, "target_node_id": str(node),
            "person_id": str(MANJ), "approval_id": None, "execution_key": None,
            "reply_id": str(reply), "summary": None,
            "issued_at": now.isoformat(), "valid_until": (now + timedelta(minutes=2)).isoformat(),
        }  # fmt: skip
        forged = make_authz(served["gw_key"], fingerprint(public_bytes(core_key)), obj)
    p = Probe(served["port"], state, "--wait", "4")
    p.wait_for(lambda e: e == {"event": "link", "status": "connected"})
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, authz, final,"
            " target_node_id, valid_until) VALUES (%s, %s, 1, 'ACTION', %s, false, %s, %s)",
            (reply, session, Jsonb(forged), node, now + timedelta(minutes=2)),
        )
        conn.execute("SELECT pg_notify('jarvis_stream', %s)", (str(reply),))
    events = p.finish()
    assert "bad_signature" in p.stderr, p.stderr[-2000:]
    assert not [e for e in events if e["event"] == "reply_action"], events
    assert {"event": "link", "status": "needs_reenrol"} not in events
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        receipts = conn.execute(
            "SELECT count(*) FROM conv.inbound WHERE kind = 'action.receipt'"
            " AND payload->>'reply_id' = %s",
            (str(reply),),
        ).fetchone()
    assert receipts == (0,)
