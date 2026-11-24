"""Gateway, part 2: the stream relay with its durable cursor, outbox delivery and
acknowledgement, the welcome summary, duplicate connections, fencing and the heartbeat (spec
§6.2, §8.2, §8.3; R8-1, R9-1, R12-8, R12-9; P2-E03, P2-E04, P2-E05, P2-E07, P2-E13).

Runs in its own database. The gateway connects as jarvis_gateway; tests write chunks, items and
sessions as jarvis_app (core's role) and NOTIFY as core does, and plant cursor rows as the
owner. The gateway clock is a FakeClock that starts at the real time, so the database's own
`last_sent_at` and `created_at` line up with it; advancing it stands in for waiting. Every
person is fictional.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
import random
import secrets
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.websockets import WebSocket, WebSocketDisconnect

from jarvis_server import comm, nodes
from jarvis_server.comm import OutboxState, Priority
from jarvis_server.contracts import Welcome
from jarvis_server.core import rules, summary
from jarvis_server.dbcall import OutboxRefused
from jarvis_server.gateway import relay
from jarvis_server.signing import fingerprint, public_bytes

from .conftest import Dsns, fresh_database
from .fakes.fake_node import FakeNode
from .helpers.threads import InThread
from .test_gateway_handshake import MANJ, FakeClock, Gw, _wait_for, running_gateway

Conn = psycopg.Connection[Any]
HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
TRACE_PATH = Path(__file__).parent / "fakes" / "traces" / "e13.jsonl"


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def ddb() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def app_conn(ddb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(ddb.app, autocommit=True) as conn:
        yield conn


@contextmanager
def gateway(
    dsns: Dsns,
    tmp: Path,
    *,
    keys_from: Gw | None = None,
    recheck_interval: float = 0.1,
    tick_interval: float = 0.02,
    clock: FakeClock | None = None,
    send_timeout: float = 10.0,
) -> Iterator[Gw]:
    """A gateway app; `keys_from` restarts with an earlier one's keys (and clock), as a new
    process would: nothing else carries over."""
    with running_gateway(
        dsns,
        tmp,
        gw_key=keys_from.gw_key if keys_from else None,
        core_key=keys_from.core_key if keys_from else None,
        recheck_interval=recheck_interval,
        tick_interval=tick_interval,
        send_timeout=send_timeout,
        clock=clock or (keys_from.clock if keys_from else FakeClock(datetime.now(UTC))),
    ) as gw:
        yield gw


@pytest.fixture
def gw(ddb: Dsns, tmp_path: Path) -> Iterator[Gw]:
    with gateway(ddb, tmp_path) as g:
        yield g


# ------------------------------------------------------------------------------ helpers


def _relay(g: Gw) -> relay.Relay:
    return g.app.state.gateway.relay


def _sweep(g: Gw) -> None:
    """Run one re-send sweep to completion inside the app's event loop."""
    r = _relay(g)
    g.client.portal.call(lambda: r.sweep(wait=True))


def reborn(g: Gw, fake: FakeNode, identity: str | None = None) -> FakeNode:
    """The same device (key, address, state file) as a new FakeNode on gateway `g`."""
    g.whois.table[fake.peer_ip] = identity or _identity(fake)
    return g.node(key=fake.key, peer_ip=fake.peer_ip, state=fake.state_path)


_IDENTITIES: dict[str, str] = {}


def _identity(fake: FakeNode) -> str:
    return _IDENTITIES[fake.peer_ip]


def enrolled(g: Gw, person: uuid.UUID = MANJ) -> FakeNode:
    fake = g.node()
    if person == MANJ:
        code = g.code()
    else:
        with psycopg.connect(g.dsns.app, autocommit=True) as conn:
            code = nodes.create_enrolment(
                conn,
                person_id=person,
                kind="DESKTOP",
                name="guest pc",
                core_fp=fingerprint(public_bytes(g.core_key)),
                gw_fp=fingerprint(public_bytes(g.gw_key)),
            )
    fake.enrol(code)
    _IDENTITIES[fake.peer_ip] = g.whois.table[fake.peer_ip]
    fake.sync()
    return fake


def person(app: Conn) -> uuid.UUID:
    row = app.execute(
        "INSERT INTO ledger.person (display_name, organization_id, source_ref)"
        " VALUES ('Fictional Guest', %s, 'test') RETURNING id",
        (HOUSEHOLD,),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def session_of(app: Conn, who: uuid.UUID = MANJ) -> uuid.UUID:
    app.execute(
        "INSERT INTO conv.session (person_id) VALUES (%s)"
        " ON CONFLICT (person_id) WHERE closed_at IS NULL DO NOTHING",
        (who,),
    )
    row = app.execute(
        "SELECT id FROM conv.session WHERE person_id = %s AND closed_at IS NULL", (who,)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def speech(
    app: Conn,
    reply: uuid.UUID,
    seqs: list[int],
    *,
    who: uuid.UUID = MANJ,
    final: int | None = None,
    notify: bool = True,
) -> None:
    """Write SPEECH chunks as core does, then NOTIFY jarvis_stream with the reply ID only."""
    session = session_of(app, who)
    for seq in seqs:
        app.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, text, final)"
            " VALUES (%s, %s, %s, 'SPEECH', 'A fictional reply', %s)",
            (reply, session, seq, seq == final),
        )
    if notify:
        app.execute("SELECT pg_notify('jarvis_stream', %s)", (str(reply),))


def action(
    app: Conn,
    reply: uuid.UUID,
    seq: int,
    *,
    authz: dict[str, Any],
    target: uuid.UUID,
    valid_until: datetime,
) -> None:
    app.execute(
        "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, authz, final,"
        " target_node_id, valid_until) VALUES (%s, %s, %s, 'ACTION', %s, false, %s, %s)",
        (reply, session_of(app), seq, Jsonb(authz), target, valid_until),
    )


def item(
    app: Conn,
    *,
    who: uuid.UUID = MANJ,
    priority: Priority = Priority.TODAY,
    kind: str = "test.notice",
    group_key: str = "test",
    target: uuid.UUID | None = None,
) -> uuid.UUID:
    event = comm.record_event(
        app,
        "test.notice",
        subject_type="test",
        subject_id=f"t-{secrets.token_hex(4)}",
        person_id=who,
        details={"code": "fictional"},
    )
    return comm.create_outbox(
        app,
        person_id=who,
        event_id=event,
        priority=priority,
        kind=kind,
        body={"event": kind, "text": "A fictional notice"},
        group_key=group_key,
        expires_at=None,
        target_node_id=target,
    )


def chunk_seqs(envs: list[dict[str, Any]], reply: uuid.UUID) -> list[int]:
    return [
        e["payload"]["seq"]
        for e in envs
        if e["kind"] == "reply.chunk" and e["payload"]["reply_id"] == str(reply)
    ]


def item_ids(envs: list[dict[str, Any]], kind: str = "outbox.item") -> list[str]:
    return [e["payload"]["outbox_id"] for e in envs if e["kind"] == kind]


def collect(
    fake: FakeNode, done: Callable[[list[dict[str, Any]]], bool], timeout: float = 5.0
) -> list[dict[str, Any]]:
    """Receive until `done(received)` holds (or fail), then whatever else is queued."""
    out: list[dict[str, Any]] = []
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while not done(out):
        assert datetime.now(UTC) < deadline, [e["kind"] for e in out]
        with contextlib.suppress(TimeoutError):
            out.append(fake.recv(0.05))
    return out + fake.drain(0.1)


def cursor(app: Conn, reply: uuid.UUID, node: uuid.UUID | None) -> int | None:
    row = app.execute(
        "SELECT acked_seq FROM conv.stream_cursor WHERE reply_id = %s AND node_id = %s",
        (reply, node),
    ).fetchone()
    return None if row is None else int(row[0])


def plant_cursor(dsns: Dsns, reply: uuid.UUID, node: uuid.UUID | None, acked: int) -> None:
    with psycopg.connect(dsns.owner, autocommit=True) as owner:
        owner.execute(
            "INSERT INTO conv.stream_cursor (reply_id, node_id, acked_seq) VALUES (%s, %s, %s)",
            (reply, node, acked),
        )


def resume_events(app: Conn, node: uuid.UUID | None) -> list[dict[str, Any]]:
    rows = app.execute(
        "SELECT details FROM comm.event WHERE kind = 'node.resume_out_of_range'"
        " AND subject_id = %s ORDER BY occurred_at, id",
        (str(node),),
    ).fetchall()
    return [dict(r[0]) for r in rows]


def audit_seq(app: Conn) -> int:
    row = app.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


def state(app: Conn, outbox_id: uuid.UUID | str) -> str:
    row = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (outbox_id,)).fetchone()
    assert row is not None
    return str(row[0])


def attempts(app: Conn, outbox_id: uuid.UUID | str, node: uuid.UUID | None) -> int | None:
    row = app.execute(
        "SELECT attempts FROM comm.delivery WHERE outbox_id = %s AND node_id = %s",
        (outbox_id, node),
    ).fetchone()
    return None if row is None else int(row[0])


def closed(fake: FakeNode, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every message up to the gateway closing the socket (which must happen)."""
    out: list[dict[str, Any]] = []
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while True:
        assert datetime.now(UTC) < deadline, "the gateway never closed the connection"
        try:
            out.append(fake.recv(0.05))
        except TimeoutError:
            continue
        except WebSocketDisconnect:
            return out


def close_reason(app: Conn, node: uuid.UUID | None) -> list[Any]:
    rows = app.execute(
        "SELECT close_reason FROM gw.node_connection WHERE node_id = %s ORDER BY id", (node,)
    ).fetchall()
    return [r[0] for r in rows]


class _FencedError(psycopg.Error):
    sqlstate = "JF001"


# ------------------------------------------------------------------------------ stream relay


def test_stream_resumes_after_gateway_restart(ddb: Dsns, tmp_path: Path, app_conn: Conn) -> None:
    """P2-E03: the cursor, not the process, carries the stream across a gateway restart."""
    reply = uuid.uuid4()
    with gateway(ddb, tmp_path) as first:
        a = enrolled(first)
        speech(app_conn, reply, [1, 2, 3])
        collect(a, lambda envs: len(chunk_seqs(envs, reply)) == 3)
        a.sync()  # its `received` messages are recorded
        assert cursor(app_conn, reply, a.node_id) == 3
        shown_before = list(a.shown_chunks[str(reply)])
        a.close()
    with gateway(ddb, tmp_path, keys_from=first) as second:  # nothing in memory carries over
        b = reborn(second, a)
        b.hello()
        assert chunk_seqs(b.sync(), reply) == []
        speech(app_conn, reply, [4, 5], final=5)
        envs = collect(b, lambda e: len(chunk_seqs(e, reply)) == 2)
        assert chunk_seqs(envs, reply) == [4, 5]
        assert shown_before + b.shown_chunks[str(reply)] == [1, 2, 3, 4, 5]


def test_unacked_chunks_resent_after_restart(ddb: Dsns, tmp_path: Path, app_conn: Conn) -> None:
    reply = uuid.uuid4()
    with gateway(ddb, tmp_path) as first:
        a = enrolled(first)
        a.ack_chunks = False
        speech(app_conn, reply, [1, 2, 3])
        collect(a, lambda envs: len(chunk_seqs(envs, reply)) == 3)
        a.send("received", {"reply_id": str(reply), "seq": 2})
        a.sync()
        assert cursor(app_conn, reply, a.node_id) == 2
        shown_before = list(a.shown_chunks[str(reply)])
        a.close()
    with gateway(ddb, tmp_path, keys_from=first) as second:
        b = reborn(second, a)
        b.hello(resume=[])  # no resume position: the cursor alone decides
        assert chunk_seqs(b.sync(), reply) == [3]
        assert shown_before == [1, 2, 3] and b.shown_chunks[str(reply)] == []  # shown once


def _stored_three_acked_one(g: Gw, app: Conn, dsns: Dsns) -> tuple[FakeNode, uuid.UUID]:
    a = enrolled(g)
    a.close()
    reply = uuid.uuid4()
    speech(app, reply, [1, 2, 3], final=3, notify=False)
    plant_cursor(dsns, reply, a.node_id, 1)
    return a, reply


def test_resume_beyond_stored_max_does_not_skip(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": str(reply), "last_seq": 99}])
    assert chunk_seqs(b.sync(), reply) == [2, 3]
    assert resume_events(app_conn, a.node_id) == [
        {"node_id": str(a.node_id), "reply_id": str(reply), "reason": "beyond_max"}
    ]


def test_resume_within_range_used(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": str(reply), "last_seq": 2}])
    assert chunk_seqs(b.sync(), reply) == [3]
    assert resume_events(app_conn, a.node_id) == []


def test_resume_negative_treated_as_zero(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": str(reply), "last_seq": -5}])
    assert chunk_seqs(b.sync(), reply) == [2, 3]
    assert resume_events(app_conn, a.node_id) == []


def test_resume_huge_last_seq_reaches_the_database(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    """The largest `last_seq` a signed `hello` can carry (JCS limits signed integers to
    2^53 - 1, so a larger one can never verify) reaches the database as numeric: ignored,
    recorded, nothing skipped. Beyond 64 bits is covered at the SQL boundary (test_comm_db)."""
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": str(reply), "last_seq": 2**53 - 1}])
    assert chunk_seqs(b.sync(), reply) == [2, 3]
    assert [e["reason"] for e in resume_events(app_conn, a.node_id)] == ["beyond_max"]


def test_resume_last_seq_beyond_two_to_the_53_is_refused(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    """Owner final-review area 1 (no widening): a `hello` whose `last_seq` is 2^53 can't
    carry a valid signature (the node signs 2^53 - 1, the frame says 2^53). The gateway
    refuses the handshake once as `bad_signature`; nothing reaches the resume function, no
    connection opens and no chunk is sent."""
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    connections = close_reason(app_conn, a.node_id)
    b = reborn(gw, a)
    b.connect()
    assert b.challenge is not None
    since = audit_seq(app_conn)

    def widen(env: dict[str, Any]) -> None:
        env["payload"]["resume"][0]["last_seq"] = 2**53

    resume = [{"reply_id": str(reply), "last_seq": 2**53 - 1}]
    b.send("hello", {"nonce": b.challenge["nonce"], "resume": resume}, mutate=widen)
    assert '"last_seq": 9007199254740992' in b.last_sent  # the frame on the wire
    got = closed(b)
    assert chunk_seqs(got, reply) == [] and [e["kind"] for e in got] == []  # no welcome
    rows = app_conn.execute(
        "SELECT action::text, reason FROM audit.event WHERE seq > %s ORDER BY seq", (since,)
    ).fetchall()
    assert rows == [("CONNECT_REFUSED", "bad_signature")]
    assert resume_events(app_conn, a.node_id) == []
    assert close_reason(app_conn, a.node_id) == connections  # no connection was opened


@pytest.mark.parametrize("last_seq", [1e18, 2.0, 9007199254740993.0])
def test_resume_last_seq_float_spelling_is_refused(
    gw: Gw, app_conn: Conn, ddb: Dsns, last_seq: float
) -> None:
    """Final review M-1: a `last_seq` spelled as a JSON number that isn't an integer literal
    (`1e+18`, `2.0`) is refused whatever its value, even when correctly signed: the handshake
    is refused `malformed`, no connection opens, nothing reaches the resume function."""
    a, reply = _stored_three_acked_one(gw, app_conn, ddb)
    connections = close_reason(app_conn, a.node_id)
    b = reborn(gw, a)
    b.connect()
    assert b.challenge is not None
    since = audit_seq(app_conn)
    resume = [{"reply_id": str(reply), "last_seq": last_seq}]
    b.send("hello", {"nonce": b.challenge["nonce"], "resume": resume})  # signed as sent
    assert f'"last_seq": {json.dumps(last_seq)}' in b.last_sent
    got = closed(b)
    assert [e["kind"] for e in got] == []  # no welcome
    rows = app_conn.execute(
        "SELECT action::text, reason FROM audit.event WHERE seq > %s ORDER BY seq", (since,)
    ).fetchall()
    assert rows == [("CONNECT_REFUSED", "malformed")]
    assert resume_events(app_conn, a.node_id) == []
    assert close_reason(app_conn, a.node_id) == connections


def test_resume_for_foreign_reply_ignored(gw: Gw, app_conn: Conn) -> None:
    guest = person(app_conn)
    foreign = uuid.uuid4()
    speech(app_conn, foreign, [1, 2], who=guest, notify=False)
    a = enrolled(gw)
    a.close()
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": str(foreign), "last_seq": 1}])
    assert chunk_seqs(b.sync(), foreign) == []
    assert resume_events(app_conn, a.node_id) == [
        {"node_id": str(a.node_id), "reply_id": str(foreign), "reason": "unknown_reply"}
    ]


def test_resume_only_first_64_entries_checked(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    a.close()
    unknown = [str(uuid.uuid4()) for _ in range(70)]
    b = reborn(gw, a)
    b.hello(resume=[{"reply_id": r, "last_seq": 1} for r in unknown])
    b.sync()
    assert [e["reply_id"] for e in resume_events(app_conn, a.node_id)] == unknown[:64]


def test_reconnect_never_uses_cursor_rows_as_authority(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    """R9-1: a cursor row for (A's reply, B's node), planted by the owner, gives B nothing."""
    reply_a = uuid.uuid4()
    speech(app_conn, reply_a, [1, 2, 3], notify=False)  # A = the seeded fictional person
    guest = person(app_conn)
    b = enrolled(gw, guest)
    plant_cursor(ddb, reply_a, b.node_id, 0)
    b.close()
    b2 = reborn(gw, b)
    b2.hello(resume=[])
    assert chunk_seqs(b2.sync(), reply_a) == []
    speech(app_conn, reply_a, [4])  # a live wake-up for A's reply reaches A's nodes only
    assert chunk_seqs(b2.drain(0.3), reply_a) == []
    assert cursor(app_conn, reply_a, b.node_id) == 0


def test_ack_for_pruned_chunk_is_noop(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    reply = uuid.uuid4()
    speech(app_conn, reply, [1, 2], final=2)
    collect(a, lambda envs: len(chunk_seqs(envs, reply)) == 2)
    a.sync()
    app_conn.execute("DELETE FROM conv.stream_cursor WHERE reply_id = %s", (reply,))
    app_conn.execute("DELETE FROM conv.stream_chunk WHERE reply_id = %s", (reply,))  # retention
    since = audit_seq(app_conn)
    a.send("received", {"reply_id": str(reply), "seq": 2})  # a late ack
    assert a.sync() == []  # no error, and the connection stays open
    rows = app_conn.execute("SELECT count(*) FROM audit.event WHERE seq > %s", (since,))
    assert rows.fetchone() == (0,)
    assert cursor(app_conn, reply, a.node_id) is None


def test_core_restart_mid_stream(gw: Gw, app_conn: Conn) -> None:
    """P2-E03, P2-B4, gateway half only (controller ruling G3): core wrote chunks 1..2 and
    died. Standing in for the lease-expiry sweep, the core role creates one
    `turn.indeterminate` item. The node keeps 1..2, gets no final chunk and exactly one such
    item. "Brain called once" and the real lease-expiry path are tested in Task 10."""
    a = enrolled(gw)
    reply = uuid.uuid4()
    speech(app_conn, reply, [1, 2])
    envs = collect(a, lambda e: len(chunk_seqs(e, reply)) == 2)
    event = comm.record_event(
        app_conn,
        "turn.indeterminate",
        subject_type="reply",
        subject_id=str(reply),
        person_id=MANJ,
        details={"reply_id": str(reply)},
    )
    [made] = rules.notify(
        app_conn, "turn.indeterminate", person_id=MANJ, event_id=event, context={}
    )
    envs += collect(a, lambda e: str(made) in item_ids(e))
    envs += a.sync()
    assert a.shown_chunks[str(reply)] == [1, 2]
    assert not any(e["payload"].get("final") for e in envs if e["kind"] == "reply.chunk")
    items = [e["payload"] for e in envs if e["kind"] == "outbox.item"]
    assert [(i["outbox_id"], i["kind"]) for i in items] == [(str(made), "turn.indeterminate")]
    assert items[0]["body"] == "A request ended without a clear outcome"  # body["text"] (G4.1)


def test_action_chunk_only_to_target_and_before_valid_until(gw: Gw, app_conn: Conn) -> None:
    """G4.2: a live ACTION chunk goes as `node.action {authz}` to its target only, and only
    while now() <= valid_until. Never as `reply.chunk` type ACTION."""
    a = enrolled(gw)
    b = enrolled(gw)
    reply = uuid.uuid4()
    now = gw.clock()
    live = gw.authz(a.node_id)
    expired = gw.authz(a.node_id)
    speech(app_conn, reply, [1], notify=False)
    action(app_conn, reply, 2, authz=live, target=a.node_id, valid_until=now + timedelta(minutes=2))
    action(
        app_conn, reply, 3, authz=expired, target=a.node_id, valid_until=now - timedelta(seconds=1)
    )
    speech(app_conn, reply, [4], final=4)
    envs_a = collect(a, lambda e: len(chunk_seqs(e, reply)) == 2)
    envs_b = collect(b, lambda e: len(chunk_seqs(e, reply)) == 2)
    relevant = [e for e in envs_a if e["kind"] in ("reply.chunk", "node.action")]
    assert [(e["kind"], e["payload"].get("seq")) for e in relevant] == [
        ("reply.chunk", 1),
        ("node.action", None),
        ("reply.chunk", 4),
    ]
    assert relevant[1]["payload"] == {"authz": live}
    assert a.actions == [live["object"]] and a.rejected == []
    assert [e["kind"] for e in envs_b if e["kind"] in ("reply.chunk", "node.action")] == [
        "reply.chunk",
        "reply.chunk",
    ]
    assert chunk_seqs(envs_b, reply) == [1, 4] and b.actions == []
    assert cursor(app_conn, reply, a.node_id) == 4  # SPEECH acks move the cursor past ACTIONs


def test_action_chunk_never_resent_on_reconnect(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    a.ack_chunks = False
    reply = uuid.uuid4()
    speech(app_conn, reply, [1], notify=False)
    action(
        app_conn,
        reply,
        2,
        authz=gw.authz(a.node_id),
        target=a.node_id,
        valid_until=gw.clock() + timedelta(minutes=2),
    )
    speech(app_conn, reply, [3], final=3)
    envs = collect(a, lambda e: len(chunk_seqs(e, reply)) == 2)
    assert [e["kind"] for e in envs if e["kind"] == "node.action"] == ["node.action"]
    a.close()
    b = reborn(gw, a)
    b.hello(resume=[])  # nothing acknowledged: every SPEECH chunk again, never the action
    again = b.sync()
    assert chunk_seqs(again, reply) == [1, 3]
    assert [e for e in again if e["kind"] == "node.action"] == []


# ------------------------------------------------------------------------------ outbox


def test_outbox_results_are_typed(ddb: Dsns, app_conn: Conn) -> None:
    with psycopg.connect(ddb.gateway, autocommit=True) as gwc:
        code = nodes.create_enrolment(
            app_conn,
            person_id=MANJ,
            kind="DESKTOP",
            name="pc",
            core_fp=fingerprint(bytes(range(32))),
            gw_fp=fingerprint(bytes(range(32, 64))),
        )
        token, _, _ = nodes.parse_enrolment_code(code)
        node = nodes.enrol(
            gwc,
            token=token,
            public_key=secrets.token_bytes(32),
            tailscale_identity=f"ts:pc-{secrets.token_hex(4)}",
        )
        row = gwc.execute("SELECT conn_id FROM gw.open_connection(%s)", (node,)).fetchone()
        assert row is not None
        conn_id = int(row[0])
    outbox_id = item(app_conn)
    unknown = uuid.uuid4()
    since = audit_seq(app_conn)

    async def run() -> tuple[Any, Any, Any]:
        async with await psycopg.AsyncConnection.connect(ddb.gateway, autocommit=True) as c:
            got = await relay.record_received(c, conn_id, outbox_id)
            acked = await relay.acknowledge(c, conn_id, outbox_id)
            with pytest.raises(OutboxRefused) as refused:
                await relay.acknowledge(c, conn_id, unknown)
            with pytest.raises(OutboxRefused):
                await relay.record_received(c, conn_id, unknown)
            return got, acked, refused.value.reason

    got, acked, reason = asyncio.run(run())
    assert type(got) is OutboxState and got is OutboxState.DELIVERED
    assert type(acked) is OutboxState and acked is OutboxState.ACKNOWLEDGED
    assert reason == "unknown_item"
    with psycopg.connect(ddb.app, autocommit=True) as other:  # the refusals committed first
        rows = other.execute(
            "SELECT action::text, reason FROM audit.event WHERE seq > %s AND outcome = 'REJECTED'"
            " ORDER BY seq",
            (since,),
        ).fetchall()
    assert rows == [("ACK", "unknown_item"), ("RECEIVE", "unknown_item")]


def test_outbox_delivered_to_all_person_nodes(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    b = enrolled(gw)
    guest = enrolled(gw, person(app_conn))
    outbox_id = str(item(app_conn))
    for fake in (a, b):
        assert item_ids(collect(fake, lambda e: outbox_id in item_ids(e))) == [outbox_id]
        assert fake.displays[outbox_id] == 1
    assert item_ids(guest.drain(0.3)) == []
    _wait_for(lambda: state(app_conn, outbox_id) == "DELIVERED")


def test_targeted_item_reaches_only_its_node(gw: Gw, app_conn: Conn) -> None:
    """R12-23: an item with a target node is offered, re-sent and cleared only on that node;
    the person's other node gets nothing, and an item without a target still goes to both."""
    a = enrolled(gw)
    b = enrolled(gw)
    a.auto_received = False
    targeted = str(item(app_conn, target=a.node_id))
    everyone = str(item(app_conn))
    got_a = collect(a, lambda e: {targeted, everyone} <= set(item_ids(e)))
    assert sorted(item_ids(got_a)) == sorted([targeted, everyone])
    got_b = collect(b, lambda e: everyone in item_ids(e))
    assert item_ids(got_b + b.sync() + b.drain(0.2)) == [everyone]  # no offer
    gw.clock.advance(150)
    _sweep(gw)
    assert targeted in item_ids(a.drain(0.3))  # re-sent to its node (unconfirmed)
    assert item_ids(b.sync() + b.drain(0.2)) == []  # no re-send
    b2 = reborn(gw, b)  # a reconnect offers it nothing either
    b2.hello()
    assert targeted not in item_ids(b2.sync() + b2.drain(0.2))
    assert attempts(app_conn, targeted, b.node_id) is None
    since = audit_seq(app_conn)
    b2.received(uuid.UUID(targeted))  # not b's item: refused like another person's
    b2.sync()
    rows = app_conn.execute(
        "SELECT action::text, outcome::text, reason FROM audit.event WHERE seq > %s"
        " AND entity_id = %s",
        (since, targeted),
    ).fetchall()
    assert rows == [("RECEIVE", "REJECTED", "unknown_item")]
    a.ack(uuid.UUID(targeted))
    a.sync()
    assert state(app_conn, targeted) == "ACKNOWLEDGED"
    assert item_ids(b2.sync() + b2.drain(0.3), "outbox.cleared") == []  # no clear
    assert b.displays[targeted] == 0 and b2.displays[targeted] == 0


def test_ack_on_a_clears_on_b(gw: Gw, app_conn: Conn) -> None:
    """P2-E05."""
    a = enrolled(gw)
    b = enrolled(gw)
    outbox_id = str(item(app_conn))
    collect(a, lambda e: outbox_id in item_ids(e))
    collect(b, lambda e: outbox_id in item_ids(e))
    a.ack(uuid.UUID(outbox_id))
    cleared = collect(b, lambda e: outbox_id in item_ids(e, "outbox.cleared"))
    assert [e["payload"] for e in cleared if e["kind"] == "outbox.cleared"] == [
        {"outbox_id": outbox_id, "reason": "ACKNOWLEDGED"}
    ]
    assert state(app_conn, outbox_id) == "ACKNOWLEDGED"
    assert item_ids(a.sync() + a.drain(0.2), "outbox.cleared") == []  # not to the acker


def test_offline_node_sees_acked_item_only_in_history(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    b = enrolled(gw)
    b.close()
    outbox_id = str(item(app_conn))
    collect(a, lambda e: outbox_id in item_ids(e))
    a.ack(uuid.UUID(outbox_id))
    a.sync()
    b2 = reborn(gw, b)
    welcome = b2.hello()
    assert all(outbox_id not in g["outbox_ids"] for g in welcome["summary"])
    assert outbox_id not in item_ids(b2.sync() + b2.drain(0.2))
    assert b2.displays[outbox_id] == 0


def test_resend_after_two_minutes_same_id(gw: Gw, app_conn: Conn) -> None:
    """P2-E04: the sweep re-sends an unconfirmed item with the same ID; shown once."""
    a = enrolled(gw)
    a.auto_received = False
    outbox_id = str(item(app_conn))
    collect(a, lambda e: outbox_id in item_ids(e))
    _sweep(gw)
    assert item_ids(a.drain(0.2)) == []  # not yet two minutes
    gw.clock.advance(150)
    again = collect(a, lambda e: outbox_id in item_ids(e))  # the 60 s ticker's sweep
    assert item_ids(again) == [outbox_id]
    assert attempts(app_conn, outbox_id, a.node_id) == 2
    assert a.displays[outbox_id] == 1


def test_outbox_survives_restart(ddb: Dsns, tmp_path: Path, app_conn: Conn) -> None:
    """P2-E03: a PENDING item reaches a node that connects to a new gateway process."""
    with gateway(ddb, tmp_path) as first:
        a = enrolled(first)
        a.close()
    outbox_id = str(item(app_conn))
    assert state(app_conn, outbox_id) == "PENDING"
    with gateway(ddb, tmp_path, keys_from=first) as second:
        b = reborn(second, a)
        b.hello()
        assert item_ids(b.sync()) == [outbox_id]
        b.sync()
        assert state(app_conn, outbox_id) == "DELIVERED"


def test_second_connection_supersedes_first(gw: Gw, app_conn: Conn) -> None:
    """Review Focus 3: the first socket is closed `superseded`; each item reaches the node
    once in total."""
    a1 = enrolled(gw)
    before = str(item(app_conn))
    collect(a1, lambda e: before in item_ids(e))
    a2 = reborn(gw, a1)
    a2.hello()
    first_frames = closed(a1)
    assert close_reason(app_conn, a1.node_id)[-2] == "superseded"
    after = str(item(app_conn))
    second_frames = collect(a2, lambda e: after in item_ids(e))
    total = Counter(item_ids(first_frames) + item_ids(second_frames)) + Counter([before])
    assert total == Counter({before: 1, after: 1})
    assert attempts(app_conn, before, a1.node_id) == 1
    assert attempts(app_conn, after, a1.node_id) == 1


def test_frame_written_only_after_fenced_record(
    gw: Gw, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = relay.audited_call_async

    async def failing(conn: Any, sql: Any, params: Any) -> Any:
        if "record_sent" in sql:
            raise _FencedError("injected connection_fenced")
        return await real(conn, sql, params)

    a = enrolled(gw)
    monkeypatch.setattr(relay, "audited_call_async", failing)
    outbox_id = str(item(app_conn))
    assert item_ids(closed(a)) == []  # fenced: no frame, and the socket closed
    assert attempts(app_conn, outbox_id, a.node_id) is None


def test_revoke_during_delivery(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8-1 end to end, in both orders."""
    # 1. The revocation commits first: record_sent is fenced, and nothing is written. The
    #    re-check is slow here, so only the fenced delivery can close the socket.
    with gateway(ddb, tmp_path, recheck_interval=60) as g:
        a = enrolled(g)
        nodes.revoke(app_conn, a.node_id)
        outbox_id = str(item(app_conn))
        assert item_ids(closed(a)) == []
        assert attempts(app_conn, outbox_id, a.node_id) is None

    # 2. record_sent takes its locks first: the revocation waits for it, the frame is written
    #    after its commit, then the re-check closes the socket within one interval.
    with gateway(ddb, tmp_path, recheck_interval=0.2) as g:
        b = enrolled(g)
        holding = threading.Event()
        release = threading.Event()
        real = relay.audited_call_async

        async def slow(conn: Any, sql: Any, params: Any) -> Any:
            if "record_sent" not in sql:
                return await real(conn, sql, params)
            async with conn.transaction():
                cur = await conn.execute(sql, params)
                row = await cur.fetchone()
                holding.set()
                await asyncio.to_thread(release.wait, 10)
            return tuple(row)

        monkeypatch.setattr(relay, "audited_call_async", slow)
        outbox_id = str(item(app_conn))
        assert holding.wait(5)
        revoker = InThread(ddb.app, lambda c: nodes.revoke(c, b.node_id))
        revoker.wait_until_blocked(ddb.superuser)
        release.set()
        frames = closed(b)
        revoker.join()
        assert item_ids(frames) == [outbox_id]
        assert attempts(app_conn, outbox_id, b.node_id) == 1


def test_old_socket_is_fenced(gw: Gw, app_conn: Conn, ddb: Dsns) -> None:
    """A connection superseded elsewhere (another gateway process): this process still has
    the old socket. Its delivery is fenced, and so is everything it sends."""
    a = enrolled(gw)
    with psycopg.connect(ddb.gateway, autocommit=True) as other:
        other.execute("SELECT gw.open_connection(%s)", (a.node_id,))
    outbox_id = str(item(app_conn))
    assert item_ids(closed(a)) == []  # no outbox.item on the old socket
    assert attempts(app_conn, outbox_id, a.node_id) is None

    a2 = reborn(gw, a)
    a2.auto_received = False
    a2.hello()
    assert item_ids(a2.sync()) == [outbox_id]
    with psycopg.connect(ddb.gateway, autocommit=True) as other:
        other.execute("SELECT gw.open_connection(%s)", (a.node_id,))
    message_id = a2.send("utterance", {"text": "show me the news", "screen_summary": None})
    a2.ack(uuid.UUID(outbox_id))
    a2.received(uuid.UUID(outbox_id))
    closed(a2)
    assert app_conn.execute(
        "SELECT count(*) FROM conv.inbound WHERE id = %s", (message_id,)
    ).fetchone() == (0,)
    assert state(app_conn, outbox_id) == "DELIVERING"
    row = app_conn.execute(
        "SELECT received_at FROM comm.delivery WHERE outbox_id = %s", (outbox_id,)
    ).fetchone()
    assert row == (None,)


# ------------------------------------------------------------------------------ P2-E13


def _e13_trace_line(
    run: int, ops: list[str], frames: dict[str, list[dict[str, Any]]], item_id: str
) -> dict[str, Any]:
    def symbolic(env: dict[str, Any]) -> dict[str, Any] | None:
        if env["kind"] not in ("outbox.item", "outbox.cleared"):
            return None
        oid = env["payload"]["outbox_id"]
        out = {"kind": env["kind"], "outbox_id": "this" if oid == item_id else "earlier"}
        if env["kind"] == "outbox.cleared":
            out["reason"] = env["payload"]["reason"]
        return out

    return {
        "run": run,
        "ops": ops,
        "frames": {
            name: [s for s in (symbolic(e) for e in envs) if s is not None]
            for name, envs in frames.items()
        },
        "expect": "each outbox_id displayed at most once per node, never after outbox.cleared",
    }


E13_RUNS = int(os.environ.get("JARVIS_E13_RUNS", "200"))  # JARVIS_E13_RUNS=1000: the spec's count
OPEN_STATES = ("PENDING", "DELIVERING", "DELIVERED")


@dataclass
class _Connection:
    """One connection instance of a node in P2-E13: the frames the gateway sent it, and the
    sweeps that ran while it was connected, with the items unconfirmed by its node then."""

    node: str
    fake: FakeNode
    frames: list[dict[str, Any]] = field(default_factory=list)
    sweeps: list[set[str]] = field(default_factory=list)


def _unconfirmed(app: Conn, node: uuid.UUID | None, items: list[str]) -> set[str]:
    """The items `node` has not confirmed receipt of (no delivery row, or not received)."""
    confirmed = {
        str(r[0])
        for r in app.execute(
            "SELECT outbox_id FROM comm.delivery WHERE node_id = %s AND received_at IS NOT NULL",
            (node,),
        ).fetchall()
    }
    return set(items) - confirmed


def _assert_e13_gateway_side(
    app: Conn, connections: list[_Connection], items: list[str], terminal: dict[str, str | None]
) -> None:
    """The gateway's own P2-E13 properties (§8.2), over the frames it sent, per connection:
    nothing after `outbox.cleared`, one outbox_id for an item on every send, at most one send
    per connection plus one per sweep that found it unconfirmed, and DB state consistent."""
    payloads: dict[str, dict[str, Any]] = {}
    cleared: dict[str, set[str]] = {}
    frames_to: Counter[tuple[str, str]] = Counter()
    for c in connections:
        gone = cleared.setdefault(c.node, set())
        sends: Counter[str] = Counter()
        for env in c.frames:
            if env["kind"] == "outbox.cleared":
                gone.add(env["payload"]["outbox_id"])
            elif env["kind"] == "outbox.item":
                oid = env["payload"]["outbox_id"]
                assert oid in items, oid  # never a new ID for a retry
                assert oid not in gone, ("sent after outbox.cleared", c.node, oid)
                # Every send of an item, on every node and retry, is the same item.
                assert payloads.setdefault(oid, env["payload"]) == env["payload"], oid
                sends[oid] += 1
                frames_to[(c.node, oid)] += 1
        for oid, n in sends.items():
            allowed = 1 + sum(oid in unconfirmed for unconfirmed in c.sweeps)
            assert n <= allowed, ("re-sent on one connection without a due sweep", c.node, oid)
    nodes_by_name = {c.node: c.fake.node_id for c in connections}
    for oid in items:
        state = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (oid,)).fetchone()
        assert state is not None
        expected = terminal[oid]
        assert state[0] == expected if expected else state[0] in OPEN_STATES, (oid, state)
        rows = app.execute(
            "SELECT action::text, outcome::text, seq, from_state, to_state FROM audit.event"
            " WHERE entity_type = 'outbox' AND entity_id = %s ORDER BY seq",
            (oid,),
        ).fetchall()
        applied_sends = [seq for action, outcome, seq, _, _ in rows
                         if action == "SEND" and outcome == "APPLIED"]  # fmt: skip
        # The one move into a terminal state (a later ack elsewhere is a 'late' no-op).
        ends = [seq for action, outcome, seq, before, after in rows
                if outcome == "APPLIED" and before in OPEN_STATES
                and after in ("ACKNOWLEDGED", "SUPERSEDED", "EXPIRED")]  # fmt: skip
        assert len(ends) == (1 if expected else 0), (oid, rows)
        if ends:  # never sent once terminal
            assert all(seq < ends[0] for seq in applied_sends), (oid, rows)
        total_attempts = 0
        for name, node_id in nodes_by_name.items():
            got = attempts(app, oid, node_id) or 0
            total_attempts += got
            assert got >= frames_to[(name, oid)], (name, oid)  # every frame was recorded first
        assert total_attempts == len(applied_sends), oid


def test_display_once_per_node_across_retry_and_reconnect(
    ddb: Dsns, tmp_path: Path, app_conn: Conn
) -> None:
    """P2-E13: two nodes, randomised runs (200; `JARVIS_E13_RUNS=1000` for the spec's count)
    of re-send sweep, reconnect, ack on either node and supersede, with each node confirming
    receipt or not at random.

    Node half: for every node and item, displayed at most once, never after its
    outbox.cleared. Gateway half (final review I-3), over the frames the gateway sent and the
    database, independent of FakeNode's own dedup: no `outbox.item` after `outbox.cleared` for
    it on that node; the same outbox_id and item on every retry; per connection at most one
    send plus one per sweep that found it unconfirmed; and each run's final `comm.outbox` state,
    audit trail and `comm.delivery` attempts consistent with the ops.

    With JARVIS_WRITE_TRACES=1 the runs are written to tests/fakes/traces/e13.jsonl (committed)
    for Plan B to replay against the Rust node: per run the ops and, per node, the outbox frames
    it received, with IDs made symbolic."""
    rng = random.Random(13)  # noqa: S311  # a reproducible scenario, not crypto
    trace: list[dict[str, Any]] = []
    items: list[str] = []
    terminal: dict[str, str | None] = {}
    with gateway(ddb, tmp_path, tick_interval=3600) as g:  # the sweep runs only when called
        fakes = {"A": enrolled(g), "B": enrolled(g)}
        history: dict[str, list[FakeNode]] = {k: [v] for k, v in fakes.items()}
        live = {name: _Connection(name, fake) for name, fake in fakes.items()}
        connections = list(live.values())
        for run in range(E13_RUNS):
            # A node that doesn't confirm receipt this run leaves items for the sweep to retry.
            confirms = {name: rng.random() < 0.5 for name in fakes}
            for name, fake in fakes.items():
                fake.auto_received = confirms[name]
            outbox_id = str(item(app_conn))
            items.append(outbox_id)
            terminal[outbox_id] = None
            frames: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
            ops = rng.choices(
                ["sweep", "reconnect_A", "reconnect_B", "ack_A", "ack_B", "supersede", "wait"],
                k=4,
            )
            for op in ops:
                if op == "sweep":
                    g.clock.advance(150)
                    for name, c in live.items():  # what may be re-sent, before it runs
                        c.sweeps.append(_unconfirmed(app_conn, fakes[name].node_id, items))
                    _sweep(g)
                elif op.startswith("reconnect_"):
                    name = op[-1]
                    got = fakes[name].drain(0.005)
                    frames[name] += got
                    live[name].frames += got
                    fakes[name].close()
                    fakes[name] = reborn(g, fakes[name])
                    fakes[name].auto_received = confirms[name]
                    history[name].append(fakes[name])
                    live[name] = _Connection(name, fakes[name])
                    connections.append(live[name])
                    fakes[name].hello()
                elif op.startswith("ack_"):
                    fake = fakes[op[-1]]
                    if outbox_id in fake.seen_outbox and outbox_id not in fake.cleared:
                        fake.ack(uuid.UUID(outbox_id))
                        if terminal[outbox_id] is None:  # the first end wins
                            terminal[outbox_id] = "ACKNOWLEDGED"
                            assert _wait_for(
                                lambda x=outbox_id: state(app_conn, x) == "ACKNOWLEDGED", 5
                            )
                elif op == "supersede":
                    row = app_conn.execute("SELECT comm.supersede(%s)", (outbox_id,)).fetchone()
                    assert row == ("terminal" if terminal[outbox_id] else "applied",)
                    terminal[outbox_id] = terminal[outbox_id] or "SUPERSEDED"
                for name, fake in fakes.items():
                    got = fake.drain(0.005)
                    frames[name] += got
                    live[name].frames += got
            for name, fake in fakes.items():
                got = fake.sync() + fake.drain(0.02)
                frames[name] += got
                live[name].frames += got
            trace.append(_e13_trace_line(run, ops, frames, outbox_id))
        _assert_e13_gateway_side(app_conn, connections, items, terminal)
        for name, instances in history.items():
            displays: Counter[str] = Counter()
            log: list[tuple[str, str]] = []
            for fake in instances:
                displays += fake.displays
                log += fake.log
            assert displays and max(displays.values()) == 1, name
            gone: set[str] = set()
            for what, oid in log:
                if what == "cleared":
                    gone.add(oid)
                else:
                    assert oid not in gone, (name, oid)
    if os.environ.get("JARVIS_WRITE_TRACES") == "1":
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRACE_PATH.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in trace))


# ------------------------------------------------------------------------------ welcome, heartbeat


def test_welcome_summary_order(gw: Gw, app_conn: Conn) -> None:
    """P2-E07 end to end: `welcome` carries the open session and the §8.3 summary, mapped
    onto the message schema's groups (controller ruling G2)."""
    guest = person(app_conn)
    a = enrolled(gw, guest)
    a.close()
    session = session_of(app_conn, guest)
    u1 = item(
        app_conn, who=guest, priority=Priority.URGENT, kind="approval.requested", group_key="ap-1"
    )
    t1 = item(app_conn, who=guest, kind="budget.80", group_key="budget")
    d1 = item(app_conn, who=guest, priority=Priority.DIGEST, kind="brain.down", group_key="brain")
    t2 = item(app_conn, who=guest, kind="budget.100", group_key="budget")
    u2 = item(
        app_conn, who=guest, priority=Priority.URGENT, kind="approval.expiring", group_key="ap-2"
    )
    t3 = item(app_conn, who=guest, kind="turn.indeterminate", group_key="turn")
    d2 = item(app_conn, who=guest, priority=Priority.DIGEST, kind="brain.back", group_key="brain")
    item(app_conn, who=guest, priority=Priority.SILENT, kind="action.readonly_ok", group_key="x")
    b = reborn(gw, a)
    welcome = b.hello()
    Welcome.model_validate(welcome)  # the schema's shape, nothing extra
    assert welcome["session_id"] == str(session)
    assert welcome["summary"] == [
        {"priority": "URGENT", "group_key": "ap-1", "outbox_ids": [str(u1)],
         "line": "Approval waiting for your decision"},
        {"priority": "URGENT", "group_key": "ap-2", "outbox_ids": [str(u2)],
         "line": "Approval expires in 2 minutes"},
        {"priority": "TODAY", "group_key": "budget", "outbox_ids": [str(t1), str(t2)],
         "line": "Claude budget 80% used this month (2)"},
        {"priority": "TODAY", "group_key": "turn", "outbox_ids": [str(t3)],
         "line": "A request ended without a clear outcome"},
        {"priority": "DIGEST", "group_key": "brain", "outbox_ids": [str(d1), str(d2)],
         "line": "Claude is available again (2)"},
    ]  # fmt: skip


def test_welcome_summary_offers_a_targeted_item_only_on_its_node(gw: Gw, app_conn: Conn) -> None:
    """Final review I-1, R12-23: an URGENT `action.approved` item addressed to node A is in A's
    `welcome` summary, never in B's; an untargeted item is in both."""
    guest = person(app_conn)
    a = enrolled(gw, guest)
    b = enrolled(gw, guest)
    a.close()
    b.close()
    targeted = item(
        app_conn, who=guest, priority=Priority.URGENT, kind="action.approved",
        group_key="action:x", target=a.node_id,
    )  # fmt: skip
    everyone = item(app_conn, who=guest, kind="budget.80", group_key="budget")
    summary_a = reborn(gw, a).hello()["summary"]
    summary_b = reborn(gw, b).hello()["summary"]
    assert summary_a == [
        {"priority": "URGENT", "group_key": "action:x", "outbox_ids": [str(targeted)],
         "line": summary.LINE["action.approved"]},
        {"priority": "TODAY", "group_key": "budget", "outbox_ids": [str(everyone)],
         "line": summary.LINE["budget.80"]},
    ]  # fmt: skip
    assert summary_b == [summary_a[1]]


def test_welcome_without_open_session(gw: Gw, app_conn: Conn) -> None:
    guest = person(app_conn)
    a = enrolled(gw, guest)
    a.close()
    assert reborn(gw, a).hello() == {"session_id": None, "summary": []}


def test_summary_and_delivery_continue_while_claude_is_down(gw: Gw, app_conn: Conn) -> None:
    """P2-E08 and the delivery half of P2-B1, end to end: with the brain marked DOWN, a
    reconnecting device still gets its `welcome` summary (built by code), the queued item
    and a new live item."""
    from jarvis_server.brain import health

    guest = person(app_conn)
    a = enrolled(gw, guest)
    a.close()
    health.record_failure(app_conn)
    health.record_failure(app_conn)
    try:
        assert health.is_down(app_conn)
        down = item(app_conn, who=guest, priority=Priority.DIGEST, kind="brain.down",
                    group_key="brain")  # fmt: skip
        queued = item(app_conn, who=guest, kind="budget.80", group_key="budget")
        b = reborn(gw, a)
        welcome = b.hello()
        assert welcome["summary"] == [
            {"priority": "TODAY", "group_key": "budget", "outbox_ids": [str(queued)],
             "line": summary.LINE["budget.80"]},
            {"priority": "DIGEST", "group_key": "brain", "outbox_ids": [str(down)],
             "line": summary.DIGEST_LINE.format(label=summary.LINE["brain.down"], count=1)},
        ]  # fmt: skip
        assert sorted(item_ids(b.sync())) == sorted([str(down), str(queued)])
        live = str(item(app_conn, who=guest))
        assert live in item_ids(collect(b, lambda e: live in item_ids(e)))
        assert health.is_down(app_conn)  # nothing above needed Claude
    finally:
        health.record_success(app_conn)


def test_gateway_process_loads_no_brain_or_memory_code() -> None:
    """P2-E08, P2-B1 (delivery) and P2-S1: importing the gateway's entry point in a fresh
    interpreter loads no brain, memory, turn or policy module and no HTTP client, so delivery,
    acknowledgement and the reconnect summary can't depend on Claude being reachable."""
    import subprocess
    import sys

    probe = "import json, sys, jarvis_server.gateway.app\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True
    )
    loaded = json.loads(out.stdout)
    assert "jarvis_server.gateway.app" in loaded and "jarvis_server.core.summary" in loaded
    forbidden = (
        "jarvis_server.brain", "jarvis_server.memory", "jarvis_server.core.turn",
        "jarvis_server.core.approvals", "jarvis_server.policy", "jarvis_server.atlas",
        "httpx", "anthropic",
    )  # fmt: skip
    prefixes = tuple(f + "." for f in forbidden)
    assert [m for m in loaded if m in forbidden or m.startswith(prefixes)] == []


def test_three_missed_heartbeats_close_connection(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    a.answer_pings = False
    for _ in range(3):
        gw.clock.advance(30)
        assert collect(a, lambda e: any(x["kind"] == "ping" for x in e))
    _wait_for(lambda: close_reason(app_conn, a.node_id) == [None])  # still open
    gw.clock.advance(30)
    closed(a)
    assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["heartbeat"])
    row = app_conn.execute(
        "SELECT closed_at IS NOT NULL FROM gw.node_connection WHERE node_id = %s", (a.node_id,)
    ).fetchone()
    assert row == (True,)


def test_answered_heartbeats_keep_connection(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    for _ in range(5):
        gw.clock.advance(30)
        collect(a, lambda e: any(x["kind"] == "ping" for x in e))  # answered with pong
    a.sync()
    assert close_reason(app_conn, a.node_id) == [None]


# ------------------------------------------------------------------------------ fix round 1


@contextmanager
def stuck_sends(monkeypatch: pytest.MonkeyPatch, fake: FakeNode) -> Iterator[threading.Event]:
    """From when the returned event is set, every send to `fake`'s socket blocks forever, as
    for a peer that stopped reading (its TCP window full). Other sockets are untouched."""
    stuck = threading.Event()
    real = WebSocket.send_text

    async def send_text(self: WebSocket, data: str) -> None:
        if stuck.is_set() and self.client is not None and self.client.host == fake.peer_ip:
            await asyncio.Event().wait()  # never set
        await real(self, data)

    monkeypatch.setattr(WebSocket, "send_text", send_text)
    yield stuck


def _audit_reasons(dsns: Dsns, node: uuid.UUID | None, since: int) -> list[Any]:
    with psycopg.connect(dsns.app, autocommit=True) as other:  # a separate connection
        rows = other.execute(
            "SELECT action::text, details->>'reason' FROM audit.event WHERE seq > %s"
            " AND action::text = 'DISCONNECT' AND details->>'node_id' = %s ORDER BY seq",
            (since, str(node)),
        ).fetchall()
    return [tuple(r) for r in rows]


def test_stuck_socket_does_not_stall_other_nodes(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-1: a node whose sends block forever. Another node of the same person still gets a
    live chunk and an outbox.item promptly, and its heartbeat and sweep keep running. Then the
    stuck node is closed `send_timeout` on the injected timeout, with its DISCONNECT row and no
    open connection row, checked from a separate connection."""
    with gateway(ddb, tmp_path, send_timeout=3.0) as g:
        stuck_node = enrolled(g)
        healthy = enrolled(g)
        healthy.auto_received = False  # leaves the item for the sweep to re-send
        since = audit_seq(app_conn)
        with stuck_sends(monkeypatch, stuck_node) as stuck:
            stuck.set()
            started = datetime.now(UTC)
            reply = uuid.uuid4()
            speech(app_conn, reply, [1])
            outbox_id = str(item(app_conn))
            collect(
                healthy,
                lambda e: chunk_seqs(e, reply) == [1] and outbox_id in item_ids(e),
                timeout=1.5,
            )
            g.clock.advance(150)  # a heartbeat and a sweep fall due for both nodes
            collect(
                healthy,
                lambda e: any(x["kind"] == "ping" for x in e) and outbox_id in item_ids(e),
                timeout=1.5,
            )
            elapsed = (datetime.now(UTC) - started).total_seconds()
            assert elapsed < 3.0, elapsed  # all of it before the stuck send timed out
            assert close_reason(app_conn, stuck_node.node_id) == [None]  # still stuck, open
            assert attempts(app_conn, outbox_id, healthy.node_id) == 2
            assert healthy.displays[outbox_id] == 1

            assert _wait_for(
                lambda: close_reason(app_conn, stuck_node.node_id) == ["send_timeout"], 6
            )
        assert _audit_reasons(ddb, stuck_node.node_id, since) == [("DISCONNECT", "send_timeout")]
        with psycopg.connect(ddb.app, autocommit=True) as other:
            row = other.execute(
                "SELECT count(*) FROM gw.node_connection WHERE node_id = %s AND closed_at IS NULL",
                (stuck_node.node_id,),
            ).fetchone()
        assert row == (0,)
        assert _wait_for(lambda: stuck_node.node_id not in _relay(g)._links)
        assert close_reason(app_conn, healthy.node_id) == [None]
        healthy.sync()  # still served


def test_missed_heartbeats_close_while_send_blocked(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner requirement 1: missed-heartbeat detection, the close through gw.close_connection
    and cancelling the link's queue and sender all work while its send is blocked (the send
    timeout here is far longer than the test)."""
    with gateway(ddb, tmp_path, send_timeout=600) as g:
        a = enrolled(g)
        with stuck_sends(monkeypatch, a) as stuck:
            stuck.set()
            link = _relay(g)._links[a.node_id]
            for _ in range(4):
                g.clock.advance(30)
                _wait_for(lambda: False, 0.1)  # let the ticker see each step
            assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["heartbeat"], 5)
            assert _wait_for(lambda: link._sender.done(), 5)
            assert a.node_id not in _relay(g)._links


def test_close_connection_failure_after_send_timeout(
    ddb: Dsns,
    tmp_path: Path,
    app_conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner requirement 3: recording the disconnect fails; it is logged, and the link still
    stops and the socket still closes."""
    from jarvis_server.gateway import session as gateway_session

    real = gateway_session.audited_call_async

    async def failing(conn: Any, sql: Any, params: Any) -> Any:
        if "close_connection" in sql and params[1] == "send_timeout":
            raise psycopg.errors.InternalError("injected")
        return await real(conn, sql, params)

    with gateway(ddb, tmp_path, send_timeout=0.5) as g:
        a = enrolled(g)
        link = _relay(g)._links[a.node_id]
        monkeypatch.setattr(gateway_session, "audited_call_async", failing)
        with stuck_sends(monkeypatch, a) as stuck:
            stuck.set()
            item(app_conn)
            assert _wait_for(lambda: link._sender.done(), 5)
            assert _wait_for(lambda: a.node_id not in _relay(g)._links, 5)
        assert _wait_for(lambda: "could not close connection" in caplog.text, 5)
        assert close_reason(app_conn, a.node_id) == [None]  # the row stays for the next connect
        closed(a)  # the socket was closed regardless


def test_close_connection_failure_after_missed_heartbeats(
    gw: Gw, app_conn: Conn, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Owner final-review area 3, the heartbeat path: recording the `heartbeat` disconnect
    fails. It is logged, the link stops and the socket still closes; the row left open is
    closed `superseded` when the device next connects, which then works normally."""
    from jarvis_server.gateway import session as gateway_session

    real = gateway_session.audited_call_async

    async def failing(conn: Any, sql: Any, params: Any) -> Any:
        if "close_connection" in sql and params[1] == "heartbeat":
            raise psycopg.errors.InternalError("injected")
        return await real(conn, sql, params)

    a = enrolled(gw)
    a.answer_pings = False
    monkeypatch.setattr(gateway_session, "audited_call_async", failing)
    for _ in range(4):
        gw.clock.advance(30)
        _wait_for(lambda: False, 0.1)  # let the ticker see each step
    closed(a)
    assert _wait_for(lambda: a.node_id not in _relay(gw)._links, 5)
    assert _wait_for(lambda: "could not close connection" in caplog.text, 5)
    assert close_reason(app_conn, a.node_id) == [None]  # nothing recorded
    monkeypatch.setattr(gateway_session, "audited_call_async", real)
    again = reborn(gw, a)
    again.hello()
    assert close_reason(app_conn, a.node_id) == ["superseded", None]
    outbox_id = str(item(app_conn))
    assert outbox_id in item_ids(collect(again, lambda e: outbox_id in item_ids(e)))


# ------------------------------------------------------------------------------ final review I-2
#
# Every way an established connection ends goes through gw.close_connection with a reason:
# one DISCONNECT row, and no connection row left open (checked from a separate connection).


def _open_rows(dsns: Dsns, node: uuid.UUID | None = None) -> int:
    with psycopg.connect(dsns.app, autocommit=True) as other:
        row = other.execute(
            "SELECT count(*) FROM gw.node_connection WHERE closed_at IS NULL"
            " AND (%s::uuid IS NULL OR node_id = %s::uuid)",
            (node, node),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _disconnects(dsns: Dsns, since: int) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.app, autocommit=True) as other:
        rows = other.execute(
            "SELECT details->>'node_id', details->>'reason' FROM audit.event"
            " WHERE seq > %s AND action::text = 'DISCONNECT' ORDER BY seq",
            (since,),
        ).fetchall()
    return [tuple(r) for r in rows]


@contextmanager
def failing_sends(
    monkeypatch: pytest.MonkeyPatch, fake: FakeNode, *, kind: str, error: Callable[[], Exception]
) -> Iterator[None]:
    """Every send of a `kind` envelope to `fake`'s socket raises `error()` (what Starlette
    raises once the peer has gone) or, for "stuck", never finishes."""
    real = WebSocket.send_text
    marker = f'"kind": "{kind}"'

    async def send_text(self: WebSocket, data: str) -> None:
        if self.client is not None and self.client.host == fake.peer_ip and marker in data:
            raise error()
        await real(self, data)

    monkeypatch.setattr(WebSocket, "send_text", send_text)
    yield


def _stuck_sends_of(monkeypatch: pytest.MonkeyPatch, fake: FakeNode, kind: str) -> None:
    real = WebSocket.send_text
    marker = f'"kind": "{kind}"'

    async def send_text(self: WebSocket, data: str) -> None:
        if self.client is not None and self.client.host == fake.peer_ip and marker in data:
            await asyncio.Event().wait()  # never set: a peer that stopped reading
        await real(self, data)

    monkeypatch.setattr(WebSocket, "send_text", send_text)


def test_reply_to_a_gone_peer_records_the_disconnect(
    gw: Gw, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (a): the `pong` to a `ping` fails because the peer has gone (Starlette raises
    WebSocketDisconnect): closed `node_closed`, nothing left open."""
    a = enrolled(gw)
    since = audit_seq(app_conn)
    with failing_sends(monkeypatch, a, kind="pong", error=lambda: WebSocketDisconnect(1006)):
        a.send("ping", {})
        assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["node_closed"], 5)
    assert _disconnects(gw.dsns, since) == [(str(a.node_id), "node_closed")]
    assert _open_rows(gw.dsns, a.node_id) == 0


def test_error_reply_timeout_after_a_db_error_records_the_disconnect(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (b): a database error on an inbound message, and the `error server_error` reply
    to it times out: still closed `server_error`, nothing left open."""
    with gateway(ddb, tmp_path, send_timeout=0.5) as g:
        a = enrolled(g)
        since = audit_seq(app_conn)
        _stuck_sends_of(monkeypatch, a, "error")
        # Valid for the Utterance schema, but Postgres jsonb refuses U+0000.
        a.send("utterance", {"text": "a\u0000b", "screen_summary": None})
        assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["server_error"], 5)
        assert _disconnects(ddb, since) == [(str(a.node_id), "server_error")]
        assert _open_rows(ddb, a.node_id) == 0


def test_unexpected_error_in_the_receive_loop_records_the_disconnect(
    gw: Gw, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (c): an exception that escapes the receive loop (here, a fault in handling a
    message) is no longer lost before gw.close_connection: closed `server_error`, the socket
    closed, nothing left open."""
    from jarvis_server.gateway import session as gateway_session

    a = enrolled(gw)
    since = audit_seq(app_conn)

    async def broken(self: Any, text: Any, data: Any) -> None:
        raise ValueError("a fictional fault")

    monkeypatch.setattr(gateway_session.Session, "_handle", broken)
    a.send("ping", {})
    closed(a)
    assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["server_error"], 5)
    assert _disconnects(gw.dsns, since) == [(str(a.node_id), "server_error")]
    assert _open_rows(gw.dsns, a.node_id) == 0


def test_welcome_send_timeout_records_the_disconnect(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (d), path A: the row is open and `welcome` times out: closed `send_timeout`."""
    with gateway(ddb, tmp_path, send_timeout=0.5) as g:
        a = enrolled(g)
        a.close()
        assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["node_closed"], 5)
        b = reborn(g, a)
        _stuck_sends_of(monkeypatch, b, "welcome")
        since = audit_seq(app_conn)
        b.connect()
        assert b.challenge is not None
        b.send("hello", {"nonce": b.challenge["nonce"], "resume": []})
        assert _wait_for(lambda: close_reason(app_conn, a.node_id)[-1:] == ["send_timeout"], 5)
        assert _disconnects(ddb, since) == [(str(a.node_id), "send_timeout")]
        assert _open_rows(ddb, a.node_id) == 0


def test_welcome_db_failure_records_the_disconnect(
    gw: Gw, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (d): the row is open and building `welcome` fails in the database: closed
    `server_error`, nothing left open."""
    a = enrolled(gw)
    a.close()
    assert _wait_for(lambda: close_reason(app_conn, a.node_id) == ["node_closed"], 5)
    b = reborn(gw, a)

    async def failing(*args: Any) -> Any:
        raise psycopg.errors.InternalError("injected")

    monkeypatch.setattr(relay, "welcome", failing)
    since = audit_seq(app_conn)
    with pytest.raises(WebSocketDisconnect):
        b.hello()
    assert _wait_for(lambda: close_reason(app_conn, a.node_id)[-1:] == ["server_error"], 5)
    assert _disconnects(gw.dsns, since) == [(str(a.node_id), "server_error")]
    assert _open_rows(gw.dsns, a.node_id) == 0


def test_enrolment_welcome_send_timeout_records_the_disconnect(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (d), path B: a new node is enrolled and its row opened, and the enrolment
    `welcome` times out: closed `send_timeout`, nothing left open."""
    with gateway(ddb, tmp_path, send_timeout=0.5) as g:
        fake = g.node()
        _stuck_sends_of(monkeypatch, fake, "welcome")
        since = audit_seq(app_conn)
        with pytest.raises((TimeoutError, WebSocketDisconnect)):
            fake.enrol(g.code())

        def node_closed() -> list[tuple[Any, ...]]:
            return _disconnects(ddb, since)

        assert _wait_for(lambda: [r[1] for r in node_closed()] == ["send_timeout"], 5)
        [(node_id, _)] = node_closed()
        assert close_reason(app_conn, uuid.UUID(node_id)) == ["send_timeout"]
        assert _open_rows(ddb, uuid.UUID(node_id)) == 0


def test_confirmation_is_per_device(ddb: Dsns, tmp_path: Path, app_conn: Conn) -> None:
    """Owner ruling: A confirmed, B didn't. Past the re-send interval B gets the same
    outbox_id again and A doesn't; after both reconnect, the same per-device outcome. The
    ticker is off so each pass is exactly the one this test runs."""
    with gateway(ddb, tmp_path, tick_interval=3600) as gw:
        _per_device(gw, app_conn)


def _per_device(gw: Gw, app_conn: Conn) -> None:
    a = enrolled(gw)
    b = enrolled(gw)
    b.auto_received = False
    outbox_id = str(item(app_conn))
    collect(a, lambda e: outbox_id in item_ids(e))
    collect(b, lambda e: outbox_id in item_ids(e))
    a.sync()
    assert state(app_conn, outbox_id) == "DELIVERED"

    def resent_once_more() -> tuple[list[str], list[str]]:
        gw.clock.advance(150)
        _sweep(gw)
        got_b = item_ids(collect(b, lambda e: outbox_id in item_ids(e)))
        got_a = item_ids(a.sync() + a.drain(0.2))
        return got_a, got_b

    assert resent_once_more() == ([], [outbox_id])
    assert attempts(app_conn, outbox_id, a.node_id) == 1
    assert attempts(app_conn, outbox_id, b.node_id) == 2

    a.close()
    b.close()
    a = reborn(gw, a)
    b = reborn(gw, b)
    b.auto_received = False
    a.hello()
    b.hello()
    # On connect only never-sent items go out; B's unconfirmed item stays eligible for the pass.
    assert item_ids(a.sync()) == [] and item_ids(b.sync()) == []
    assert resent_once_more() == ([], [outbox_id])
    assert attempts(app_conn, outbox_id, a.node_id) == 1
    assert attempts(app_conn, outbox_id, b.node_id) == 3
    assert a.displays[outbox_id] == 0 and b.displays[outbox_id] == 0  # seen-lists hold


def test_shutdown_leaves_no_pending_tasks(
    ddb: Dsns,
    tmp_path: Path,
    app_conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner requirement 2: stopping the gateway with a healthy link, a link whose send is
    blocked and queued work cancels every sender and task cleanly: nothing left pending, no
    "Task was destroyed but it is pending" and no unretrieved task exception."""
    caplog.set_level(logging.ERROR, logger="asyncio")
    with gateway(ddb, tmp_path, send_timeout=600) as g:
        healthy = enrolled(g)
        blocked = enrolled(g)
        links = [_relay(g)._links[f.node_id] for f in (healthy, blocked)]
        with stuck_sends(monkeypatch, blocked) as stuck:
            stuck.set()
            item(app_conn)
            item(app_conn)  # the second one waits in the blocked link's queue
            collect(healthy, lambda e: len(item_ids(e)) >= 2)
            relay_tasks = list(_relay(g)._tasks)
    gc.collect()
    assert all(link._sender.done() for link in links)
    assert all(task.done() for task in relay_tasks)
    bad = [
        r.getMessage()
        for r in caplog.records
        if "destroyed but it is pending" in r.getMessage() or "never retrieved" in r.getMessage()
    ]
    assert bad == []


def test_work_dropped_at_shutdown_is_delivered_after_restart(
    ddb: Dsns, tmp_path: Path, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner final-review area 4: the queue a shutdown cancels holds work, not state. A
    device whose send was blocked at shutdown, with a second item still queued behind it,
    reconnects to a new gateway process: the never-sent item comes with the connect, the
    one whose send was cut off comes with the re-send pass, and each is shown once."""
    with gateway(ddb, tmp_path, send_timeout=600, tick_interval=3600) as first:
        guest = person(app_conn)  # its own person: no other test's items in the way
        blocked = enrolled(first, guest)
        link = _relay(first)._links[blocked.node_id]
        with stuck_sends(monkeypatch, blocked) as stuck:
            stuck.set()
            cut = str(item(app_conn, who=guest))
            assert _wait_for(lambda: attempts(app_conn, cut, blocked.node_id) == 1, 5)
            queued = str(item(app_conn, who=guest))  # waits behind the blocked send
            assert _wait_for(lambda: link._queue.qsize() >= 1, 5)
        assert attempts(app_conn, queued, blocked.node_id) is None
    stuck.clear()
    assert link._sender.done() and link._queue.empty()
    with gateway(ddb, tmp_path, keys_from=first, tick_interval=3600) as second:
        again = reborn(second, blocked)
        again.hello()
        assert item_ids(again.sync()) == [queued]  # never sent: offered on connect
        second.clock.advance(150)
        _sweep(second)
        assert item_ids(collect(again, lambda e: cut in item_ids(e))) == [cut]
        again.sync()
        assert again.displays[cut] == 1 and again.displays[queued] == 1
        assert state(app_conn, cut) == "DELIVERED" and state(app_conn, queued) == "DELIVERED"
