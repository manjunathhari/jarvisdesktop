"""Ledger access: create records, change state through the one door, read the audit trail.

Every write declares an Actor. State never changes except through ledger.transition() in the
database (ADR-007); this module only wraps it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from jarvis_server.contracts import (
    ActorKind,
    AuditEvent,
    Commitment,
    CommitmentState,
    EntityType,
    Task,
    TaskState,
    Visibility,
)


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str


class IllegalTransition(Exception):
    """The move is not in ledger.allowed_transition (or the record does not exist).

    The refusal has already been written to the audit trail when this is raised.
    """

    def __init__(self, entity: EntityType, entity_id: UUID, to_state: str) -> None:
        super().__init__(f"{entity.value} {entity_id}: transition to {to_state} refused")
        self.entity = entity
        self.entity_id = entity_id
        self.to_state = to_state


def _declare(
    conn: psycopg.Connection[Any], actor: Actor, reason: str, correlation_id: UUID | None
) -> None:
    conn.execute(
        "SELECT set_config('jarvis.actor_kind', %s, true), set_config('jarvis.actor_id', %s, true),"
        " set_config('jarvis.reason', %s, true), set_config('jarvis.correlation_id', %s, true)",
        (actor.kind.value, actor.id, reason, str(correlation_id) if correlation_id else ""),
    )


def create_task(
    conn: psycopg.Connection[Any],
    actor: Actor,
    *,
    title: str,
    definition_of_done: str,
    source_ref: str,
    owner_person_id: UUID | None = None,
    due_at: datetime | None = None,
    visibility: Visibility = Visibility.PRIVATE,
    reason: str = "created",
    correlation_id: UUID | None = None,
) -> Task:
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        _declare(conn, actor, reason, correlation_id)
        cur.execute(
            "INSERT INTO ledger.task (title, definition_of_done, owner_person_id, due_at,"
            " visibility, source_ref) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id, title, definition_of_done, owner_person_id, state::text, due_at,"
            " visibility::text, source_ref, created_at, updated_at",
            (title, definition_of_done, owner_person_id, due_at, visibility.value, source_ref),
        )
        row = cur.fetchone()
    assert row is not None
    return Task.model_validate(row)


def create_commitment(
    conn: psycopg.Connection[Any],
    actor: Actor,
    *,
    person_id: UUID,
    description: str,
    source_ref: str,
    due_at: datetime | None = None,
    event_id: UUID | None = None,
    visibility: Visibility = Visibility.PRIVATE,
    reason: str = "created",
    correlation_id: UUID | None = None,
) -> Commitment:
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        _declare(conn, actor, reason, correlation_id)
        cur.execute(
            "INSERT INTO ledger.commitment (person_id, description, due_at, event_id, visibility,"
            " source_ref) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id, person_id, description, due_at, event_id, state::text,"
            " visibility::text, source_ref, created_at, updated_at",
            (person_id, description, due_at, event_id, visibility.value, source_ref),
        )
        row = cur.fetchone()
    assert row is not None
    return Commitment.model_validate(row)


def transition(
    conn: psycopg.Connection[Any],
    entity: EntityType,
    entity_id: UUID,
    to_state: TaskState | CommitmentState | str,
    actor: Actor,
    reason: str,
    correlation_id: UUID | None = None,
) -> None:
    """Move a record to a new state, or raise IllegalTransition (the refusal is audited)."""
    target = str(to_state)
    with conn.transaction():
        row = conn.execute(
            "SELECT ledger.transition(%s, %s, %s, %s, %s, %s, %s)",
            (entity.value, entity_id, target, actor.kind.value, actor.id, reason, correlation_id),
        ).fetchone()
    assert row is not None
    if row[0] != "APPLIED":
        raise IllegalTransition(entity, entity_id, target)


def state_of(conn: psycopg.Connection[Any], entity: EntityType, entity_id: UUID) -> str | None:
    table = {EntityType.TASK: "ledger.task", EntityType.COMMITMENT: "ledger.commitment"}[entity]
    row = conn.execute(f"SELECT state::text FROM {table} WHERE id = %s", (entity_id,)).fetchone()  # noqa: S608
    return None if row is None else str(row[0])


def audit_trail(
    conn: psycopg.Connection[Any], entity: EntityType, entity_id: UUID
) -> list[AuditEvent]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT seq, id, occurred_at, actor_kind::text, actor_id, entity_type, entity_id,"
            " action::text, from_state, to_state, outcome::text, reason, correlation_id, details"
            " FROM audit.event WHERE entity_type = %s AND entity_id = %s ORDER BY seq",
            (entity.value, entity_id),
        )
        return [AuditEvent.model_validate(r) for r in cur.fetchall()]
