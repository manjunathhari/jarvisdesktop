"""Gateway role and node registry (migration 0008; spec §4.1, §5.1, §7.2, R7-1 … R7-6).

Runs in its own database: node and enrolment rows block the 0008 downgrade (R7-6), so they
must never reach the session database that P0-6 takes down and up. Keys, tokens and Tailscale
identities are random test values; the only person is the seeded fictional household member.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import conninfo_to_dict

from jarvis_server import config, core_main, dbcall, migrate, nodes
from jarvis_server.bootstrap import GATEWAY_ROLE
from jarvis_server.dbcall import (
    FENCED_SQLSTATE,
    EnrolmentRefused,
    RevokeRefused,
    TransactionNotIdle,
)
from jarvis_server.memory import format as memory_format
from jarvis_server.signing import fingerprint, public_bytes

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
CFP = fingerprint(bytes(range(32)))
GFP = fingerprint(bytes(range(32, 64)))

ACTIONS_0007 = ["CREATE", "TRANSITION", "UPDATE", "CONFIRM", "ARCHIVE"]

GATEWAY_FUNCTIONS = {
    "gw.enrol_node(text,bytea,text)",
    "gw.open_connection(uuid)",
    "gw.close_connection(bigint,text)",
    "gw.touch_connection(bigint)",
    "gw.current_node(bigint)",
}


@pytest.fixture(scope="module")
def ndb() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def app(ndb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(ndb.app, autocommit=True) as conn:
        yield conn


@pytest.fixture
def owner(ndb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(ndb.owner, autocommit=True) as conn:
        yield conn


@pytest.fixture
def gateway(ndb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(ndb.gateway, autocommit=True) as conn:
        yield conn


def _key() -> bytes:
    return secrets.token_bytes(32)


def _ts() -> str:
    return f"ts:pc-{secrets.token_hex(4)}"


def _token(app: Conn, kind: str = "DESKTOP") -> str:
    code = nodes.create_enrolment(app, person_id=MANJ, kind=kind, name="pc", core_fp=CFP, gw_fp=GFP)
    token, core_fp, gw_fp = nodes.parse_enrolment_code(code)
    assert (core_fp, gw_fp) == (CFP, GFP)
    return token


def _enrolment(app: Conn, token: str) -> tuple[Any, ...]:
    row = app.execute(
        "SELECT id, used_at, node_id FROM gw.enrolment WHERE token_hash = %s",
        (hashlib.sha256(token.encode()).digest(),),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _node(app: Conn, gateway: Conn) -> uuid.UUID:
    return nodes.enrol(gateway, token=_token(app), public_key=_key(), tailscale_identity=_ts())


def _open(gateway: Conn, node_id: uuid.UUID) -> int:
    row = gateway.execute(
        "SELECT conn_id, refusal FROM gw.open_connection(%s)", (node_id,)
    ).fetchone()
    assert row is not None and row[1] is None
    return int(row[0])


def _seq(conn: Conn) -> int:
    row = conn.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


def _audit_seen_elsewhere(dsn: str, since: int) -> list[tuple[Any, ...]]:
    """Audit rows after `since`, read on a separate, new connection (R7-1)."""
    with psycopg.connect(dsn, autocommit=True) as other:
        return other.execute(
            "SELECT action::text, outcome::text, reason, entity_id, details FROM audit.event"
            " WHERE entity_type = 'node' AND seq > %s ORDER BY seq",
            (since,),
        ).fetchall()


def _node_audit_actions(app: Conn, since: int) -> list[tuple[Any, ...]]:
    return app.execute(
        "SELECT action::text FROM audit.event WHERE entity_type = 'node' AND seq > %s ORDER BY seq",
        (since,),
    ).fetchall()


def _node_row(app: Conn, node_id: uuid.UUID) -> dict[str, Any]:
    with app.cursor(row_factory=psycopg.rows.dict_row) as cur:
        row = cur.execute("SELECT * FROM gw.node WHERE id = %s", (node_id,)).fetchone()
    assert row is not None
    return row


# --- enrolment ---------------------------------------------------------------------------------


def test_enrol_node_once(app: Conn, gateway: Conn) -> None:
    code = nodes.create_enrolment(
        app, person_id=MANJ, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP
    )
    token, _, _ = nodes.parse_enrolment_code(code)
    node_id = nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity="ts:pc")
    row = app.execute("SELECT person_id, kind FROM gw.node WHERE id=%s", (node_id,)).fetchone()
    assert row == (MANJ, "DESKTOP")
    _, used_at, linked = _enrolment(app, token)
    assert used_at is not None and linked == node_id


def test_token_reuse_refused_and_audited(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    token = _token(app)
    first = nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    since = _seq(app)
    with pytest.raises(EnrolmentRefused) as e:
        nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    assert e.value.reason == "bad_token"
    enrolment_id = _enrolment(app, token)[0]
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2]) for r in rows] == [("ENROL", "REJECTED", "bad_token")]
    assert rows[0][4] == {"enrolment_id": str(enrolment_id)}
    assert app.execute("SELECT count(*) FROM gw.node WHERE id = %s", (first,)).fetchone() == (1,)


def test_expired_token_refused(app: Conn, gateway: Conn, owner: Conn, ndb: Dsns) -> None:
    token = _token(app)
    owner.execute(
        "UPDATE gw.enrolment SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (_enrolment(app, token)[0],),
    )
    since = _seq(app)
    with pytest.raises(EnrolmentRefused, match="bad_token"):
        nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2]) for r in rows] == [("ENROL", "REJECTED", "bad_token")]
    assert _enrolment(app, token)[1] is None


def test_enrol_node_returns_structured_refusal(app: Conn, gateway: Conn) -> None:
    token = _token(app)
    nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    row = gateway.execute(
        "SELECT node_id, refusal FROM gw.enrol_node(%s, %s, %s)", (token, _key(), _ts())
    ).fetchone()
    assert row == (None, "bad_token")
    unknown = gateway.execute(
        "SELECT node_id, refusal FROM gw.enrol_node(%s, %s, %s)", ("x" * 26, _key(), _ts())
    ).fetchone()
    assert unknown == (None, "bad_token")


def test_enrol_node_malformed_call_raises(app: Conn, gateway: Conn) -> None:
    # A programming error, not a refused request: invalid_parameter_value, no audit (R7-1).
    since = _seq(app)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        gateway.execute("SELECT * FROM gw.enrol_node(%s, %s, %s)", (_token(app), b"short", _ts()))
    assert [r[0] for r in _node_audit_actions(app, since)] == ["ENROL_CREATE"]


def test_audited_call_refuses_open_transaction(app: Conn, gateway: Conn) -> None:
    token = _token(app)
    since = _seq(app)
    with pytest.raises(TransactionNotIdle), gateway.transaction():
        nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    assert _enrolment(app, token)[1] is None
    assert _node_audit_actions(app, since) == []


def test_refusal_audit_survives_caller_exception(app: Conn, ndb: Dsns) -> None:
    token = _token(app)
    since = _seq(app)
    with psycopg.connect(ndb.gateway) as gw_conn:  # not autocommit: the caller can roll back
        nodes.enrol(gw_conn, token=token, public_key=_key(), tailscale_identity=_ts())
        try:
            nodes.enrol(gw_conn, token=token, public_key=_key(), tailscale_identity=_ts())
        except EnrolmentRefused:
            gw_conn.rollback()
        else:
            pytest.fail("token reuse was not refused")
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1]) for r in rows] == [("ENROL", "APPLIED"), ("ENROL", "REJECTED")]


def test_audited_call_async_follows_the_same_rule(app: Conn, ndb: Dsns) -> None:
    token = _token(app)

    async def run() -> tuple[Any, ...]:
        async with await psycopg.AsyncConnection.connect(ndb.gateway, autocommit=True) as conn:
            async with conn.transaction():
                with pytest.raises(TransactionNotIdle):
                    await dbcall.audited_call_async(conn, "SELECT 1", ())
            return await dbcall.audited_call_async(
                conn,
                "SELECT node_id, refusal FROM gw.enrol_node(%s, %s, %s)",
                (token, _key(), _ts()),
            )

    node_id, refusal = asyncio.run(run())
    assert refusal is None and _enrolment(app, token)[2] == node_id


def test_create_enrolment_is_audited(app: Conn, owner: Conn, ndb: Dsns) -> None:
    since = _seq(app)
    token = _token(app, kind="PHONE")
    enrolment_id = _enrolment(app, token)[0]
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("ENROL_CREATE", "APPLIED", "created", enrolment_id)
    ]
    assert rows[0][4] == {"person_id": str(MANJ), "kind": "PHONE"}
    row = owner.execute(
        "SELECT person_id, kind, name, used_at, node_id,"
        " expires_at - now() BETWEEN interval '14 minutes 50 seconds' AND interval '15 minutes'"
        " FROM gw.enrolment WHERE id = %s",
        (enrolment_id,),
    ).fetchone()
    assert row == (MANJ, "PHONE", "pc", None, None, True)


def test_create_enrolment_checks_its_arguments(app: Conn) -> None:
    for args in (
        (b"\x00" * 31, MANJ, "DESKTOP", "pc"),
        (b"\x00" * 32, MANJ, "LAPTOP", "pc"),
        (b"\x00" * 32, MANJ, "DESKTOP", ""),
        (b"\x00" * 32, uuid.uuid4(), "DESKTOP", "pc"),
    ):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            app.execute("SELECT gw.create_enrolment(%s, %s, %s, %s)", args)


def test_app_cannot_insert_enrolment_directly(app: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "INSERT INTO gw.enrolment (token_hash, person_id, kind, name, expires_at)"
            " VALUES (%s, %s, 'DESKTOP', 'pc', now() + interval '1 day')",
            (b"\x00" * 32, MANJ),
        )
    for stmt in (
        "UPDATE gw.enrolment SET expires_at = now() + interval '1 day'",
        "DELETE FROM gw.enrolment",
        "INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)"
        " VALUES ('00000000-0000-4000-8000-000000000002', 'DESKTOP', 'x', '\\x00', 'ts:x')",
        "UPDATE gw.node SET revoked_at = NULL",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(stmt)  # static statements above


def test_enrolment_code_round_trip_and_rejects_malformed() -> None:
    token = "a" * 26
    assert nodes.parse_enrolment_code(f"{token}.{CFP}.{GFP}") == (token, CFP, GFP)
    for bad in (
        f"{token}.{CFP}",
        f"{token}.{CFP}.{GFP}.x",
        f"{'A' * 26}.{CFP}.{GFP}",
        f"{token}.{CFP[:-1]}.{GFP}",
        f"{token}1.{CFP}.{GFP}",
    ):
        with pytest.raises(ValueError, match="enrolment code"):
            nodes.parse_enrolment_code(bad)


def test_concurrent_use_of_one_token_one_wins(app: Conn, ndb: Dsns) -> None:
    token = _token(app)
    keys = [_key(), _key()]
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def attempt(key: bytes) -> None:
        with psycopg.connect(ndb.gateway, autocommit=True) as conn:
            barrier.wait()
            try:
                outcome: object = nodes.enrol(
                    conn, token=token, public_key=key, tailscale_identity=_ts()
                )
            except EnrolmentRefused as e:
                outcome = e.reason
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(type(r).__name__ for r in results) == ["UUID", "str"]
    assert "bad_token" in results
    count = app.execute(
        "SELECT count(*) FROM gw.node WHERE public_key = ANY(%s)", (keys,)
    ).fetchone()
    assert count == (1,)


def test_enrol_never_updates_existing_node(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    pub1 = _key()
    node1 = nodes.enrol(gateway, token=_token(app), public_key=pub1, tailscale_identity=_ts())
    before = _node_row(app, node1)
    token2 = _token(app)
    since = _seq(app)
    with pytest.raises(EnrolmentRefused) as e:
        nodes.enrol(gateway, token=token2, public_key=pub1, tailscale_identity=_ts())
    assert e.value.reason == "key_in_use"
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2]) for r in rows] == [("ENROL", "REJECTED", "key_in_use")]
    assert _enrolment(app, token2)[1:] == (None, None)  # the token is still unused
    assert _node_row(app, node1) == before


def test_active_tailscale_identity_is_unique(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    ts = _ts()
    node1 = nodes.enrol(gateway, token=_token(app), public_key=_key(), tailscale_identity=ts)
    token2 = _token(app)
    since = _seq(app)
    with pytest.raises(EnrolmentRefused, match="identity_in_use"):
        nodes.enrol(gateway, token=token2, public_key=_key(), tailscale_identity=ts)
    assert [r[2] for r in _audit_seen_elsewhere(ndb.app, since)] == ["identity_in_use"]
    assert _enrolment(app, token2)[1] is None
    # Once the old node is revoked, the identity is free for a new enrolment.
    nodes.revoke(app, node1)
    node2 = nodes.enrol(gateway, token=token2, public_key=_key(), tailscale_identity=ts)
    assert node2 != node1


# --- revocation --------------------------------------------------------------------------------


def test_revoke_returns_explicit_result(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    node_id = _node(app, gateway)
    since = _seq(app)
    nodes.revoke(app, node_id)
    assert _node_row(app, node_id)["revoked_at"] is not None
    with pytest.raises(RevokeRefused) as e:
        nodes.revoke(app, node_id)
    assert e.value.reason == "already_revoked"
    missing = uuid.uuid4()
    with pytest.raises(RevokeRefused, match="unknown_node"):
        nodes.revoke(app, missing)
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("REVOKE", "APPLIED", "revoked", node_id),
        ("REVOKE", "REJECTED", "already_revoked", node_id),
        ("REVOKE", "REJECTED", "unknown_node", missing),
    ]


def test_revoke_fences_the_open_connection(app: Conn, gateway: Conn) -> None:
    node_id = _node(app, gateway)
    conn_id = _open(gateway, node_id)
    assert gateway.execute("SELECT gw.current_node(%s)", (conn_id,)).fetchone() == (node_id,)
    nodes.revoke(app, node_id)
    with pytest.raises(psycopg.Error) as e:
        gateway.execute("SELECT gw.current_node(%s)", (conn_id,))
    assert e.value.sqlstate == FENCED_SQLSTATE
    row = app.execute(
        "SELECT closed_at IS NOT NULL, close_reason FROM gw.node_connection WHERE id = %s",
        (conn_id,),
    ).fetchone()
    assert row == (True, "revoked")


# --- connections and fencing -------------------------------------------------------------------


def test_open_connection_refusals_are_structured_and_audited(
    app: Conn, gateway: Conn, ndb: Dsns
) -> None:
    node_id = _node(app, gateway)
    nodes.revoke(app, node_id)
    missing = uuid.uuid4()
    since = _seq(app)
    for target, reason in ((node_id, "revoked"), (missing, "unknown_node")):
        row = gateway.execute(
            "SELECT conn_id, refusal FROM gw.open_connection(%s)", (target,)
        ).fetchone()
        assert row == (None, reason)
    rows = _audit_seen_elsewhere(ndb.app, since)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("CONNECT_REFUSED", "REJECTED", "revoked", node_id),
        ("CONNECT_REFUSED", "REJECTED", "unknown_node", missing),
    ]


def test_fenced_error_code_is_jf001(app: Conn, gateway: Conn) -> None:
    conn_id = _open(gateway, _node(app, gateway))
    gateway.execute("SELECT gw.close_connection(%s, 'node_closed')", (conn_id,))
    for stmt in ("SELECT gw.current_node(%s)", "SELECT gw.touch_connection(%s)"):
        with pytest.raises(psycopg.Error) as e:
            gateway.execute(stmt, (conn_id,))  # static statements above
        assert e.value.sqlstate == FENCED_SQLSTATE == "JF001"
        assert not isinstance(e.value, psycopg.errors.InsufficientPrivilege)
    with pytest.raises(psycopg.Error) as e:
        gateway.execute("SELECT gw.current_node(%s)", (10**12,))
    assert e.value.sqlstate == "JF001"


def test_close_connection_checks_its_reason(app: Conn, gateway: Conn) -> None:
    conn_id = _open(gateway, _node(app, gateway))
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        gateway.execute("SELECT gw.close_connection(%s, %s)", (conn_id, "Free text here"))
    gateway.execute("SELECT gw.close_connection(%s, 'node_closed')", (conn_id,))
    gateway.execute("SELECT gw.close_connection(%s, 'again')", (conn_id,))  # idempotent
    row = app.execute(
        "SELECT close_reason FROM gw.node_connection WHERE id = %s", (conn_id,)
    ).fetchone()
    assert row == ("node_closed",)


def test_touch_connection_changes_only_last_seen(gateway: Conn, app: Conn) -> None:
    node_id = _node(app, gateway)
    conn_id = _open(gateway, node_id)
    before = _node_row(app, node_id)
    gateway.execute("SELECT gw.touch_connection(%s)", (conn_id,))
    after = _node_row(app, node_id)
    assert before["last_seen_at"] is None and after["last_seen_at"] is not None
    assert {k: v for k, v in after.items() if k != "last_seen_at"} == {
        k: v for k, v in before.items() if k != "last_seen_at"
    }
    ping = app.execute(
        "SELECT last_ping_at IS NOT NULL FROM gw.node_connection WHERE id = %s", (conn_id,)
    ).fetchone()
    assert ping == (True,)


def test_heartbeat_from_superseded_connection_rejected(gateway: Conn, app: Conn) -> None:
    node_id = _node(app, gateway)
    conn1 = _open(gateway, node_id)
    conn2 = _open(gateway, node_id)
    row = app.execute(
        "SELECT closed_at IS NOT NULL, close_reason FROM gw.node_connection WHERE id = %s",
        (conn1,),
    ).fetchone()
    assert row == (True, "superseded")
    before = _node_row(app, node_id)["last_seen_at"]
    with pytest.raises(psycopg.Error) as e:
        gateway.execute("SELECT gw.touch_connection(%s)", (conn1,))
    assert e.value.sqlstate == FENCED_SQLSTATE
    assert _node_row(app, node_id)["last_seen_at"] == before
    open_rows = app.execute(
        "SELECT id FROM gw.node_connection WHERE node_id = %s AND closed_at IS NULL", (node_id,)
    ).fetchall()
    assert open_rows == [(conn2,)]
    gateway.execute("SELECT gw.touch_connection(%s)", (conn2,))


def test_at_most_one_open_connection_per_node(app: Conn, gateway: Conn, owner: Conn) -> None:
    node_id = _node(app, gateway)
    _open(gateway, node_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute("INSERT INTO gw.node_connection (node_id) VALUES (%s)", (node_id,))


# --- strict fencing (R8-1): races in both orders, one lock order ------------------------------

NIL = uuid.UUID(int=0)


class _InThread:
    """Run `fn(conn)` on a new autocommit connection in a thread; keep its pid and outcome."""

    def __init__(self, dsn: str, fn: Any) -> None:
        self.pid: int | None = None
        self.result: Any = None
        self.error: BaseException | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(dsn, fn))
        self._thread.start()
        assert self._ready.wait(10)

    def _run(self, dsn: str, fn: Any) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn:
            self.pid = conn.info.backend_pid
            self._ready.set()
            try:
                self.result = fn(conn)
            except BaseException as e:
                self.error = e

    def join(self) -> None:
        self._thread.join(15)
        assert not self._thread.is_alive()


def _wait_until_lock_blocked(ndb: Dsns, pid: int | None) -> None:
    """Proves a real lock wait (pg_stat_activity), not a sleep."""
    with psycopg.connect(ndb.superuser, autocommit=True) as su:
        for _ in range(400):
            row = su.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
            time.sleep(0.025)
    pytest.fail(f"backend {pid} never waited on a lock")


def _conn_row(app: Conn, conn_id: int) -> tuple[Any, ...]:
    row = app.execute(
        "SELECT closed_at IS NOT NULL, close_reason, last_ping_at FROM gw.node_connection"
        " WHERE id = %s",
        (conn_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_revoke_wins_heartbeat_refused(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    node_id = _node(app, gateway)
    conn_id = _open(gateway, node_id)
    with psycopg.connect(ndb.app) as holder:  # revoke_node, transaction held open
        holder.execute("SELECT * FROM gw.revoke_node(%s)", (node_id,))
        touch = _InThread(
            ndb.gateway, lambda c: c.execute("SELECT gw.touch_connection(%s)", (conn_id,))
        )
        _wait_until_lock_blocked(ndb, touch.pid)
        holder.commit()
    touch.join()
    assert isinstance(touch.error, psycopg.Error) and touch.error.sqlstate == FENCED_SQLSTATE
    assert _node_row(app, node_id)["last_seen_at"] is None
    assert _conn_row(app, conn_id) == (True, "revoked", None)


def test_heartbeat_wins_revoke_waits(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    node_id = _node(app, gateway)
    conn_id = _open(gateway, node_id)
    with psycopg.connect(ndb.gateway) as holder:  # touch_connection, transaction held open
        holder.execute("SELECT gw.touch_connection(%s)", (conn_id,))
        revoke = _InThread(ndb.app, lambda c: nodes.revoke(c, node_id))
        _wait_until_lock_blocked(ndb, revoke.pid)
        holder.commit()
    revoke.join()
    assert revoke.error is None
    node = _node_row(app, node_id)
    assert node["revoked_at"] is not None and node["last_seen_at"] is not None
    closed, reason, ping = _conn_row(app, conn_id)
    assert (closed, reason) == (True, "revoked") and ping is not None


def test_supersede_wins_write_refused(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    node_id = _node(app, gateway)
    old = _open(gateway, node_id)
    with psycopg.connect(ndb.gateway) as holder:  # open_connection(new), held open
        row = holder.execute("SELECT conn_id FROM gw.open_connection(%s)", (node_id,)).fetchone()
        assert row is not None
        write = _InThread(
            ndb.gateway, lambda c: c.execute("SELECT gw.current_node(%s)", (old,)).fetchone()
        )
        _wait_until_lock_blocked(ndb, write.pid)
        holder.commit()
    write.join()
    assert isinstance(write.error, psycopg.Error) and write.error.sqlstate == FENCED_SQLSTATE
    with pytest.raises(psycopg.Error) as e:
        gateway.execute("SELECT gw.current_node(%s)", (old,))
    assert e.value.sqlstate == FENCED_SQLSTATE


def test_write_wins_supersede_waits(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    node_id = _node(app, gateway)
    old = _open(gateway, node_id)
    with psycopg.connect(ndb.gateway) as holder:  # a write's fence: current_node(old), held
        row = holder.execute("SELECT gw.current_node(%s)", (old,)).fetchone()
        assert row == (node_id,)
        opener = _InThread(
            ndb.gateway,
            lambda c: c.execute(
                "SELECT conn_id, refusal FROM gw.open_connection(%s)", (node_id,)
            ).fetchone(),
        )
        _wait_until_lock_blocked(ndb, opener.pid)
        assert _conn_row(app, old)[0] is False  # the old connection is still open meanwhile
        holder.commit()
    opener.join()
    assert opener.error is None and opener.result[1] is None
    assert _conn_row(app, old)[:2] == (True, "superseded")


def test_no_deadlock_under_mixed_load(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    lock = threading.Lock()
    node_ids = [_node(app, gateway) for _ in range(2)]
    current: dict[uuid.UUID, int] = {n: _open(gateway, n) for n in node_ids}
    all_nodes = list(node_ids)
    failures: list[BaseException] = []

    def worker(seed: int) -> None:
        rnd = random.Random(seed)  # noqa: S311  # a reproducible test load, not crypto
        with (
            psycopg.connect(ndb.gateway, autocommit=True) as gw_conn,
            psycopg.connect(ndb.app, autocommit=True) as app_conn,
        ):
            for _ in range(200):
                with lock:
                    slot = rnd.randrange(len(node_ids))
                    node_id = node_ids[slot]
                    conn_id = current.get(node_id, 0)
                op = rnd.choices(
                    ["touch", "close", "open", "revoke", "current"], [40, 10, 15, 2, 33]
                )[0]
                try:
                    if op == "touch":
                        gw_conn.execute("SELECT gw.touch_connection(%s)", (conn_id,))
                    elif op == "close":
                        gw_conn.execute("SELECT gw.close_connection(%s, 'node_closed')", (conn_id,))
                    elif op == "open":
                        row = gw_conn.execute(
                            "SELECT conn_id FROM gw.open_connection(%s)", (node_id,)
                        ).fetchone()
                        if row is not None and row[0] is not None:
                            with lock:
                                current[node_id] = int(row[0])
                    elif op == "revoke":
                        with contextlib.suppress(RevokeRefused):
                            nodes.revoke(app_conn, node_id)
                        new = nodes.enrol(
                            gw_conn,
                            token=_token(app_conn),
                            public_key=_key(),
                            tailscale_identity=_ts(),
                        )
                        with lock:
                            node_ids[slot] = new
                            all_nodes.append(new)
                    else:
                        with gw_conn.transaction():
                            gw_conn.execute("SELECT gw.current_node(%s)", (conn_id,))
                            time.sleep(0.001)  # hold the fence briefly, like a real write
                except psycopg.Error as e:
                    if e.sqlstate not in (FENCED_SQLSTATE, "22023"):  # fenced or unknown conn
                        failures.append(e)
                except BaseException as e:
                    failures.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads)
    assert failures == []
    # Final state: one open connection at most per node, none on a revoked node, and the
    # permanent audit agrees with the connection rows.
    bad = app.execute(
        "SELECT count(*) FROM gw.node_connection c JOIN gw.node n ON n.id = c.node_id"
        " WHERE n.id = ANY(%s) AND c.closed_at IS NULL AND n.revoked_at IS NOT NULL",
        (all_nodes,),
    ).fetchone()
    assert bad == (0,)
    counts = app.execute(
        "SELECT (SELECT count(*) FROM gw.node_connection WHERE node_id = ANY(%(n)s)),"
        " (SELECT count(*) FROM gw.node_connection WHERE node_id = ANY(%(n)s)"
        "  AND closed_at IS NOT NULL),"
        " (SELECT count(*) FROM audit.event WHERE action = 'CONNECT'"
        "  AND entity_id = ANY(%(n)s)),"
        " (SELECT count(*) FROM audit.event WHERE action = 'DISCONNECT'"
        "  AND entity_id = ANY(%(n)s))",
        {"n": all_nodes},
    ).fetchone()
    assert counts is not None and counts[0] == counts[2] and counts[1] == counts[3]


def test_gw_connection_functions_do_not_call_current_node(owner: Conn) -> None:
    for signature in (
        "gw.open_connection(uuid)",
        "gw.close_connection(bigint,text)",
        "gw.touch_connection(bigint)",
        "gw.enrol_node(text,bytea,text)",
    ):
        row = owner.execute(
            "SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure", (signature,)
        ).fetchone()
        assert row is not None and "current_node" not in row[0], signature


# --- connection audit (R8-2) ---------------------------------------------------------------


def _audit_rows(app: Conn, since: int) -> list[tuple[Any, ...]]:
    return app.execute(
        "SELECT action::text, outcome::text, reason, entity_id, details FROM audit.event"
        " WHERE seq > %s AND entity_type = 'node' ORDER BY seq",
        (since,),
    ).fetchall()


def test_connect_disconnect_audited(app: Conn, gateway: Conn) -> None:
    node_id = _node(app, gateway)
    n = str(node_id)
    since = _seq(app)
    first = _open(gateway, node_id)
    gateway.execute("SELECT gw.touch_connection(%s)", (first,))  # heartbeats aren't audited
    second = _open(gateway, node_id)
    row = gateway.execute("SELECT gw.close_connection(%s, 'node_closed')", (second,)).fetchone()
    assert row == (True,)
    third = _open(gateway, node_id)
    nodes.revoke(app, node_id)
    rows = _audit_rows(app, since)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("CONNECT", "APPLIED", "connected", node_id),
        ("DISCONNECT", "APPLIED", "superseded", node_id),
        ("CONNECT", "APPLIED", "connected", node_id),
        ("DISCONNECT", "APPLIED", "node_closed", node_id),
        ("CONNECT", "APPLIED", "connected", node_id),
        ("REVOKE", "APPLIED", "revoked", node_id),
        ("DISCONNECT", "APPLIED", "revoked", node_id),
    ]
    assert [r[4] for r in rows] == [
        {"node_id": n, "conn_id": first, "superseded_conn_id": None},
        {"node_id": n, "conn_id": first, "reason": "superseded"},
        {"node_id": n, "conn_id": second, "superseded_conn_id": first},
        {"node_id": n, "conn_id": second, "reason": "node_closed"},
        {"node_id": n, "conn_id": third, "superseded_conn_id": None},
        {},
        {"node_id": n, "conn_id": third, "reason": "revoked"},
    ]


def test_connection_audit_outlives_connection_rows(app: Conn, gateway: Conn, owner: Conn) -> None:
    node_id = _node(app, gateway)
    since = _seq(app)
    _open(gateway, node_id)
    _open(gateway, node_id)
    owner.execute("DELETE FROM gw.node_connection WHERE node_id = %s", (node_id,))  # retention
    assert [r[0] for r in _audit_rows(app, since)] == ["CONNECT", "DISCONNECT", "CONNECT"]


def test_close_connection_twice_is_noop(app: Conn, gateway: Conn) -> None:
    conn_id = _open(gateway, _node(app, gateway))
    since = _seq(app)
    results = [
        gateway.execute("SELECT gw.close_connection(%s, 'node_closed')", (conn_id,)).fetchone()
        for _ in range(2)
    ]
    assert results == [(True,), (False,)]
    assert [r[0] for r in _audit_rows(app, since)] == ["DISCONNECT"]


def test_close_after_revoke_allowed(app: Conn, gateway: Conn) -> None:
    node_id = _node(app, gateway)
    conn_id = _open(gateway, node_id)
    nodes.revoke(app, node_id)
    since = _seq(app)
    row = gateway.execute("SELECT gw.close_connection(%s, 'node_closed')", (conn_id,)).fetchone()
    assert row == (False,)
    assert _audit_rows(app, since) == []
    assert _conn_row(app, conn_id)[:2] == (True, "revoked")


def test_refused_enrolment_audit_uses_enrolment_id(app: Conn, gateway: Conn, owner: Conn) -> None:
    used = _token(app)
    nodes.enrol(gateway, token=used, public_key=_key(), tailscale_identity=_ts())
    expired = _token(app)
    owner.execute(
        "UPDATE gw.enrolment SET expires_at = now() - interval '1 second' WHERE id = %s",
        (_enrolment(app, expired)[0],),
    )
    since = _seq(app)
    for token in (used, expired, "a" * 26):
        with pytest.raises(EnrolmentRefused, match="bad_token"):
            nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    assert [r[3] for r in _audit_rows(app, since)] == [
        _enrolment(app, used)[0],
        _enrolment(app, expired)[0],
        NIL,
    ]


def test_token_length_checked(app: Conn, gateway: Conn) -> None:
    since = _seq(app)
    for token in ("a" * 25, "a" * 27):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            gateway.execute("SELECT * FROM gw.enrol_node(%s, %s, %s)", (token, _key(), _ts()))
    assert _audit_rows(app, since) == []


# --- the gateway's privileges (P2-S6) ----------------------------------------------------------


def test_gateway_cannot_insert_node_directly(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute(
            "INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)"
            " VALUES (%s, 'DESKTOP', 'x', %s, 'ts:x')",
            (MANJ, _key()),
        )


def test_gateway_cannot_read_enrolment(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT * FROM gw.enrolment")


def test_gateway_cannot_revoke(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT * FROM gw.revoke_node(%s)", (uuid.uuid4(),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT gw.create_enrolment(%s, %s, 'DESKTOP', 'pc')", (b"\x00" * 32, MANJ))


def test_gateway_cannot_change_node_key(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("UPDATE gw.node SET public_key = %s", (_key(),))


def test_gateway_cannot_change_node_identity(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("UPDATE gw.node SET tailscale_identity = 'ts:other'")


def test_gateway_cannot_unrevoke_or_repoint(gateway: Conn) -> None:
    for stmt in (
        "UPDATE gw.node SET revoked_at = NULL",
        "UPDATE gw.node SET person_id = '00000000-0000-4000-8000-000000000001'",
        "UPDATE gw.node SET kind = 'PHONE'",
        "UPDATE gw.node SET last_seen_at = now()",
        "DELETE FROM gw.node",
        "UPDATE gw.node_connection SET closed_at = NULL",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(stmt)  # static statements above


def test_gateway_reads_only_the_granted_node_columns(app: Conn, gateway: Conn) -> None:
    node_id = _node(app, gateway)
    row = gateway.execute(
        "SELECT id, person_id, kind, public_key, tailscale_identity, revoked_at FROM gw.node"
        " WHERE id = %s",
        (node_id,),
    ).fetchone()
    assert row is not None and row[0] == node_id
    for column in ("name", "registered_at", "last_seen_at"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(f"SELECT {column} FROM gw.node")


def test_gateway_has_no_write_privilege_anywhere(owner: Conn) -> None:
    """Catalogue-driven: no INSERT, UPDATE, DELETE or TRUNCATE on any table, view or sequence
    in any schema, at table or column level. The brief grants no column UPDATE at all:
    gw.node.last_seen_at changes only through gw.touch_connection."""
    tables = owner.execute(
        "SELECT c.oid::regclass::text, c.relkind FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
        " AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
    ).fetchall()
    assert any(name == "gw.node" for name, _ in tables)
    granted: list[str] = []
    for name, kind in tables:
        if kind == "S":
            privileges = ("USAGE", "UPDATE")
            for p in privileges:
                ok = owner.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)", (GATEWAY_ROLE, name, p)
                ).fetchone()
                if ok and ok[0]:
                    granted.append(f"{p} {name}")
            continue
        for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            ok = owner.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (GATEWAY_ROLE, name, p)
            ).fetchone()
            if ok and ok[0]:
                granted.append(f"{p} {name}")
        columns = owner.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0"
            " AND NOT attisdropped",
            (name,),
        ).fetchall()
        for (column,) in columns:
            for p in ("INSERT", "UPDATE", "REFERENCES"):
                ok = owner.execute(
                    "SELECT has_column_privilege(%s, %s, %s, %s)",
                    (GATEWAY_ROLE, name, column, p),
                ).fetchone()
                if ok and ok[0]:
                    granted.append(f"{p} {name}.{column}")
    assert granted == []


def test_gateway_execute_surface_is_exactly_five(owner: Conn) -> None:
    """Every non-system, non-trigger function. Trigger functions are excluded: they can only
    run as triggers, never be called."""
    rows = owner.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        " AND p.prorettype <> 'trigger'::regtype"
        " AND has_function_privilege(%s, p.oid, 'EXECUTE')",
        (GATEWAY_ROLE,),
    ).fetchall()
    assert {r[0].replace(" ", "") for r in rows} == GATEWAY_FUNCTIONS
    # Each is SECURITY DEFINER, owned by jarvis_owner, and executable by exactly one role.
    for signature in GATEWAY_FUNCTIONS:
        row = owner.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner),"
            " (SELECT array_agg(pg_get_userbyid(a.grantee)::text ORDER BY 1)"
            "  FROM aclexplode(p.proacl) a"
            "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature,),
        ).fetchone()
        assert row == (True, "jarvis_owner", [GATEWAY_ROLE]), signature
    for signature in ("gw.create_enrolment(bytea,uuid,text,text)", "gw.revoke_node(uuid)"):
        row = owner.execute(
            "SELECT p.prosecdef, (SELECT array_agg(pg_get_userbyid(a.grantee)::text)"
            "  FROM aclexplode(p.proacl) a"
            "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature,),
        ).fetchone()
        assert row == (True, ["jarvis_app"]), signature


# --- P2-S6 catalogue tests (cover every later migration too) -----------------------------------


def test_security_definer_functions_have_fixed_search_path(owner: Conn) -> None:
    rows = owner.execute("""SELECT p.oid::regprocedure, p.proconfig FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef
        AND n.nspname IN ('gw','conv','comm','ledger','ops','audit')""").fetchall()
    assert rows and all(cfg and "search_path=pg_catalog, pg_temp" in cfg for _, cfg in rows)


def test_no_function_executable_by_public(owner: Conn) -> None:
    rows = owner.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE p.prosecdef"
        " AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        " AND (p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a"
        "      WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'))"
    ).fetchall()
    total = owner.execute("SELECT count(*) FROM pg_proc WHERE prosecdef").fetchone()
    assert total is not None and total[0] > 0
    assert rows == []


# --- the CLI (jarvis-core node enrol / node revoke) -------------------------------------------


def test_cli_enrol_and_revoke(
    ndb: Dsns,
    gateway: Conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parts = conninfo_to_dict(ndb.app)
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "pg_app_password").write_text(str(parts["password"]))
    core_key = Ed25519PrivateKey.generate()
    (secret_dir / "core_signing_key").write_bytes(
        core_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    gw_public = public_bytes(Ed25519PrivateKey.generate())
    (secret_dir / "gateway_signing_key.pub").write_bytes(gw_public)
    people = tmp_path / "memory" / "people"
    people.mkdir(parents=True)
    person = memory_format.Person("manj", MANJ, "Manj")
    (people / "manj.md").write_text(memory_format.render_file("people/manj.md", [], person))
    monkeypatch.setattr(config, "SECRETS_DIR", secret_dir)
    monkeypatch.setenv("JARVIS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("JARVIS_DB_HOST", str(parts["host"]))
    monkeypatch.setenv("JARVIS_DB_PORT", str(parts.get("port", "5432")))
    monkeypatch.setenv("JARVIS_DB_NAME", ndb.database)

    argv = ["node", "enrol", "--person", "manj", "--kind", "desktop", "--name", "study PC"]
    assert core_main.main(argv) == 0
    token, core_fp, gw_fp = nodes.parse_enrolment_code(capsys.readouterr().out.split()[-1])
    assert core_fp == fingerprint(public_bytes(core_key)) and gw_fp == fingerprint(gw_public)
    node_id = nodes.enrol(gateway, token=token, public_key=_key(), tailscale_identity=_ts())
    assert core_main.main(["node", "revoke", str(node_id)]) == 0
    assert core_main.main(["node", "revoke", str(node_id)]) == 1
    with pytest.raises(SystemExit):
        core_main.main(["node", "enrol", "--person", "nobody", "--kind", "PHONE", "--name", "x"])


# --- migration 0008 rollback (R7-6) ------------------------------------------------------------


def _gateway_acl_entries(conn: Conn) -> int:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a"
        "        WHERE a.grantee = %(r)s::regrole)"
        "     + (SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a"
        "        WHERE a.grantee = %(r)s::regrole)"
        "     + (SELECT count(*) FROM pg_namespace s, aclexplode(s.nspacl) a"
        "        WHERE a.grantee = %(r)s::regrole)"
        "     + (SELECT count(*) FROM pg_attribute t, aclexplode(t.attacl) a"
        "        WHERE a.grantee = %(r)s::regrole)",
        {"r": GATEWAY_ROLE},
    ).fetchone()
    assert row is not None
    return int(row[0])


def _gw_fingerprint(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'gw' ORDER BY 1, 2"
    ).fetchall()


def test_0008_down_and_up_without_node_rows() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.owner, autocommit=True) as conn:
            before = _gw_fingerprint(conn)
            grants_before = _gateway_acl_entries(conn)
        assert before and grants_before > 0
        migrate.downgrade(d.owner, "0007")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute("SELECT to_regnamespace('gw')").fetchone() == (None,)
            assert _gateway_acl_entries(conn) == 0
            labels = conn.execute("SELECT unnest(enum_range(NULL::audit.action))::text").fetchall()
            assert [r[0] for r in labels] == ACTIONS_0007
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id,"
                    " action, outcome, reason) VALUES ('SYSTEM', 'system', 'node',"
                    " gen_random_uuid(), 'CREATE', 'APPLIED', 'probe')"
                )
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert _gw_fingerprint(conn) == before
            assert _gateway_acl_entries(conn) == grants_before


def test_0008_downgrade_refuses_once_a_node_exists(app: Conn, gateway: Conn, ndb: Dsns) -> None:
    _node(app, gateway)
    with pytest.raises(Exception, match="node rows exist"):
        migrate.downgrade(ndb.owner, "0007")
    with psycopg.connect(ndb.owner, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('gw.node')").fetchone() == ("gw.node",)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0008",)


def test_0008_downgrade_refuses_once_an_enrolment_exists() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.app, autocommit=True) as conn:
            _token(conn)
        with pytest.raises(Exception, match="node rows exist"):
            migrate.downgrade(d.owner, "0007")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM gw.enrolment").fetchone() == (1,)
