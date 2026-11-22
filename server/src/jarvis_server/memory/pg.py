"""Postgres collaborators of the memory engine: the people directory and the audit sink.

Audit rows are written only by the SECURITY DEFINER function audit.record_memory() (migration
0007), which never accepts memory content: only the reference, action, tags, reason and commit.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg

from jarvis_server.memory.engine import AuditRecord

# Namespace for memory entity IDs in audit.event (UUID v3 of the memory reference).
# Migration 0007 computes the same value in SQL; tests keep the two in step.
MEMORY_NAMESPACE = uuid.UUID("6a1c3f8e-2b4d-4e6f-9a0b-1c2d3e4f5a6b")


def memory_entity_id(ref: str) -> uuid.UUID:
    return uuid.uuid3(MEMORY_NAMESPACE, ref)


class PgDirectory:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    def organization_of(self, person_id: uuid.UUID) -> uuid.UUID | None:
        row = self.conn.execute(
            "SELECT organization_id FROM ledger.person WHERE id = %s", (person_id,)
        ).fetchone()
        if row is None:
            raise LookupError(person_id)
        org: uuid.UUID | None = row[0]
        return org


class PgAudit:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    def record(self, rec: AuditRecord) -> None:
        self.conn.execute(
            "SELECT audit.record_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                rec.actor_kind.value,
                rec.actor_id,
                rec.ref,
                rec.action.value,
                rec.from_tag,
                rec.to_tag,
                rec.outcome.value,
                rec.reason,
                rec.commit,
            ),
        )

    def recorded_commits(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT details->>'commit' FROM audit.event"
            " WHERE entity_type = 'memory' AND details ? 'commit'"
        ).fetchall()
        return {r[0] for r in rows if r[0]}
