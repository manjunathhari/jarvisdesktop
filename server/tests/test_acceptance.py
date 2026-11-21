"""Phase 0 acceptance tests P0-1 … P0-6 (spec §9). P0-7 and P0-8 run on the server."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import ledger, migrate
from jarvis_server.contracts import (
    ActorKind,
    AuditAction,
    AuditOutcome,
    CommitmentState,
    EntityType,
    TaskState,
)
from jarvis_server.ledger import Actor, IllegalTransition

from .conftest import Dsns

TEST = Actor(ActorKind.SYSTEM, "test-runner")
MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")

Conn = psycopg.Connection[Any]


def _new_task(app: Conn, **kw: Any) -> uuid.UUID:
    task = ledger.create_task(
        app,
        TEST,
        title="Test ticket",
        definition_of_done="Walks every state",
        source_ref="test",
        **kw,
    )
    return task.id


# P0-1 ---------------------------------------------------------------------------------------
def test_p0_1_test_ticket_walks_every_main_state_with_one_audit_row_each(app: Conn) -> None:
    corr = uuid.uuid4()
    task_id = _new_task(app, correlation_id=corr)
    path = [
        TaskState.INTERPRETED,
        TaskState.PROPOSED,
        TaskState.APPROVAL_PENDING,
        TaskState.READY,
        TaskState.IN_PROGRESS,
        TaskState.COMPLETED,
    ]
    for state in path:
        ledger.transition(app, EntityType.TASK, task_id, state, TEST, f"walk to {state}", corr)

    assert ledger.state_of(app, EntityType.TASK, task_id) == "COMPLETED"
    trail = ledger.audit_trail(app, EntityType.TASK, task_id)
    assert len(trail) == 7
    assert [e.action for e in trail] == [AuditAction.CREATE] + [AuditAction.TRANSITION] * 6
    assert all(e.outcome is AuditOutcome.APPLIED for e in trail)
    assert [(e.from_state, e.to_state) for e in trail] == list(
        zip([None, "DETECTED", *path[:-1]], ["DETECTED", *path], strict=True)
    )
    assert [e.seq for e in trail] == sorted(e.seq for e in trail)
    assert all(e.correlation_id == corr and e.actor_id == "test-runner" for e in trail)


# P0-2 ---------------------------------------------------------------------------------------
def test_p0_2_illegal_transition_is_refused_state_unchanged_and_audited(app: Conn) -> None:
    task_id = _new_task(app)
    with pytest.raises(IllegalTransition):
        ledger.transition(app, EntityType.TASK, task_id, TaskState.COMPLETED, TEST, "skip ahead")
    assert ledger.state_of(app, EntityType.TASK, task_id) == "DETECTED"
    last = ledger.audit_trail(app, EntityType.TASK, task_id)[-1]
    assert last.outcome is AuditOutcome.REJECTED
    assert (last.from_state, last.to_state) == ("DETECTED", "COMPLETED")
    assert last.reason == "skip ahead"


def test_unknown_record_is_refused_and_audited(app: Conn) -> None:
    ghost = uuid.uuid4()
    with pytest.raises(IllegalTransition):
        ledger.transition(app, EntityType.TASK, ghost, TaskState.INTERPRETED, TEST, "ghost")
    (event,) = ledger.audit_trail(app, EntityType.TASK, ghost)
    assert event.outcome is AuditOutcome.REJECTED and event.reason == "not_found: ghost"


def test_unknown_state_name_is_refused(app: Conn) -> None:
    task_id = _new_task(app)
    with pytest.raises(IllegalTransition):
        ledger.transition(app, EntityType.TASK, task_id, "DONE", TEST, "typo")
    assert ledger.state_of(app, EntityType.TASK, task_id) == "DETECTED"


# P0-3 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.event SET reason = 'rewritten'",
        "DELETE FROM audit.event",
        "TRUNCATE audit.event",
        "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id, action, outcome,"
        " reason) VALUES ('SYSTEM', 'forger', 'task', gen_random_uuid(), 'CREATE', 'APPLIED', 'x')",
    ],
)
def test_p0_3_app_role_cannot_change_or_forge_audit_rows(app: Conn, statement: str) -> None:
    _new_task(app)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.event SET reason = 'rewritten'",
        "DELETE FROM audit.event",
        "TRUNCATE audit.event",
    ],
)
def test_p0_3_even_the_owner_cannot_change_audit_rows(
    app: Conn, owner: Conn, statement: str
) -> None:
    _new_task(app)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        owner.execute(statement)


def test_app_role_cannot_set_state_directly(app: Conn) -> None:
    task_id = _new_task(app)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("UPDATE ledger.task SET state = 'COMPLETED' WHERE id = %s", (task_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("DELETE FROM ledger.task WHERE id = %s", (task_id,))
    # Other columns are editable.
    app.execute("UPDATE ledger.task SET title = 'Renamed' WHERE id = %s", (task_id,))


def test_app_role_cannot_set_commitment_state_directly(app: Conn) -> None:
    c = ledger.create_commitment(
        app, TEST, person_id=MANJ, description="Protected state", source_ref="test"
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("UPDATE ledger.commitment SET state = 'FULFILLED' WHERE id = %s", (c.id,))


def test_no_ledger_state_column_is_updatable_by_app(app: Conn) -> None:
    rows = app.execute(
        "SELECT table_name FROM information_schema.column_privileges"
        " WHERE grantee = 'jarvis_app' AND table_schema = 'ledger'"
        " AND column_name IN ('state', 'id', 'created_at', 'updated_at')"
        " AND privilege_type = 'UPDATE'"
    ).fetchall()
    assert rows == []


def test_app_role_cannot_call_internal_audit_writer(app: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "SELECT audit.append('SYSTEM','forger','task',gen_random_uuid(),'CREATE',"
            "NULL,NULL,'APPLIED','x',NULL)"
        )


def test_records_must_start_in_initial_state(app: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), app.transaction():
        ledger._declare(app, TEST, "sneaky", None)
        app.execute(
            "INSERT INTO ledger.task (title, definition_of_done, source_ref, state)"
            " VALUES ('x', 'y', 'test', 'COMPLETED')"
        )


def test_creation_without_declared_actor_is_refused(app: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "INSERT INTO ledger.task (title, definition_of_done, source_ref) VALUES ('x','y','t')"
        )


def test_timestamps_are_set_by_the_database(app: Conn) -> None:
    task_id = _new_task(app)
    app.execute("UPDATE ledger.task SET title = 'x', source_ref = 'test' WHERE id = %s", (task_id,))
    row = app.execute(
        "SELECT created_at <= updated_at, created_at > now() - interval '1 minute'"
        " FROM ledger.task WHERE id = %s",
        (task_id,),
    ).fetchone()
    assert row == (True, True)


# P0-4 ---------------------------------------------------------------------------------------
def test_p0_4_commitment_lifecycle_with_audit(app: Conn) -> None:
    c = ledger.create_commitment(
        app, TEST, person_id=MANJ, description="Be at the airport by 07:00", source_ref="test"
    )
    assert c.state is CommitmentState.EXPECTED
    for state in (CommitmentState.AT_RISK, CommitmentState.SECURED, CommitmentState.FULFILLED):
        ledger.transition(app, EntityType.COMMITMENT, c.id, state, TEST, f"to {state}")
    trail = ledger.audit_trail(app, EntityType.COMMITMENT, c.id)
    assert [e.to_state for e in trail] == ["EXPECTED", "AT_RISK", "SECURED", "FULFILLED"]
    assert all(e.outcome is AuditOutcome.APPLIED for e in trail)
    with pytest.raises(IllegalTransition):
        ledger.transition(app, EntityType.COMMITMENT, c.id, CommitmentState.MISSED, TEST, "late")


# P0-5 ---------------------------------------------------------------------------------------
def test_p0_5_expired_context_is_excluded_from_current_context(app: Conn) -> None:
    now = datetime.now(UTC)
    rows = [
        ("expired", now - timedelta(days=3), now - timedelta(days=1)),
        ("live", now - timedelta(hours=1), now + timedelta(days=2)),
        ("future", now + timedelta(days=1), now + timedelta(days=2)),
    ]
    tag = uuid.uuid4().hex
    for summary, start, end in rows:
        app.execute(
            "INSERT INTO ledger.context (subject_person_id, kind, summary, valid_from, expires_at,"
            " source_ref) VALUES (%s, 'availability', %s, %s, %s, %s)",
            (MANJ, summary, start, end, tag),
        )
    current = app.execute(
        "SELECT summary FROM ledger.current_context WHERE source_ref = %s", (tag,)
    ).fetchall()
    assert [r[0] for r in current] == ["live"]


# P0-6 ---------------------------------------------------------------------------------------
def test_p0_6_migrations_go_down_and_up_cleanly(dsns: Dsns) -> None:
    def schema_fingerprint() -> list[tuple[Any, ...]]:
        with psycopg.connect(dsns.owner) as conn:
            return conn.execute(
                "SELECT table_schema, table_name, column_name, data_type FROM"
                " information_schema.columns WHERE table_schema IN ('ledger','audit','ops')"
                " ORDER BY 1, 2, 3"
            ).fetchall()

    before = schema_fingerprint()
    migrate.downgrade(dsns.owner, "base")
    assert schema_fingerprint() == []
    migrate.upgrade(dsns.owner, "head")
    assert schema_fingerprint() == before
