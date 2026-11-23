"""Approvals with single, audited consumption (migration 0010; spec §5.5, §9.4; R7-1, R10-1,
R10-2).

Runs in its own database: approval rows and approval audit rows block the 0010 downgrade, so
they must never reach the session database that P0-6 takes down and up. People, nodes, keys
and Tailscale identities are fictional test values.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server import approvals_db, ledger, migrate, nodes
from jarvis_server.bootstrap import APP_ROLE
from jarvis_server.contracts import ActorKind
from jarvis_server.dbcall import ConsumeRefused, Refused, TransactionNotIdle
from jarvis_server.signing import fingerprint

from .conftest import Dsns, fresh_database
from .helpers.threads import InThread, run_all

Conn = psycopg.Connection[Any]

MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
CFP = fingerprint(bytes(range(32)))
GFP = fingerprint(bytes(range(32, 64)))
TEST = ledger.Actor(ActorKind.SYSTEM, "test-runner")

CONSUME = "SELECT execution_key, refusal FROM ledger.consume_approval(%s)"
ROW_LOCK_WAITS = ("transactionid", "tuple")  # pg_stat_activity.wait_event of a row-lock wait

ACTIONS_0009 = [
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
    "INBOUND",
    "SEND",
    "RECEIVE",
    "ACK",
    "SUPERSEDE",
    "EXPIRE",
    "CHUNK_RECEIVE",
]

CARD_AUTHZ = {
    "object": {
        "v": 1,
        "kind": "approval.card",
        "action_kind": "file.delete",
        "args": {"name": "fictional-test.txt"},
        "target_node_id": None,
        "person_id": None,
    },
    "key_id": "core-test",
    "sig": "c2lnbmF0dXJl",
}


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    """Overrides the session database for this module (and its app/gateway/owner fixtures)."""
    with fresh_database() as d:
        yield d


@pytest.fixture(scope="module")
def ids(dsns: Dsns) -> tuple[uuid.UUID, uuid.UUID]:
    """(task_id, node_id): one fictional task and one enrolled desktop node, shared."""
    with (
        psycopg.connect(dsns.app, autocommit=True) as app,
        psycopg.connect(dsns.gateway, autocommit=True) as gateway,
    ):
        return _task(app), _node(app, gateway)


# --- helpers -----------------------------------------------------------------------------------


def _one(conn: Conn, sql: Any, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _task(app: Conn) -> uuid.UUID:
    task = ledger.create_task(
        app,
        TEST,
        title="Delete a fictional test file",
        definition_of_done="the file is gone",
        source_ref="test",
        owner_person_id=MANJ,
    )
    return task.id


def _node(app: Conn, gateway: Conn) -> uuid.UUID:
    code = nodes.create_enrolment(
        app, person_id=MANJ, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP
    )
    token, _, _ = nodes.parse_enrolment_code(code)
    return nodes.enrol(
        gateway,
        token=token,
        public_key=secrets.token_bytes(32),
        tailscale_identity=f"ts:pc-{secrets.token_hex(4)}",
    )


def _approval(
    conn: Conn, ids: tuple[uuid.UUID, uuid.UUID], expires: str = "10 minutes"
) -> uuid.UUID:
    """A PENDING approval expiring `expires` from now (a negative interval: already expired)."""
    task_id, node_id = ids
    return uuid.UUID(
        str(
            _one(
                conn,
                "INSERT INTO ledger.approval (task_id, requested_from_person_id,"
                " requesting_node_id, tier, action_kind, action_args, summary, card_authz,"
                " expires_at) VALUES (%s, %s, %s, 'IRREVERSIBLE', 'file.delete', %s,"
                " 'Delete fictional-test.txt', %s, clock_timestamp() + %s::interval)"
                " RETURNING id",
                (
                    task_id,
                    MANJ,
                    node_id,
                    Jsonb({"name": "fictional-test.txt", "size": 12}),
                    Jsonb(CARD_AUTHZ),
                    expires,
                ),
            )
        )
    )


def _decide(conn: Conn, approval: uuid.UUID, decision: str, node_id: uuid.UUID | None) -> None:
    conn.execute(
        "UPDATE ledger.approval SET decision = %s, decided_at = now(), decided_by_node_id = %s"
        " WHERE id = %s",
        (decision, node_id, approval),
    )


def _approved(app: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    approval = _approval(app, ids)
    _decide(app, approval, "APPROVED", ids[1])
    return approval


def _approved_then_expired(app: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    """APPROVED while in date, then left to pass its expiry (about half a second)."""
    approval = _approval(app, ids, "500 milliseconds")
    _decide(app, approval, "APPROVED", ids[1])
    deadline = time.monotonic() + 5
    while not _one(
        app, "SELECT clock_timestamp() > expires_at FROM ledger.approval WHERE id = %s", (approval,)
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return approval


def _seq(conn: Conn) -> int:
    return int(_one(conn, "SELECT coalesce(max(seq), 0) FROM audit.event"))


def _audit(conn: Conn, since: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT entity_type, entity_id, action::text, outcome::text, reason, details"
        " FROM audit.event WHERE seq > %s ORDER BY seq",
        (since,),
    ).fetchall()


def _row(conn: Conn, approval: uuid.UUID) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT decision, consumed_at, execution_key FROM ledger.approval WHERE id = %s",
        (approval,),
    ).fetchone()
    assert row is not None
    return tuple(row)


# --- decision guards (the backstop trigger, R10-2) ---------------------------------------------


def test_decision_only_from_pending_before_expiry(
    app: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    node_id = ids[1]
    for decision in ("APPROVED", "DENIED"):  # P2-E11: at or after expires_at → raises
        late = _approval(app, ids, "-1 second")
        since = _seq(app)
        with pytest.raises(psycopg.errors.CheckViolation):
            _decide(app, late, decision, node_id)
        assert _row(app, late) == ("PENDING", None, None)
        assert _audit(app, since) == []
    expired = _approval(app, ids, "-1 second")
    _decide(app, expired, "EXPIRED", None)  # the sweep may mark it expired
    assert _row(app, expired)[0] == "EXPIRED"
    # In date: each decision is accepted from PENDING, and only from PENDING.
    for first, second in (
        ("APPROVED", "DENIED"),
        ("DENIED", "APPROVED"),
        ("EXPIRED", "APPROVED"),
        ("APPROVED", "PENDING"),
        ("APPROVED", "EXPIRED"),
    ):
        approval = _approval(app, ids)
        _decide(app, approval, first, None if first == "EXPIRED" else node_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            _decide(app, approval, second, None if second in ("PENDING", "EXPIRED") else node_id)
        assert _row(app, approval)[0] == first


def test_decided_approval_needs_decider_and_time(
    app: Conn, owner: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """APPROVED or DENIED needs both decided_at and decided_by_node_id; EXPIRED needs neither."""
    task_id, node_id = ids
    for decision in ("APPROVED", "DENIED"):
        for decided_at, decider in (("now()", "NULL"), ("NULL", "%s"), ("NULL", "NULL")):
            approval = _approval(app, ids)
            params: tuple[Any, ...] = (node_id, approval) if decider == "%s" else (approval,)
            with pytest.raises(psycopg.errors.CheckViolation) as e:
                app.execute(
                    f"UPDATE ledger.approval SET decision = '{decision}',"
                    f" decided_at = {decided_at}, decided_by_node_id = {decider}"
                    " WHERE id = %s".encode(),
                    params,
                )
            assert e.value.diag.constraint_name == "approval_decided_by_node_and_time"
            assert _row(app, approval) == ("PENDING", None, None)
            # An insert in that shape is refused too (the guard trigger refuses it first).
            for conn in (app, owner):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(
                        "INSERT INTO ledger.approval (task_id, requested_from_person_id,"
                        " requesting_node_id, tier, action_kind, action_args, summary,"
                        " card_authz, expires_at, decision, decided_at, decided_by_node_id)"
                        " VALUES (%s, %s, %s, 'IRREVERSIBLE', 'file.delete', '{}', 'x', %s,"
                        f" now() + interval '10 minutes', '{decision}', {decided_at},"
                        f" {decider})".encode(),
                        (task_id, MANJ, node_id, Jsonb(CARD_AUTHZ))
                        + ((node_id,) if decider == "%s" else ()),
                    )
    expired = _approval(app, ids, "-1 second")
    app.execute(  # expiry is not a node decision
        "UPDATE ledger.approval SET decision = 'EXPIRED' WHERE id = %s", (expired,)
    )
    assert _row(app, expired)[0] == "EXPIRED"


def test_new_approval_starts_pending_and_unconsumed(
    app: Conn, owner: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    task_id, node_id = ids
    base = (
        "INSERT INTO ledger.approval (task_id, requested_from_person_id, requesting_node_id,"
        " tier, action_kind, action_args, summary, card_authz, expires_at"
    )
    values = (
        " VALUES (%s, %s, %s, 'IRREVERSIBLE', 'file.delete', '{}', 'x', %s,"
        " now() + interval '10 minutes'"
    )
    params = (task_id, MANJ, node_id, Jsonb(CARD_AUTHZ))
    for extra, value in (
        (", decision", ", 'APPROVED'"),
        (", decided_at", ", now()"),
        (", consumed_at", ", now()"),
        (", execution_key", ", gen_random_uuid()"),
    ):
        for conn in (app, owner):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute((base + extra + ")" + values + value + ")").encode(), params)
    _approval(app, ids)  # the plain shape is accepted


def test_request_fields_are_immutable(
    app: Conn, owner: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    approval = _approved(app, ids)
    for column, value in (
        ("action_args", Jsonb({"name": "another.txt"})),
        ("action_kind", "file.move"),
        ("summary", "something else"),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("card_authz", Jsonb({**CARD_AUTHZ, "sig": "b3RoZXI"})),
        ("tier", "READ_ONLY"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                f"UPDATE ledger.approval SET {column} = %s WHERE id = %s".encode(),
                (value, approval),
            )


def test_trigger_is_backstop(app: Conn, owner: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    approval = _approved(app, ids)
    since = _seq(app)
    # The owner holds every privilege, so only the trigger stands in the way.
    for assignment in (
        "execution_key = gen_random_uuid()",
        "consumed_at = now()",
        "consumed_at = now(), execution_key = gen_random_uuid()",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                f"UPDATE ledger.approval SET {assignment} WHERE id = %s".encode(), (approval,)
            )
    # Changing a decided approval: APPROVED → DENIED, by the app.
    with pytest.raises(psycopg.errors.CheckViolation):
        _decide(app, approval, "DENIED", ids[1])
    # The app has no column privilege for consumption at all (defence in depth).
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "UPDATE ledger.approval SET execution_key = gen_random_uuid() WHERE id = %s",
            (approval,),
        )
    assert _row(app, approval) == ("APPROVED", None, None)
    assert _audit(app, since) == []  # the backstop writes no audit row
    # Once consumed, the key and time can't be changed or cleared, even by the owner.
    key = approvals_db.consume(app, approval)
    since = _seq(app)
    for assignment in (
        "execution_key = gen_random_uuid()",
        "consumed_at = now() + interval '1 second'",
        "consumed_at = NULL, execution_key = NULL",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                f"UPDATE ledger.approval SET {assignment} WHERE id = %s".encode(), (approval,)
            )
    assert _row(app, approval)[2] == key
    assert _audit(app, since) == []


def test_consumption_flag_is_bound_to_one_row(
    owner: Conn, app: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The transaction-local flag that consume_approval sets authorises only its own row and
    only the exact consumption shape, never a decision change or a second consumption."""
    approval, other = _approved(app, ids), _approved(app, ids)
    pending = _approval(app, ids)
    consume_shape = (
        "UPDATE ledger.approval SET consumed_at = now(), execution_key = gen_random_uuid()"
        " WHERE id = %s"
    )
    for flagged, target in ((other, approval), (pending, pending)):  # another row; not APPROVED
        with owner.transaction():
            owner.execute("SELECT set_config('jarvis.consume_approval', %s, true)", (str(flagged),))
            with pytest.raises(psycopg.errors.CheckViolation), owner.transaction():
                owner.execute(consume_shape, (target,))
    assert _row(app, approval) == ("APPROVED", None, None)
    assert _row(app, pending) == ("PENDING", None, None)
    # The flag is cleared as soon as consume_approval's UPDATE is done, inside its transaction.
    with app.transaction():
        key, refusal = _one_row(app, CONSUME, (approval,))
        assert refusal is None and key is not None
        assert _one(app, "SELECT current_setting('jarvis.consume_approval', true)") == ""


# --- consume_approval (the refusal path, R10-1) -------------------------------------------------


def test_consume_once(app: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    approval = _approved(app, ids)
    since = _seq(app)
    key, refusal = _one_row(app, CONSUME, (approval,))
    assert refusal is None and isinstance(key, uuid.UUID)
    decision, consumed_at, stored = _row(app, approval)
    assert (decision, stored) == ("APPROVED", key) and consumed_at is not None
    assert _audit(app, since) == [
        ("approval", approval, "CONSUME", "APPLIED", "consumed", {"task_id": str(ids[0])})
    ]
    since = _seq(app)
    assert _one_row(app, CONSUME, (approval,)) == (None, "already_consumed")
    assert _row(app, approval) == (decision, consumed_at, key)  # nothing reissued
    assert _audit(app, since) == [
        ("approval", approval, "CONSUME", "REJECTED", "already_consumed", {"decision": "APPROVED"})
    ]


def _one_row(conn: Conn, sql: Any, params: tuple[Any, ...]) -> tuple[Any, ...]:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return tuple(row)


def test_consume_refused_unless_approved_and_in_date(
    app: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    pending = _approval(app, ids)
    denied = _approval(app, ids)
    _decide(app, denied, "DENIED", ids[1])
    expired = _approval(app, ids, "-1 second")
    _decide(app, expired, "EXPIRED", None)
    late = _approved_then_expired(app, ids)
    unknown = uuid.uuid4()
    cases = [
        (pending, "not_approved", {"decision": "PENDING"}),
        (denied, "not_approved", {"decision": "DENIED"}),
        (expired, "not_approved", {"decision": "EXPIRED"}),
        (late, "expired", {"decision": "APPROVED"}),
        (unknown, "unknown_approval", {}),
    ]
    for approval, reason, details in cases:
        before = _row(app, approval) if approval != unknown else None
        since = _seq(app)
        assert _one_row(app, CONSUME, (approval,)) == (None, reason)
        assert _audit(app, since) == [
            ("approval", approval, "CONSUME", "REJECTED", reason, details)
        ]
        if before is not None:
            assert _row(app, approval) == before
            assert before[1:] == (None, None)


def test_consume_malformed_call_raises(app: Conn) -> None:
    since = _seq(app)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app.execute(CONSUME, (None,))
    assert _audit(app, since) == []


def test_consume_wrapper_returns_key(app: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    approval = _approved(app, ids)
    key = approvals_db.consume(app, approval)
    assert _row(app, approval)[2] == key


def test_consume_refusal_audit_survives_python_boundary(
    dsns: Dsns, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with psycopg.connect(dsns.app, autocommit=True) as setup:
        pending = _approval(setup, ids)
        since = _seq(setup)
    with psycopg.connect(dsns.app) as conn:  # not autocommit: audited_call opens its own
        with pytest.raises(ConsumeRefused) as e:
            approvals_db.consume(conn, pending)
        assert isinstance(e.value, Refused) and e.value.reason == "not_approved"
        conn.rollback()  # whatever the caller does next, the refusal is already committed
    with psycopg.connect(dsns.app, autocommit=True) as other:  # a separate connection
        assert _audit(other, since) == [
            ("approval", pending, "CONSUME", "REJECTED", "not_approved", {"decision": "PENDING"})
        ]


def test_consume_wrapper_refuses_inside_open_transaction(
    dsns: Dsns, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with psycopg.connect(dsns.app) as conn:
        approval = _approved(conn, ids)  # leaves a transaction open
        with pytest.raises(TransactionNotIdle):
            approvals_db.consume(conn, approval)
        conn.rollback()


# --- concurrency --------------------------------------------------------------------------------


def test_concurrent_consume_one_wins(
    dsns: Dsns, app: Conn, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    approval = _approved(app, ids)
    since = _seq(app)
    held = threading.Event()
    release = threading.Event()

    def first(c: Conn) -> tuple[Any, ...]:
        with c.transaction():  # consume, then hold the row lock until released
            row = _one_row(c, CONSUME, (approval,))
            held.set()
            assert release.wait(15), "never released"
        return row

    def second(c: Conn) -> tuple[Any, ...]:
        return _one_row(c, CONSUME, (approval,))

    a = InThread(dsns.app, first)
    try:
        deadline = time.monotonic() + 15
        while not held.wait(0.05):
            if a.error is not None:
                a.join()  # re-raises the first thread's error
            assert time.monotonic() < deadline, "the first consume never ran"
        b = InThread(dsns.app, second)
        assert b.wait_until_blocked(dsns.superuser) in ROW_LOCK_WAITS
        assert _row(app, approval) == ("APPROVED", None, None)  # nothing committed yet
    finally:
        release.set()
    won = a.join()
    lost = b.join()
    assert won[1] is None and isinstance(won[0], uuid.UUID)
    assert lost == (None, "already_consumed")
    assert _row(app, approval)[2] == won[0]
    rows = _audit(app, since)
    assert [(r[2], r[3], r[4]) for r in rows] == [
        ("CONSUME", "APPLIED", "consumed"),
        ("CONSUME", "REJECTED", "already_consumed"),
    ]


def test_consume_race_many_times(dsns: Dsns, app: Conn, ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Unheld races: every approval is issued exactly once across four racing callers."""
    for approval in [_approved(app, ids) for _ in range(10)]:
        results = _race(dsns, approval, 4)
        winners = [r for r in results if r[1] is None]
        assert len(winners) == 1, results
        assert sorted(r[1] for r in results if r[1] is not None) == ["already_consumed"] * 3
        assert _row(app, approval)[2] == winners[0][0]


def _race(dsns: Dsns, approval: uuid.UUID, n: int) -> list[tuple[Any, ...]]:
    results: list[tuple[Any, ...]] = []
    barrier = threading.Barrier(n, timeout=15)
    lock = threading.Lock()

    def racer() -> None:
        with psycopg.connect(dsns.app, autocommit=True) as c:
            barrier.wait()
            row = _one_row(c, CONSUME, (approval,))
        with lock:
            results.append(row)

    run_all([racer] * n, timeout=30)
    assert len(results) == n
    return results


# --- grants and catalogue ------------------------------------------------------------------------


def test_consume_approval_is_definer_for_app_only(owner: Conn) -> None:
    row = owner.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " (SELECT array_agg(pg_get_userbyid(a.grantee)::text ORDER BY 1)"
        "  FROM aclexplode(p.proacl) a"
        "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
        " FROM pg_proc p WHERE p.oid = 'ledger.consume_approval(uuid)'::regprocedure"
    ).fetchone()
    assert row == (True, "jarvis_owner", ["search_path=pg_catalog, pg_temp"], [APP_ROLE])


def test_app_privileges_on_approval(owner: Conn) -> None:
    table = {
        p
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
        if _one(owner, "SELECT has_table_privilege(%s, 'ledger.approval', %s)", (APP_ROLE, p))
    }
    assert table == {"SELECT", "INSERT"}
    updatable = owner.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'ledger.approval'::regclass"
        " AND attnum > 0 AND NOT attisdropped"
        " AND has_column_privilege(%s, attrelid, attnum, 'UPDATE') ORDER BY 1",
        (APP_ROLE,),
    ).fetchall()
    assert [r[0] for r in updatable] == ["decided_at", "decided_by_node_id", "decision"]


def test_gateway_has_no_access(gateway: Conn) -> None:
    for statement in (
        "SELECT * FROM ledger.approval",
        "SELECT * FROM ops.brain_budget",
        "SELECT * FROM ops.brain_reservation",
        "SELECT * FROM ops.brain_usage",
        "SELECT * FROM ledger.consume_approval(gen_random_uuid())",
        "SELECT ledger.refuse_decision(gen_random_uuid(), gen_random_uuid(), 'expired')",  # R12-21
        "SELECT ledger.refuse_receipt(gen_random_uuid(), gen_random_uuid(), 'late')",  # R12-24
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(statement.encode())


# --- migration 0010 rollback --------------------------------------------------------------------

NEW_TABLES = ("ledger.approval", "ops.brain_budget", "ops.brain_reservation", "ops.brain_usage")


def _fingerprint(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT table_schema, table_name, column_name, data_type, is_nullable"
        " FROM information_schema.columns"
        " WHERE (table_schema, table_name) IN (('ledger', 'approval'), ('ops', 'brain_budget'),"
        " ('ops', 'brain_reservation'), ('ops', 'brain_usage')) ORDER BY 1, 2, 3"
    ).fetchall()


def _objects(conn: Conn) -> list[tuple[Any, ...]]:
    """Indexes, constraints, triggers and functions (with their ACLs) 0010 adds, by name."""
    return conn.execute(
        "SELECT 'index', indexrelid::regclass::text FROM pg_index"
        "  WHERE indrelid::regclass::text = ANY(%(t)s)"
        " UNION ALL SELECT 'constraint', conrelid::regclass::text || '.' || conname"
        "  FROM pg_constraint WHERE conrelid::regclass::text = ANY(%(t)s)"
        " UNION ALL SELECT 'trigger', tgname FROM pg_trigger"
        "  WHERE NOT tgisinternal AND tgrelid::regclass::text = ANY(%(t)s)"
        " UNION ALL SELECT 'function', p.oid::regprocedure::text || ' ' || p.proacl::text"
        "  FROM pg_proc p"
        "  JOIN pg_namespace n ON n.oid = p.pronamespace"
        "  WHERE n.nspname = 'ledger' AND p.proname IN ('consume_approval', 'approval_guard')"
        " ORDER BY 1, 2",
        {"t": list(NEW_TABLES)},
    ).fetchall()


def test_0010_down_and_up_without_rows() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.owner, autocommit=True) as conn:
            before = _fingerprint(conn)
            objects = _objects(conn)
        assert {f"{r[0]}.{r[1]}" for r in before} == set(NEW_TABLES)
        assert ("trigger", "approval_guard") in objects
        assert ("index", "ops.brain_reservation_one_live_per_turn") in objects
        migrate.downgrade(d.owner, "0009")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert _fingerprint(conn) == []
            assert [o for o in _objects(conn) if o[0] == "function"] == []
            labels = conn.execute("SELECT unnest(enum_range(NULL::audit.action))::text").fetchall()
            assert [r[0] for r in labels] == ACTIONS_0009
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id,"
                    " action, outcome, reason) VALUES ('SYSTEM', 'system', 'approval',"
                    " gen_random_uuid(), 'CREATE', 'APPLIED', 'probe')"
                )
            version = _one(conn, "SELECT version_num FROM alembic_version")
            assert version == "0009"
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert _fingerprint(conn) == before
            assert _objects(conn) == objects


def _budget_month(app: Conn) -> None:
    app.execute("INSERT INTO ops.brain_budget (month, cap_inr) VALUES (DATE '2026-11-01', 500000)")


def _reservation(app: Conn) -> uuid.UUID:
    job = _one(app, "INSERT INTO ops.job (kind) VALUES ('brain.probe') RETURNING id")
    return uuid.UUID(
        str(
            _one(
                app,
                "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day, reserved_inr,"
                " lease_until) VALUES (%s, NULL, DATE '2026-11-01', DATE '2026-11-22', 100,"
                " now() + interval '5 minutes') RETURNING id",
                (job,),
            )
        )
    )


@pytest.mark.parametrize("what", ["approval", "refusal", "budget", "reservation", "usage"])
def test_0010_downgrade_refuses_once_rows_exist(what: str) -> None:
    with (
        fresh_database() as d,
        psycopg.connect(d.app, autocommit=True) as app,
        psycopg.connect(d.gateway, autocommit=True) as gateway,
    ):
        if what == "approval":
            _approval(app, (_task(app), _node(app, gateway)))
        elif what == "refusal":  # only an approval audit row, no approval row
            assert _one_row(app, CONSUME, (uuid.uuid4(),)) == (None, "unknown_approval")
            assert _one(app, "SELECT count(*) FROM ledger.approval") == 0
        elif what == "budget":
            _budget_month(app)
        else:
            _budget_month(app)
            reservation = _reservation(app)
            if what == "usage":
                app.execute(
                    "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens,"
                    " output_tokens, cost_inr) VALUES (%s, 'claude-test', 10, 20, 5)",
                    (reservation,),
                )
        with pytest.raises(Exception, match="0010 cannot be undone"):
            migrate.downgrade(d.owner, "0009")
        with psycopg.connect(d.owner, autocommit=True) as owner:
            assert _one(owner, "SELECT version_num FROM alembic_version") == "0010"
            for table in NEW_TABLES:
                assert _one(owner, "SELECT to_regclass(%s)::text", (table,)) == table
            assert (
                _one(owner, "SELECT to_regprocedure('ledger.consume_approval(uuid)')::text")
                == "ledger.consume_approval(uuid)"
            )
