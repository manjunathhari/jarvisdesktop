"""On-server check of P0-1 … P0-5 against the live database, as the app role.

Run with `jarvis-core smoke`. Leaves a clearly labelled test ticket and commitment behind
(source_ref 'smoke-check'), which is fine: the ledger is append-mostly and P0-7 reuses them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from jarvis_server import ledger
from jarvis_server.contracts import ActorKind, AuditOutcome, CommitmentState, EntityType, TaskState
from jarvis_server.ledger import Actor, IllegalTransition

ACTOR = Actor(ActorKind.SYSTEM, "smoke-check")
MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
TASK_PATH = [
    TaskState.INTERPRETED,
    TaskState.PROPOSED,
    TaskState.APPROVAL_PENDING,
    TaskState.READY,
    TaskState.IN_PROGRESS,
    TaskState.COMPLETED,
]


def run(conn: psycopg.Connection[Any]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    corr = uuid.uuid4()

    task = ledger.create_task(
        conn,
        ACTOR,
        title="P0-1 test ticket",
        definition_of_done="Walks every main state",
        source_ref="smoke-check",
        correlation_id=corr,
    )
    for s in TASK_PATH:
        ledger.transition(conn, EntityType.TASK, task.id, s, ACTOR, f"smoke walk to {s}", corr)
    trail = ledger.audit_trail(conn, EntityType.TASK, task.id)
    ok = len(trail) == 7 and all(e.outcome is AuditOutcome.APPLIED for e in trail)
    results.append(("P0-1 ticket walk", ok, f"task {task.id}, {len(trail)} audit rows"))

    other = ledger.create_task(
        conn,
        ACTOR,
        title="P0-2 illegal move",
        definition_of_done="Stays DETECTED",
        source_ref="smoke-check",
    )
    try:
        ledger.transition(conn, EntityType.TASK, other.id, TaskState.COMPLETED, ACTOR, "skip")
        results.append(("P0-2 illegal transition", False, "was allowed"))
    except IllegalTransition:
        last = ledger.audit_trail(conn, EntityType.TASK, other.id)[-1]
        state = ledger.state_of(conn, EntityType.TASK, other.id)
        ok = state == "DETECTED" and last.outcome is AuditOutcome.REJECTED
        results.append(("P0-2 illegal transition", ok, f"state {state}, refusal audited"))

    try:
        with conn.transaction():
            conn.execute("DELETE FROM audit.event WHERE entity_id = %s", (task.id,))
        results.append(("P0-3 audit append-only", False, "delete succeeded"))
    except psycopg.errors.InsufficientPrivilege:
        results.append(("P0-3 audit append-only", True, "app role refused"))

    c = ledger.create_commitment(
        conn, ACTOR, person_id=MANJ, description="P0-4 test commitment", source_ref="smoke-check"
    )
    for cs in (CommitmentState.AT_RISK, CommitmentState.SECURED, CommitmentState.FULFILLED):
        ledger.transition(conn, EntityType.COMMITMENT, c.id, cs, ACTOR, f"smoke to {cs}")
    n = len(ledger.audit_trail(conn, EntityType.COMMITMENT, c.id))
    results.append(("P0-4 commitment lifecycle", n == 4, f"{n} audit rows"))

    now = datetime.now(UTC)
    tag = f"smoke-check-{corr.hex[:8]}"
    for summary, start, end in (
        ("expired", now - timedelta(days=2), now - timedelta(days=1)),
        ("live", now - timedelta(hours=1), now + timedelta(hours=1)),
    ):
        conn.execute(
            "INSERT INTO ledger.context (subject_person_id, kind, summary, valid_from, expires_at,"
            " source_ref) VALUES (%s, 'test', %s, %s, %s, %s)",
            (MANJ, summary, start, end, tag),
        )
    rows = conn.execute(
        "SELECT summary FROM ledger.current_context WHERE source_ref = %s", (tag,)
    ).fetchall()
    results.append(("P0-5 context expiry", [r[0] for r in rows] == ["live"], f"{rows}"))
    return results
