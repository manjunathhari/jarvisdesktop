"""Python mirrors of the JSON Schemas in /contracts.

The schemas are the source of truth; tests/test_contracts.py keeps these models in step with them.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Visibility(StrEnum):
    PRIVATE = "PRIVATE"
    SELECTED_PERSON = "SELECTED_PERSON"
    SELECTED_GROUP = "SELECTED_GROUP"
    FAMILY_SHARED = "FAMILY_SHARED"
    ORGANIZATION_SHARED = "ORGANIZATION_SHARED"


class TaskState(StrEnum):
    DETECTED = "DETECTED"
    INTERPRETED = "INTERPRETED"
    PROPOSED = "PROPOSED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class CommitmentState(StrEnum):
    EXPECTED = "EXPECTED"
    AT_RISK = "AT_RISK"
    SECURED = "SECURED"
    FULFILLED = "FULFILLED"
    MISSED = "MISSED"


class ActorKind(StrEnum):
    PERSON = "PERSON"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class EntityType(StrEnum):
    TASK = "task"
    COMMITMENT = "commitment"
    MEMORY = "memory"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    TRANSITION = "TRANSITION"
    UPDATE = "UPDATE"
    CONFIRM = "CONFIRM"
    ARCHIVE = "ARCHIVE"


class AuditOutcome(StrEnum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class ApprovalTier(StrEnum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE = "REVERSIBLE"
    IRREVERSIBLE = "IRREVERSIBLE"


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    EPISODIC = "episodic"
    OPERATIONAL = "operational"
    LEARNED = "learned"


class MemoryTag(StrEnum):
    STATED = "STATED"
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    HISTORICAL = "HISTORICAL"
    EXPIRED = "EXPIRED"


def _text(max_length: int) -> Any:
    return Field(min_length=1, max_length=max_length)


Text200 = Annotated[str, _text(200)]
Text100 = Annotated[str, _text(100)]
Text2000 = Annotated[str, _text(2000)]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Task(_Contract):
    id: UUID
    title: Text200
    definition_of_done: Text2000
    owner_person_id: UUID | None = None
    state: TaskState
    due_at: datetime | None = None
    visibility: Visibility
    source_ref: Text200
    created_at: datetime
    updated_at: datetime


class Commitment(_Contract):
    id: UUID
    person_id: UUID
    description: Text2000
    due_at: datetime | None = None
    event_id: UUID | None = None
    state: CommitmentState
    visibility: Visibility
    source_ref: Text200
    created_at: datetime
    updated_at: datetime


class AuditEvent(_Contract):
    seq: Annotated[int, Field(ge=1)]
    id: UUID
    occurred_at: datetime
    actor_kind: ActorKind
    actor_id: Text200
    entity_type: EntityType
    entity_id: UUID
    action: AuditAction
    from_state: str | None = None
    to_state: str | None = None
    outcome: AuditOutcome
    reason: Text2000
    correlation_id: UUID | None = None
    details: dict[str, Any]


SLUG = r"^[a-z][a-z0-9-]{1,30}$"
Slug = Annotated[str, Field(pattern=SLUG)]


class MemoryEntry(_Contract):
    """One memory entry (Architecture v2 §11, Phase 1 spec §4.3)."""

    memory_id: Annotated[str, Field(pattern=r"^m-[0-9]{4,}$")]
    subject: Slug
    category: MemoryCategory
    domain: Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")] | None = None
    tag: MemoryTag
    content: Annotated[str, Field(min_length=1, max_length=500, pattern=r"^[^\n\r]+$")]
    source: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]
    recorded_on: date
    by: Annotated[str, Field(pattern=r"^([a-z][a-z0-9-]{1,30}|agent:[a-z][a-z0-9-]{0,39}|system)$")]
    confirmed_by: Slug | None = None
    confirmed_on: date | None = None
    visibility: Visibility
    shared_with: tuple[Slug, ...] = ()
    topics: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[a-z0-9-]{1,30}$")], ...], Field(max_length=8)
    ] = ()
    happened_on: date | None = None

    @property
    def drives_action(self) -> bool:
        """Only STATED and CONFIRMED entries may drive an action (v2 §11.3)."""
        return self.tag in (MemoryTag.STATED, MemoryTag.CONFIRMED)


class Receipt(_Contract):
    """Stub (Phase 2/3)."""

    id: UUID
    task_id: UUID
    action_id: UUID
    kind: Text100
    evidence: dict[str, Any]
    verified_at: datetime | None = None
    verified_by: str | None = None


class Approval(_Contract):
    """Stub (Phase 2/3)."""

    id: UUID
    task_id: UUID
    requested_from_person_id: UUID
    tier: ApprovalTier
    expires_at: datetime
    decision: ApprovalDecision
    decided_at: datetime | None = None


class NodeEnvelope(_Contract):
    """Stub (Phase 2): wrapper for node <-> gateway messages."""

    version: Literal[1]
    message_id: UUID
    node_id: Text100
    session_id: UUID | None = None
    sent_at: datetime
    kind: Text100
    payload: dict[str, Any]


MODELS: dict[str, type[_Contract]] = {
    "task": Task,
    "commitment": Commitment,
    "audit-event": AuditEvent,
    "memory-entry": MemoryEntry,
    "receipt": Receipt,
    "approval": Approval,
    "node-envelope": NodeEnvelope,
}
