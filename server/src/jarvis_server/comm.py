"""Core-side writers for events and the outbox (migration 0009; spec §5.3, §8.2).

`comm.event` is append-only and holds codes and IDs only, never content. An outbox item starts
PENDING; after that its state changes only through the database functions (the gateway's
record_sent / record_received / acknowledge, and core's comm.supersede / comm.expire), which
lock the row, move it forward only and audit each call.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

# Kinds and subject types are codes; subject IDs and detail values are codes or IDs. The same
# shapes are CHECK constraints on comm.event.
_CODE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


class Priority(StrEnum):
    URGENT = "URGENT"
    TODAY = "TODAY"
    DIGEST = "DIGEST"
    SILENT = "SILENT"


class OutboxState(StrEnum):
    PENDING = "PENDING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = frozenset(
    {OutboxState.ACKNOWLEDGED.value, OutboxState.SUPERSEDED.value, OutboxState.EXPIRED.value}
)


def _check_code(what: str, value: str) -> None:
    if not _CODE.fullmatch(value):
        raise ValueError(f"{what} must be a code")


def record_event(
    conn: psycopg.Connection[Any],
    kind: str,
    *,
    subject_type: str,
    subject_id: str,
    person_id: UUID | None,
    details: dict[str, str],
) -> UUID:
    """Append one event (codes and IDs only) and return its id."""
    _check_code("kind", kind)
    _check_code("subject_type", subject_type)
    if not _ID.fullmatch(subject_id):
        raise ValueError("subject_id must be a code or an ID")
    for key, value in details.items():
        if not _CODE.fullmatch(key) or not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("event details hold codes and IDs only")
    row = conn.execute(
        "INSERT INTO comm.event (kind, subject_type, subject_id, person_id, details)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (kind, subject_type, subject_id, person_id, Jsonb(details)),
    ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def create_outbox(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    event_id: UUID,
    priority: Priority,
    kind: str,
    body: dict[str, Any],
    group_key: str,
    expires_at: datetime | None,
    supersedes_id: UUID | None = None,
    authz: dict[str, Any] | None = None,
    target_node_id: UUID | None = None,
) -> UUID:
    """Create a PENDING outbox item; supersede `supersedes_id` in the same transaction.

    `target_node_id` addresses the item to that one node of the person (R12-23); None, to every
    node of the person. The database refuses a target that is not the person's node.

    NOTIFYs `jarvis_outbox` with the new id (and the supersede with the old one), delivered
    when the transaction commits. The supersede goes through comm.supersede_for_create, which
    checks the target under its own FOR UPDATE lock (R11-4): a live target is superseded with
    its audit row ('applied'); one that has already ended (acknowledged, expired or
    superseded), even concurrently, is a **non-audited no-op** ('skipped', R11-2). The old item
    is then untouched and the new item is still created.
    """
    _check_code("kind", kind)
    with conn.transaction():
        if supersedes_id is not None:
            row = conn.execute(
                "SELECT person_id FROM comm.outbox WHERE id = %s", (supersedes_id,)
            ).fetchone()
            if row is None or row[0] != person_id:
                raise ValueError("an item can only supersede an item of the same person")
        row = conn.execute(
            "INSERT INTO comm.outbox (person_id, event_id, priority, kind, body, authz,"
            " target_node_id, group_key, supersedes_id, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                person_id,
                event_id,
                Priority(priority).value,
                kind,
                Jsonb(body),
                None if authz is None else Jsonb(authz),
                target_node_id,
                group_key,
                supersedes_id,
                expires_at,
            ),
        ).fetchone()
        assert row is not None
        outbox_id = UUID(str(row[0]))
        if supersedes_id is not None:
            outcome = conn.execute(
                "SELECT comm.supersede_for_create(%s)", (supersedes_id,)
            ).fetchone()
            if outcome is None or outcome[0] not in ("applied", "skipped"):
                raise RuntimeError(f"comm.supersede_for_create returned {outcome!r}")
        conn.execute("SELECT pg_notify('jarvis_outbox', %s)", (str(outbox_id),))
    return outbox_id
