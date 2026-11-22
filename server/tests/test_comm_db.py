"""Conversations, events, outbox and multi-node delivery (migration 0009; spec §4.1, §5.2,
§5.3, §8.2; R7-1, R8-1).

Runs in its own database: comm.event, conv.inbound and outbox audit rows block the 0009
downgrade, so they must never reach the session database that P0-6 takes down and up. People,
keys and Tailscale identities are fictional test values.
"""

from __future__ import annotations

import random
import re
import secrets
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server import comm, dbcall, migrate, nodes
from jarvis_server.bootstrap import GATEWAY_ROLE
from jarvis_server.comm import OutboxState, Priority
from jarvis_server.contracts import OutboxPriority
from jarvis_server.dbcall import FENCED_SQLSTATE
from jarvis_server.signing import fingerprint

from .conftest import Dsns, fresh_database
from .helpers.threads import InThread, run_all

Conn = psycopg.Connection[Any]

MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
CFP = fingerprint(bytes(range(32)))
GFP = fingerprint(bytes(range(32, 64)))

ACTIONS_0008 = [
    "CREATE",
    "TRANSITION",
    "UPDATE",
    "CONFIRM",
    "ARCHIVE",
    "ENROL_CREATE",
    "ENROL",
    "REVOKE",
    "CONNECT_REFUSED",
    "CONNECT",
    "DISCONNECT",
]

CONN_FUNCTIONS = {
    "conv.record_inbound(bigint,uuid,text,jsonb)",
    "comm.record_sent(bigint,uuid)",
    "comm.record_received(bigint,uuid)",
    "comm.acknowledge(bigint,uuid)",
    "conv.record_chunk_received(bigint,uuid,integer)",
}

SENT = "SELECT comm.record_sent(%s, %s)"
RECEIVED = "SELECT comm.record_received(%s, %s)"
ACK = "SELECT comm.acknowledge(%s, %s)"
INBOUND = "SELECT recorded, refusal FROM conv.record_inbound(%s, %s, %s, %s)"
CHUNK = "SELECT conv.record_chunk_received(%s, %s, %s)"

TERMINAL = {"ACKNOWLEDGED", "SUPERSEDED", "EXPIRED"}
ROW_LOCK_WAITS = ("transactionid", "tuple")  # pg_stat_activity.wait_event of a row-lock wait
RANK = {"PENDING": 0, "DELIVERING": 1, "DELIVERED": 2, "ACKNOWLEDGED": 3}


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    """Overrides the session database for this module (and its app/gateway/owner fixtures)."""
    with fresh_database() as d:
        yield d


# --- helpers -----------------------------------------------------------------------------------


def _person(app: Conn) -> uuid.UUID:
    row = app.execute(
        "INSERT INTO ledger.person (display_name, organization_id, source_ref)"
        " VALUES ('Fictional Guest', %s, 'test') RETURNING id",
        (HOUSEHOLD,),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _node(app: Conn, gateway: Conn, person: uuid.UUID = MANJ) -> uuid.UUID:
    code = nodes.create_enrolment(
        app, person_id=person, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP
    )
    token, _, _ = nodes.parse_enrolment_code(code)
    return nodes.enrol(
        gateway,
        token=token,
        public_key=secrets.token_bytes(32),
        tailscale_identity=f"ts:pc-{secrets.token_hex(4)}",
    )


def _open(gateway: Conn, node_id: uuid.UUID) -> int:
    row = gateway.execute(
        "SELECT conn_id, refusal FROM gw.open_connection(%s)", (node_id,)
    ).fetchone()
    assert row is not None and row[1] is None
    return int(row[0])


def _connected(app: Conn, gateway: Conn, person: uuid.UUID = MANJ) -> tuple[uuid.UUID, int]:
    node_id = _node(app, gateway, person)
    return node_id, _open(gateway, node_id)


def _event(app: Conn, person: uuid.UUID = MANJ) -> uuid.UUID:
    return comm.record_event(
        app,
        "test.notice",
        subject_type="test",
        subject_id=f"t-{secrets.token_hex(4)}",
        person_id=person,
        details={"code": "fictional"},
    )


def _item(app: Conn, person: uuid.UUID = MANJ, **kw: Any) -> uuid.UUID:
    return comm.create_outbox(
        app,
        person_id=person,
        event_id=_event(app, person),
        priority=Priority.TODAY,
        kind="test.notice",
        body={"text": "A fictional notice"},
        group_key="test",
        expires_at=None,
        **kw,
    )


def _state(app: Conn, item: uuid.UUID) -> str:
    row = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (item,)).fetchone()
    assert row is not None
    return str(row[0])


def _one(conn: Conn, sql: Any, params: tuple[Any, ...]) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _seq(conn: Conn) -> int:
    return int(_one(conn, "SELECT coalesce(max(seq), 0) FROM audit.event", ()))


def _audit(conn: Conn, since: int, entity: str | None = None) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT entity_type, entity_id, action::text, from_state, to_state, outcome::text,"
        " reason, details FROM audit.event WHERE seq > %s"
        " AND entity_type IN ('inbound', 'outbox') AND (%s::text IS NULL OR entity_type = %s)"
        " ORDER BY seq",
        (since, entity, entity),
    ).fetchall()


def _jobs_for(app: Conn, message_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return app.execute(
        "SELECT kind, payload FROM ops.job WHERE payload->>'inbound_id' = %s ORDER BY id",
        (str(message_id),),
    ).fetchall()


def _fenced(fn: Callable[[], Any]) -> None:
    with pytest.raises(psycopg.Error) as e:
        fn()
    assert e.value.sqlstate == FENCED_SQLSTATE


# --- inbound (conv.record_inbound) -------------------------------------------------------------


def test_inbound_duplicate_is_noop(gateway: Conn, app: Conn) -> None:
    _, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    payload = Jsonb({"text": "A fictional test phrase"})
    since = _seq(app)
    first = gateway.execute(INBOUND, (conn_id, message_id, "utterance", payload)).fetchone()
    second = gateway.execute(INBOUND, (conn_id, message_id, "utterance", payload)).fetchone()
    assert (first, second) == ((True, None), (False, None))
    assert _jobs_for(app, message_id) == [("conv.turn", {"inbound_id": str(message_id)})]
    assert _one(app, "SELECT count(*) FROM conv.inbound WHERE id = %s", (message_id,)) == 1
    assert _audit(app, since) == []  # a duplicate is not a refusal


@pytest.mark.parametrize(
    ("kind", "job_kind"),
    [
        ("utterance", "conv.turn"),
        ("approval.decision", "approval.decision"),
        ("action.receipt", "action.receipt"),
    ],
)
def test_inbound_enqueues_the_fixed_job_kind(
    gateway: Conn, app: Conn, kind: str, job_kind: str
) -> None:
    _, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    row = gateway.execute(INBOUND, (conn_id, message_id, kind, Jsonb({}))).fetchone()
    assert row == (True, None)
    assert [j[0] for j in _jobs_for(app, message_id)] == [job_kind]


def test_inbound_person_comes_from_node_row(gateway: Conn, app: Conn) -> None:
    other = _person(app)
    node_id, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    payload = Jsonb({"text": "A fictional phrase", "person_id": str(other)})
    gateway.execute(INBOUND, (conn_id, message_id, "utterance", payload))
    row = app.execute(
        "SELECT node_id, person_id, kind, session_id, outcome FROM conv.inbound WHERE id = %s",
        (message_id,),
    ).fetchone()
    assert row == (node_id, MANJ, "utterance", None, None)


def test_inbound_unknown_kind_refused(gateway: Conn, app: Conn, dsns: Dsns) -> None:
    node_id, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    since = _seq(app)
    recorded, refusal = dbcall.audited_call(
        gateway,
        "SELECT recorded, refusal FROM conv.record_inbound(%s, %s, %s, %s)",
        (conn_id, message_id, "hello", Jsonb({"text": "A fictional phrase"})),
    )
    assert (recorded, refusal) == (False, "unknown_kind")
    with psycopg.connect(dsns.app, autocommit=True) as other:  # R7-1: a separate connection
        assert _one(other, "SELECT count(*) FROM conv.inbound WHERE id = %s", (message_id,)) == 0
        assert _jobs_for(other, message_id) == []
        assert _audit(other, since) == [
            (
                "inbound",
                message_id,
                "INBOUND",
                None,
                None,
                "REJECTED",
                "unknown_kind",
                {"node_id": str(node_id)},  # codes and IDs only: never the kind or payload
            )
        ]


def test_inbound_malformed_call_raises(gateway: Conn, app: Conn) -> None:
    _, conn_id = _connected(app, gateway)
    since = _seq(app)
    for message_id, kind, payload in (
        (None, "utterance", Jsonb({})),
        (uuid.uuid4(), None, Jsonb({})),
        (uuid.uuid4(), "utterance", None),
        (uuid.uuid4(), "utterance", Jsonb(["not", "an", "object"])),
    ):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            gateway.execute(INBOUND, (conn_id, message_id, kind, payload))
    assert _audit(app, since) == []


def test_inbound_notifies_jarvis_jobs(gateway: Conn, app: Conn, dsns: Dsns) -> None:
    _, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    with psycopg.connect(dsns.app, autocommit=True) as listener:
        listener.execute("LISTEN jarvis_jobs")
        gateway.execute(INBOUND, (conn_id, message_id, "utterance", Jsonb({})))
        got = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]
    job_id = _one(
        app, "SELECT id FROM ops.job WHERE payload->>'inbound_id' = %s", (str(message_id),)
    )
    assert got == [str(job_id)]


# --- events (comm.event) -----------------------------------------------------------------------


def test_event_is_append_only(app: Conn, owner: Conn) -> None:
    event_id = _event(app)
    for stmt in (
        "UPDATE comm.event SET kind = 'x' WHERE id = %s",
        "DELETE FROM comm.event WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(stmt, (event_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            owner.execute(stmt, (event_id,))  # the trigger refuses even the table owner
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        owner.execute("TRUNCATE comm.event CASCADE")
    assert _one(app, "SELECT kind FROM comm.event WHERE id = %s", (event_id,)) == "test.notice"


def test_event_details_hold_codes_and_ids_only(app: Conn) -> None:
    with pytest.raises(ValueError, match="codes and IDs"):
        comm.record_event(
            app,
            "test.notice",
            subject_type="test",
            subject_id="t-1",
            person_id=MANJ,
            details={"note": "free text with spaces"},
        )
    with pytest.raises(psycopg.errors.CheckViolation):  # the database enforces it too
        app.execute(
            "INSERT INTO comm.event (kind, subject_type, subject_id, details)"
            " VALUES ('test.notice', 'test', 't-1', %s)",
            (Jsonb({"note": "free text with spaces"}),),
        )


# --- outbox and delivery (§8.2) ----------------------------------------------------------------


def test_priority_and_state_enums() -> None:
    assert [p.value for p in Priority] == [p.value for p in OutboxPriority]
    assert [s.value for s in OutboxState] == [
        "PENDING",
        "DELIVERING",
        "DELIVERED",
        "ACKNOWLEDGED",
        "SUPERSEDED",
        "EXPIRED",
    ]


def test_new_item_starts_pending_and_notifies(app: Conn, dsns: Dsns) -> None:
    with psycopg.connect(dsns.app, autocommit=True) as listener:
        listener.execute("LISTEN jarvis_outbox")
        item = _item(app)
        got = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]
    assert got == [str(item)]
    assert _state(app, item) == "PENDING"


def test_outbox_states_only_move_forward(gateway: Conn, app: Conn, owner: Conn) -> None:
    _, conn_id = _connected(app, gateway)
    item = _item(app)
    assert _one(gateway, SENT, (conn_id, item)) is True
    assert _one(gateway, ACK, (conn_id, item)) == "ACKNOWLEDGED"
    assert _one(gateway, RECEIVED, (conn_id, item)) == "ACKNOWLEDGED"
    assert _state(app, item) == "ACKNOWLEDGED"
    # No role can move it back, not even the table owner: a trigger guards the state.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("UPDATE comm.outbox SET state = 'PENDING' WHERE id = %s", (item,))
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("UPDATE comm.outbox SET state = 'DELIVERED' WHERE id = %s", (item,))
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("UPDATE comm.outbox SET body = '{}' WHERE id = %s", (item,))


def test_received_moves_delivering_to_delivered(gateway: Conn, app: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    item = _item(app)
    since = _seq(app)
    assert _one(gateway, SENT, (conn_id, item)) is True
    assert _state(app, item) == "DELIVERING"
    assert _one(gateway, RECEIVED, (conn_id, item)) == "DELIVERED"
    row = app.execute(
        "SELECT attempts, received_at IS NOT NULL FROM comm.delivery"
        " WHERE outbox_id = %s AND node_id = %s",
        (item, node_id),
    ).fetchone()
    assert row == (1, True)
    n = {"node_id": str(node_id)}
    assert _audit(app, since) == [
        ("outbox", item, "SEND", "PENDING", "DELIVERING", "APPLIED", "sent", n),
        ("outbox", item, "RECEIVE", "DELIVERING", "DELIVERED", "APPLIED", "received", n),
    ]


def test_record_sent_refuses_terminal(gateway: Conn, app: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    item = _item(app)
    app.execute("SELECT comm.supersede(%s)", (item,))
    since = _seq(app)
    assert _one(gateway, SENT, (conn_id, item)) is False
    assert _state(app, item) == "SUPERSEDED"
    assert _one(app, "SELECT count(*) FROM comm.delivery WHERE outbox_id = %s", (item,)) == 0
    assert _audit(app, since) == [
        (
            "outbox",
            item,
            "SEND",
            "SUPERSEDED",
            None,
            "REJECTED",
            "terminal",
            {"node_id": str(node_id)},
        ),
    ]


def test_ack_on_one_node_ends_delivery(gateway: Conn, app: Conn, dsns: Dsns) -> None:
    _, conn_a = _connected(app, gateway)
    node_b, conn_b = _connected(app, gateway)
    item = _item(app)
    assert _one(gateway, SENT, (conn_a, item)) is True
    with psycopg.connect(dsns.app, autocommit=True) as listener:
        listener.execute("LISTEN jarvis_outbox")
        assert _one(gateway, ACK, (conn_a, item)) == "ACKNOWLEDGED"
        got = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]
    assert got == [str(item)]
    assert _one(gateway, SENT, (conn_b, item)) is False  # node B was offline: never gets it live
    row = app.execute(
        "SELECT count(*) FROM comm.delivery WHERE outbox_id = %s AND node_id = %s", (item, node_b)
    ).fetchone()
    assert row == (0,)


def test_retry_keeps_id_and_counts_attempts(gateway: Conn, app: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    item = _item(app)
    assert _one(gateway, SENT, (conn_id, item)) is True
    assert _one(gateway, SENT, (conn_id, item)) is True
    rows = app.execute(
        "SELECT node_id, attempts, first_sent_at <= last_sent_at, received_at FROM comm.delivery"
        " WHERE outbox_id = %s",
        (item,),
    ).fetchall()
    assert rows == [(node_id, 2, True, None)]
    assert _state(app, item) == "DELIVERING"


def test_superseded_and_expired_remain_readable(app: Conn, dsns: Dsns) -> None:
    old = _item(app)
    with psycopg.connect(dsns.app, autocommit=True) as listener:
        listener.execute("LISTEN jarvis_outbox")
        new = _item(app, supersedes_id=old)
        got = {n.payload for n in listener.notifies(timeout=5, stop_after=2)}
    assert got == {str(old), str(new)}
    gone = _item(app)
    app.execute("SELECT comm.expire(%s)", (gone,))
    rows = app.execute(
        "SELECT id, state, supersedes_id, body FROM comm.outbox WHERE id = ANY(%s) ORDER BY state",
        ([old, new, gone],),
    ).fetchall()
    assert {(r[0], r[1], r[2]) for r in rows} == {
        (old, "SUPERSEDED", None),
        (new, "PENDING", old),
        (gone, "EXPIRED", None),
    }
    assert all(r[3] == {"text": "A fictional notice"} for r in rows)


def test_supersede_and_expire_are_audited_and_final(app: Conn) -> None:
    item = _item(app)
    since = _seq(app)
    app.execute("SELECT comm.expire(%s)", (item,))
    app.execute("SELECT comm.supersede(%s)", (item,))
    assert _state(app, item) == "EXPIRED"
    assert _audit(app, since) == [
        ("outbox", item, "EXPIRE", "PENDING", "EXPIRED", "APPLIED", "expired", {}),
        ("outbox", item, "SUPERSEDE", "EXPIRED", None, "REJECTED", "terminal", {}),
    ]
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app.execute("SELECT comm.supersede(%s)", (uuid.uuid4(),))


def test_create_outbox_refuses_superseding_another_persons_item(app: Conn) -> None:
    theirs = _item(app, _person(app))
    with pytest.raises(ValueError, match="same person"):
        _item(app, supersedes_id=theirs)
    assert _state(app, theirs) == "PENDING"


def test_another_persons_item_is_refused(gateway: Conn, app: Conn, dsns: Dsns) -> None:
    node_id, conn_id = _connected(app, gateway)
    theirs = _item(app, _person(app))
    unknown = uuid.uuid4()
    since = _seq(app)
    for item in (theirs, unknown):
        assert dbcall.audited_call(gateway, "SELECT comm.record_sent(%s, %s)", (conn_id, item)) == (
            False,
        )
        for sql in ("SELECT comm.record_received(%s, %s)", "SELECT comm.acknowledge(%s, %s)"):
            assert dbcall.audited_call(gateway, sql, (conn_id, item)) == ("unknown_item",)
    assert _state(app, theirs) == "PENDING"
    with psycopg.connect(dsns.app, autocommit=True) as other:
        assert _one(other, "SELECT count(*) FROM comm.delivery WHERE node_id = %s", (node_id,)) == 0
        n = {"node_id": str(node_id)}
        assert _audit(other, since) == [
            ("outbox", item, action, None, None, "REJECTED", "unknown_item", n)
            for item in (theirs, unknown)
            for action in ("SEND", "RECEIVE", "ACK")
        ]


# --- the multi-node race (P2-E13, database half) ----------------------------------------------


def test_delivery_race_is_consistent(gateway: Conn, app: Conn, dsns: Dsns) -> None:
    runs = 1000
    rnd = random.Random(20261122)  # noqa: S311  # a reproducible test load, not crypto
    node_a, conn_a = _connected(app, gateway)
    node_b, conn_b = _connected(app, gateway)
    items = [_item(app) for _ in range(runs)]
    gw_ops = ["sent", "received", "ack"]
    plans: dict[str, list[list[str]]] = {
        "A": [rnd.choices(gw_ops, [5, 3, 2], k=rnd.randint(1, 4)) for _ in range(runs)],
        "B": [rnd.choices(gw_ops, [5, 3, 2], k=rnd.randint(1, 4)) for _ in range(runs)],
        "core": [["supersede"] if rnd.random() < 0.3 else [] for _ in range(runs)],
    }
    results: dict[str, list[list[tuple[str, Any]]]] = {k: [[] for _ in range(runs)] for k in plans}
    barrier = threading.Barrier(3, timeout=30)
    sql = {"sent": SENT, "received": RECEIVED, "ack": ACK}

    def worker(actor: str, dsn: str, conn_id: int | None) -> None:
        jitter = random.Random(actor)  # noqa: S311
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                for run in range(runs):
                    barrier.wait()
                    for op in plans[actor][run]:
                        if jitter.random() < 0.3:
                            time.sleep(jitter.random() * 0.0005)
                        if op == "supersede":
                            c.execute("SELECT comm.supersede(%s)", (items[run],))
                            results[actor][run].append((op, None))
                        else:
                            results[actor][run].append(
                                (op, _one(c, sql[op], (conn_id, items[run])))
                            )
                    barrier.wait()
        except BaseException:
            barrier.abort()
            raise

    run_all(
        [
            lambda: worker("A", dsns.gateway, conn_a),
            lambda: worker("B", dsns.gateway, conn_b),
            lambda: worker("core", dsns.app, None),
        ],
        timeout=300,
    )

    audit_rows = app.execute(
        "SELECT entity_id, action::text, from_state, to_state, outcome::text, reason,"
        " details->>'node_id' FROM audit.event WHERE entity_type = 'outbox'"
        " AND entity_id = ANY(%s) ORDER BY seq",
        (items,),
    ).fetchall()
    by_item: dict[uuid.UUID, list[tuple[Any, ...]]] = {i: [] for i in items}
    for r in audit_rows:
        by_item[r[0]].append(r[1:])
    finals = dict(
        app.execute("SELECT id, state FROM comm.outbox WHERE id = ANY(%s)", (items,)).fetchall()
    )
    attempts = {
        (r[0], r[1]): r[2]
        for r in app.execute(
            "SELECT outbox_id, node_id, attempts FROM comm.delivery WHERE outbox_id = ANY(%s)",
            (items,),
        ).fetchall()
    }
    node_of = {"A": str(node_a), "B": str(node_b)}
    finals_seen: Counter[str] = Counter()
    for run, item in enumerate(items):
        ops = {actor: results[actor][run] for actor in results}
        rows = by_item[item]
        # One audit row per call, in a single consistent chain from PENDING.
        assert len(rows) == sum(len(v) for v in ops.values()), run
        state = "PENDING"
        for action, from_state, to_state, outcome, reason, _node in rows:
            assert from_state == state, (run, rows)
            if outcome == "REJECTED":
                assert to_state is None and reason == "terminal" and state in TERMINAL, run
                assert action in ("SEND", "SUPERSEDE"), run
                continue
            if state in TERMINAL:
                assert to_state == state and reason == "late", (run, rows)
                assert action in ("RECEIVE", "ACK"), run
            else:
                assert to_state in TERMINAL or RANK[to_state] >= RANK[state], (run, rows)
            state = to_state
        final = finals[item]
        assert final == state, run
        finals_seen[final] += 1
        # The final state is the one the calls made legal: the first terminal call wins.
        done = {op for actor in ops for op, _ in ops[actor]}
        terminal_calls = {"ACKNOWLEDGED"} if "ack" in done else set()
        if "supersede" in done:
            terminal_calls.add("SUPERSEDED")
        if terminal_calls:
            assert final in terminal_calls, run
        elif "received" in done:
            assert final == "DELIVERED", run
        else:
            assert final == "DELIVERING", run
        # Each node's results match its audit rows and its delivery row.
        for actor in ("A", "B"):
            sends = [r for op, r in ops[actor] if op == "sent"]
            mine = [r for r in rows if r[5] == node_of[actor] and r[0] == "SEND"]
            assert [r[3] == "APPLIED" for r in mine] == sends, run
            assert attempts.get((item, uuid.UUID(node_of[actor])), 0) == sum(sends), run
            for op, result in ops[actor]:
                if op != "sent":
                    assert result in RANK or result in TERMINAL, run
    # The seeded plan reaches every final state the calls allow.
    assert set(finals_seen) == {"DELIVERING", "DELIVERED", "ACKNOWLEDGED", "SUPERSEDED"}


# --- strict fencing (R8-1), both orders --------------------------------------------------------


def test_revoke_wins_inbound_refused(dsns: Dsns, app: Conn, gateway: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    with psycopg.connect(dsns.app) as holder:  # revoke_node, transaction held open
        holder.execute("SELECT * FROM gw.revoke_node(%s)", (node_id,))
        write = InThread(
            dsns.gateway,
            lambda c: c.execute(INBOUND, (conn_id, message_id, "utterance", Jsonb({}))).fetchone(),
        )
        assert write.wait_until_blocked(dsns.superuser) in ROW_LOCK_WAITS
        holder.commit()
    _fenced(write.join)
    assert _one(app, "SELECT count(*) FROM conv.inbound WHERE id = %s", (message_id,)) == 0
    assert _jobs_for(app, message_id) == []


def test_inbound_wins_revoke_waits(dsns: Dsns, app: Conn, gateway: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    with psycopg.connect(dsns.gateway) as holder:  # record_inbound, transaction held open
        row = holder.execute(INBOUND, (conn_id, message_id, "utterance", Jsonb({}))).fetchone()
        assert row == (True, None)
        revoke = InThread(dsns.app, lambda c: nodes.revoke(c, node_id))
        assert revoke.wait_until_blocked(dsns.superuser) in ROW_LOCK_WAITS
        assert _one(app, "SELECT revoked_at FROM gw.node WHERE id = %s", (node_id,)) is None
        holder.commit()
    revoke.join()
    received_at = _one(app, "SELECT received_at FROM conv.inbound WHERE id = %s", (message_id,))
    revoked_at = _one(app, "SELECT revoked_at FROM gw.node WHERE id = %s", (node_id,))
    assert received_at < revoked_at  # recorded first; the revocation completed afterwards
    assert [j[0] for j in _jobs_for(app, message_id)] == ["conv.turn"]


def test_supersede_wins_ack_refused(dsns: Dsns, app: Conn, gateway: Conn) -> None:
    node_id, old = _connected(app, gateway)
    item = _item(app)
    assert _one(gateway, SENT, (old, item)) is True
    since = _seq(app)
    with psycopg.connect(dsns.gateway) as holder:  # open_connection(new) supersedes, held open
        holder.execute("SELECT conn_id FROM gw.open_connection(%s)", (node_id,))
        ack = InThread(dsns.gateway, lambda c: _one(c, ACK, (old, item)))
        assert ack.wait_until_blocked(dsns.superuser) in ROW_LOCK_WAITS
        holder.commit()
    _fenced(ack.join)
    assert _state(app, item) == "DELIVERING"
    assert _audit(app, since) == []
    received = _one(
        app,
        "SELECT received_at FROM comm.delivery WHERE outbox_id = %s AND node_id = %s",
        (item, node_id),
    )
    assert received is None


def test_ack_wins_supersede_waits(dsns: Dsns, app: Conn, gateway: Conn) -> None:
    node_id, old = _connected(app, gateway)
    item = _item(app)
    assert _one(gateway, SENT, (old, item)) is True
    with psycopg.connect(dsns.gateway) as holder:  # acknowledge(old), transaction held open
        assert _one(holder, ACK, (old, item)) == "ACKNOWLEDGED"
        opener = InThread(
            dsns.gateway,
            lambda c: c.execute(
                "SELECT conn_id, refusal FROM gw.open_connection(%s)", (node_id,)
            ).fetchone(),
        )
        assert opener.wait_until_blocked(dsns.superuser) in ROW_LOCK_WAITS
        closed = _one(app, "SELECT closed_at FROM gw.node_connection WHERE id = %s", (old,))
        assert closed is None  # the old connection stays open while the write holds its fence
        holder.commit()
    new_conn, refusal = opener.join()
    assert refusal is None and new_conn != old
    assert _state(app, item) == "ACKNOWLEDGED"
    row = app.execute(
        "SELECT close_reason FROM gw.node_connection WHERE id = %s", (old,)
    ).fetchone()
    assert row == ("superseded",)


_LOCKING = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)


def _body_statements(src: str) -> tuple[str, list[str]]:
    """Split a PL/pgSQL body into (declare section, statements after the first BEGIN)."""
    code = re.sub(r"--[^\n]*", "", src)
    parts = re.split(r"\bBEGIN\b", code, maxsplit=1, flags=re.IGNORECASE)
    assert len(parts) == 2
    head, body = parts
    return head, [s.strip() for s in body.split(";") if s.strip()]


def test_every_conn_function_calls_current_node_first(owner: Conn) -> None:
    rows = owner.execute(
        "SELECT p.oid::regprocedure::text, p.prosrc FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname IN ('conv', 'comm') AND 'p_conn_id' = ANY(p.proargnames)"
    ).fetchall()
    assert {r[0].replace(" ", "") for r in rows} == CONN_FUNCTIONS
    for signature, src in rows:
        declare, statements = _body_statements(src)
        assert ":=" not in declare and "DEFAULT" not in declare.upper(), signature
        assert re.fullmatch(r"v_node\s*:=\s*gw\.current_node\(p_conn_id\)", statements[0]), (
            signature
        )
        assert src.count("current_node") == 1, signature
        assert "touch_connection" not in src, signature  # no lock upgrade (owner requirement 1)
        for stmt in statements:
            if re.search(r"\bgw\.node(_connection)?\b", stmt):
                assert not _LOCKING.search(stmt), (signature, stmt)  # no other gw lock


def test_fenced_connection_refused_everywhere(gateway: Conn, app: Conn) -> None:
    node_id = _node(app, gateway)
    conn1 = _open(gateway, node_id)
    conn2 = _open(gateway, node_id)  # supersedes conn1
    closed = _one(app, "SELECT close_reason FROM gw.node_connection WHERE id = %s", (conn1,))
    assert closed == "superseded"
    item = _item(app)
    assert _one(gateway, SENT, (conn2, item)) is True
    reply = uuid.uuid4()
    app.execute(
        "INSERT INTO conv.stream_chunk (reply_id, seq, type, text, final)"
        " VALUES (%s, 1, 'SPEECH', 'A fictional reply', true)",
        (reply,),
    )
    message_id = uuid.uuid4()
    since = _seq(app)
    for conn_id in (conn1, 10**12):  # superseded, and unknown
        _fenced(lambda c=conn_id: gateway.execute(INBOUND, (c, message_id, "utterance", Jsonb({}))))
        _fenced(lambda c=conn_id: gateway.execute(SENT, (c, item)))
        _fenced(lambda c=conn_id: gateway.execute(RECEIVED, (c, item)))
        _fenced(lambda c=conn_id: gateway.execute(ACK, (c, item)))
        _fenced(lambda c=conn_id: gateway.execute(CHUNK, (c, reply, 1)))
    assert _one(app, "SELECT count(*) FROM conv.inbound WHERE id = %s", (message_id,)) == 0
    assert _state(app, item) == "DELIVERING"
    assert _one(app, "SELECT attempts FROM comm.delivery WHERE outbox_id = %s", (item,)) == 1
    assert _one(app, "SELECT count(*) FROM conv.stream_cursor WHERE reply_id = %s", (reply,)) == 0
    assert _audit(app, since) == []


# --- the stream cursor -------------------------------------------------------------------------


def test_chunk_cursor_only_moves_forward(gateway: Conn, app: Conn) -> None:
    node_id, conn_id = _connected(app, gateway)
    reply = uuid.uuid4()
    for seq in range(1, 6):
        app.execute(
            "INSERT INTO conv.stream_chunk (reply_id, seq, type, text, final)"
            " VALUES (%s, %s, 'SPEECH', 'A fictional reply', %s)",
            (reply, seq, seq == 5),
        )
    gateway.execute(CHUNK, (conn_id, reply, 5))
    gateway.execute(CHUNK, (conn_id, reply, 3))
    rows = gateway.execute(
        "SELECT node_id, acked_seq FROM conv.stream_cursor WHERE reply_id = %s", (reply,)
    ).fetchall()
    assert rows == [(node_id, 5)]
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        gateway.execute(CHUNK, (conn_id, reply, 6))  # no such chunk
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        gateway.execute(CHUNK, (conn_id, reply, 0))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("UPDATE conv.stream_cursor SET acked_seq = 0")


def test_stream_chunk_shape_is_checked(app: Conn) -> None:
    reply = uuid.uuid4()
    for stmt in (
        "INSERT INTO conv.stream_chunk (reply_id, seq, type, final)"
        " VALUES (%s, 1, 'SPEECH', false)",
        "INSERT INTO conv.stream_chunk (reply_id, seq, type, text, authz, final)"
        " VALUES (%s, 1, 'SPEECH', 'x', '{}', false)",
        "INSERT INTO conv.stream_chunk (reply_id, seq, type, text, final)"
        " VALUES (%s, 1, 'ACTION', 'x', false)",
        "INSERT INTO conv.stream_chunk (reply_id, seq, type, text, final)"
        " VALUES (%s, 0, 'SPEECH', 'x', false)",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(stmt, (reply,))


def test_one_open_session_per_person(app: Conn) -> None:
    person = _person(app)
    app.execute("INSERT INTO conv.session (person_id) VALUES (%s)", (person,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app.execute("INSERT INTO conv.session (person_id) VALUES (%s)", (person,))
    app.execute("UPDATE conv.session SET closed_at = now() WHERE person_id = %s", (person,))
    app.execute("INSERT INTO conv.session (person_id) VALUES (%s)", (person,))


def test_inbound_outcome_values(app: Conn, gateway: Conn) -> None:
    _, conn_id = _connected(app, gateway)
    message_id = uuid.uuid4()
    gateway.execute(INBOUND, (conn_id, message_id, "utterance", Jsonb({})))
    for outcome in ("ANSWERED", "INDETERMINATE", "REFUSED"):
        app.execute("UPDATE conv.inbound SET outcome = %s WHERE id = %s", (outcome, message_id))
    with pytest.raises(psycopg.errors.CheckViolation):
        app.execute("UPDATE conv.inbound SET outcome = 'DONE' WHERE id = %s", (message_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("UPDATE conv.inbound SET person_id = %s WHERE id = %s", (MANJ, message_id))


# --- the gateway's privileges (P2-S1) ----------------------------------------------------------


def test_gateway_cannot_write_conv_message_or_ledger(gateway: Conn) -> None:
    for stmt in (
        "INSERT INTO conv.message (session_id, seq, role, person_id, text)"
        " VALUES (gen_random_uuid(), 1, 'PERSON', gen_random_uuid(), 'x')",
        "INSERT INTO ledger.task (title, definition_of_done, source_ref) VALUES ('x', 'x', 'x')",
        "INSERT INTO conv.inbound (id, node_id, person_id, kind, payload)"
        " VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'utterance', '{}')",
        "UPDATE comm.outbox SET state = 'ACKNOWLEDGED'",
        "INSERT INTO comm.delivery (outbox_id, node_id)"
        " VALUES (gen_random_uuid(), gen_random_uuid())",
        "INSERT INTO conv.stream_cursor (reply_id, node_id)"
        " VALUES (gen_random_uuid(), gen_random_uuid())",
        "INSERT INTO comm.event (kind, subject_type, subject_id) VALUES ('x', 'x', 'x')",
        "SELECT * FROM conv.message",
        "SELECT * FROM conv.session",
        "SELECT * FROM conv.inbound",
        "SELECT * FROM comm.event",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(stmt)  # static statements above


def test_gateway_cannot_call_ledger_transition(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute(
            "SELECT ledger.transition('task', gen_random_uuid(), 'READY', 'AGENT', 'gw', 'x')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT comm.supersede(gen_random_uuid())")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT comm.expire(gen_random_uuid())")


def test_gateway_cannot_write_audit(gateway: Conn, owner: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute(
            "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id, action,"
            " outcome, reason) VALUES ('AGENT', 'gateway', 'node', gen_random_uuid(), 'CREATE',"
            " 'APPLIED', 'x')"
        )
    functions = owner.execute(
        "SELECT p.oid::regproc::text, p.pronargs FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'audit' AND p.prorettype <> 'trigger'::regtype"
    ).fetchall()
    assert {"audit.record_memory", "audit.append"} <= {f for f, _ in functions}
    for name, nargs in functions:
        call = f"SELECT {name}({', '.join(['NULL'] * nargs)})"  # names from pg_proc
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(call.encode())


GATEWAY_TABLES = {
    "SELECT conv.stream_chunk",
    "SELECT conv.stream_cursor",
    "SELECT comm.outbox",
    "SELECT comm.delivery",
}
GATEWAY_NODE_COLUMNS = {
    f"SELECT gw.node.{c}"
    for c in ("id", "person_id", "kind", "public_key", "tailscale_identity", "revoked_at")
}
GATEWAY_EXECUTE = {
    "gw.enrol_node(text,bytea,text)",
    "gw.open_connection(uuid)",
    "gw.close_connection(bigint,text)",
    "gw.touch_connection(bigint)",
    "gw.current_node(bigint)",
} | CONN_FUNCTIONS


def test_gateway_has_no_memory_function_or_table(gateway: Conn, owner: Conn) -> None:
    """The gateway's whole privilege set, from the catalogue: only §4.1's objects."""
    granted: set[str] = set()
    relations = owner.execute(
        "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
        " AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
    ).fetchall()
    assert ("conv.message",) in relations
    for (name,) in relations:
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            if _one(owner, "SELECT has_table_privilege(%s, %s, %s)", (GATEWAY_ROLE, name, p)):
                granted.add(f"{p} {name}")
            elif p in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                columns = owner.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
                    " AND attnum > 0 AND NOT attisdropped"
                    " AND has_column_privilege(%s, attrelid, attnum, %s)",
                    (name, GATEWAY_ROLE, p),
                ).fetchall()
                granted |= {f"{p} {name}.{c}" for (c,) in columns}
    assert granted == GATEWAY_TABLES | GATEWAY_NODE_COLUMNS
    functions = owner.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        " AND p.prorettype <> 'trigger'::regtype"
        " AND has_function_privilege(%s, p.oid, 'EXECUTE')",
        (GATEWAY_ROLE,),
    ).fetchall()
    assert {r[0].replace(" ", "") for r in functions} == GATEWAY_EXECUTE
    schemas = owner.execute(
        "SELECT nspname FROM pg_namespace WHERE has_schema_privilege(%s, oid, 'USAGE')"
        " AND nspname NOT IN ('pg_catalog', 'information_schema', 'public')"
        " AND nspname NOT LIKE 'pg\\_%%'",
        (GATEWAY_ROLE,),
    ).fetchall()
    assert {r[0] for r in schemas} == {"gw", "conv", "comm"}
    # Every gateway function is SECURITY DEFINER, owned by jarvis_owner, and only the gateway
    # may execute it.
    for signature in CONN_FUNCTIONS:
        row = owner.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner),"
            " (SELECT array_agg(pg_get_userbyid(a.grantee)::text ORDER BY 1)"
            "  FROM aclexplode(p.proacl) a"
            "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature,),
        ).fetchone()
        assert row == (True, "jarvis_owner", [GATEWAY_ROLE]), signature
    for signature in ("comm.supersede(uuid)", "comm.expire(uuid)"):
        row = owner.execute(
            "SELECT p.prosecdef, (SELECT array_agg(pg_get_userbyid(a.grantee)::text)"
            "  FROM aclexplode(p.proacl) a"
            "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature,),
        ).fetchone()
        assert row == (True, ["jarvis_app"]), signature


# --- migration 0009 rollback -------------------------------------------------------------------


def _fingerprint(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema IN ('conv', 'comm') ORDER BY 1, 2, 3"
    ).fetchall()


def _gateway_acl_entries(conn: Conn) -> int:
    return int(
        _one(
            conn,
            "SELECT (SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a"
            "        WHERE a.grantee = %(r)s::regrole)"
            "     + (SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a"
            "        WHERE a.grantee = %(r)s::regrole)"
            "     + (SELECT count(*) FROM pg_namespace s, aclexplode(s.nspacl) a"
            "        WHERE a.grantee = %(r)s::regrole)",
            {"r": GATEWAY_ROLE},
        )
    )


def test_0009_down_and_up_without_rows() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.owner, autocommit=True) as conn:
            before = _fingerprint(conn)
            grants_before = _gateway_acl_entries(conn)
        assert {r[0] for r in before} == {"conv", "comm"}
        migrate.downgrade(d.owner, "0008")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert _fingerprint(conn) == []
            assert conn.execute(
                "SELECT to_regnamespace('conv'), to_regnamespace('comm')"
            ).fetchone() == (None, None)
            labels = conn.execute("SELECT unnest(enum_range(NULL::audit.action))::text").fetchall()
            assert [r[0] for r in labels] == ACTIONS_0008
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id,"
                    " action, outcome, reason) VALUES ('SYSTEM', 'system', 'outbox',"
                    " gen_random_uuid(), 'CREATE', 'APPLIED', 'probe')"
                )
            assert _gateway_acl_entries(conn) < grants_before
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert _fingerprint(conn) == before
            assert _gateway_acl_entries(conn) == grants_before


@pytest.mark.parametrize("what", ["event", "refusal"])
def test_0009_downgrade_refuses_once_rows_exist(what: str) -> None:
    with (
        fresh_database() as d,
        psycopg.connect(d.app, autocommit=True) as app,
        psycopg.connect(d.gateway, autocommit=True) as gateway,
    ):
        if what == "event":
            _event(app)
        else:  # only an audit row of the new entity types, no conv or comm row
            _, conn_id = _connected(app, gateway)
            gateway.execute(INBOUND, (conn_id, uuid.uuid4(), "hello", Jsonb({})))
            assert _one(app, "SELECT count(*) FROM conv.inbound", ()) == 0
        with pytest.raises(Exception, match="0009 cannot be undone"):
            migrate.downgrade(d.owner, "0008")
        with psycopg.connect(d.owner, autocommit=True) as owner:
            version = _one(owner, "SELECT version_num FROM alembic_version", ())
        assert version == "0009"
        assert _one(app, "SELECT to_regclass('comm.event')::text", ()) == "comm.event"


# --- the shared thread helper (owner requirement 4) ---------------------------------------------


def test_thread_helper_propagates_errors(dsns: Dsns) -> None:
    bad_dsn = psycopg.conninfo.make_conninfo(dsns.gateway, password="wrong")
    with pytest.raises(psycopg.OperationalError):  # a connection error, raised in the test
        InThread(bad_dsn, lambda c: None)
    failing = InThread(dsns.gateway, lambda c: c.execute("SELECT * FROM conv.message"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # not a bare timeout
        failing.wait_until_blocked(dsns.superuser)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        failing.join()

    def boom() -> None:
        raise RuntimeError("worker failed")

    with pytest.raises(RuntimeError, match="worker failed"):
        run_all([boom, lambda: None], timeout=10)
