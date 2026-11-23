"""The Atlas interface (spec §11, ADR-014): an accepted plan becomes ledger tasks.

Phase 2 has one implementation: one plan becomes one task. It is created DETECTED and moved
through `ledger.transition()` to INTERPRETED and then PROPOSED, each move writing its audit
row; the caller takes it on from there (APPROVAL_PENDING, READY, IN_PROGRESS, then COMPLETED,
FAILED or EXPIRED). Everything happens in the caller's transaction, so a plan is accepted with
whatever the caller writes alongside it (the approval, §9.3 step 3), or not at all.

Dispatch, dependencies, retries and specialists come in Phase 3 behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import psycopg

from jarvis_server import ledger
from jarvis_server.contracts import EntityType, TaskState
from jarvis_server.ledger import Actor


@dataclass(frozen=True)
class ApprovedPlan:
    title: str
    definition_of_done: str
    source_ref: str
    owner_person_id: UUID


class AtlasInterface(Protocol):
    def accept_plan(
        self, conn: psycopg.Connection[Any], plan: ApprovedPlan, *, by: Actor
    ) -> list[UUID]: ...


class Atlas:
    """Phase 2: one plan, one task, left PROPOSED."""

    def accept_plan(
        self, conn: psycopg.Connection[Any], plan: ApprovedPlan, *, by: Actor
    ) -> list[UUID]:
        with conn.transaction():
            task = ledger.create_task(
                conn,
                by,
                title=plan.title,
                definition_of_done=plan.definition_of_done,
                source_ref=plan.source_ref,
                owner_person_id=plan.owner_person_id,
                reason="plan_accepted",
            )
            for state in (TaskState.INTERPRETED, TaskState.PROPOSED):
                ledger.transition(conn, EntityType.TASK, task.id, state, by, "plan_accepted")
        return [task.id]
