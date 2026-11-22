"""Python mirrors of the JSON Schemas in /contracts.

The schemas are the source of truth; tests/test_contracts.py keeps these models in step with them.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


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


# ---------------------------------------------------------------- signing primitives (spec §7.1)

Sig = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{86}$")]
"""Ed25519 signature, base64url without padding."""
PublicKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
"""Raw 32-byte Ed25519 public key, base64url without padding."""
KeyId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
Code = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
Seq = Annotated[int, Field(ge=1)]


class AuthzKind(StrEnum):
    NODE_ACTION = "node.action"
    APPROVAL_CARD = "approval.card"
    KEYSET = "keyset"


class AuthzObject(_Contract):
    """The core-signed authorisation object. Every field is always present (null if unused)."""

    v: Literal[1]
    kind: AuthzKind
    action_kind: Code
    args: dict[str, Any]
    target_node_id: UUID | None
    person_id: UUID | None
    approval_id: UUID | None
    execution_key: UUID | None
    reply_id: UUID | None
    summary: Annotated[str, Field(min_length=1, max_length=500)] | None
    issued_at: datetime
    valid_until: datetime


class Authz(_Contract):
    """``{object, key_id, sig}``: ``sig`` is purpose ``core-authz`` over JCS(object).

    Verify signatures over the raw JSON object, never over this model re-serialised.
    """

    object: AuthzObject
    key_id: KeyId
    sig: Sig


class Approval(_Contract):
    """A request for permission with an expiry (spec §5.5)."""

    id: UUID
    task_id: UUID
    requested_from_person_id: UUID
    requesting_node_id: UUID
    tier: ApprovalTier
    action_kind: Code
    action_args: dict[str, Any]
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    card_authz: Authz
    expires_at: datetime
    decision: ApprovalDecision
    decided_at: datetime | None = None
    decided_by_node_id: UUID | None = None
    consumed_at: datetime | None = None
    execution_key: UUID | None = None


class MessageKind(StrEnum):
    ENROL = "enrol"
    HELLO = "hello"
    WELCOME = "welcome"
    UTTERANCE = "utterance"
    REPLY_CHUNK = "reply.chunk"
    OUTBOX_ITEM = "outbox.item"
    OUTBOX_CLEARED = "outbox.cleared"
    RECEIVED = "received"
    ACK = "ack"
    APPROVAL_DECISION = "approval.decision"
    NODE_ACTION = "node.action"
    ACTION_RECEIPT = "action.receipt"
    KEYSET = "keyset"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


class NodeEnvelope(_Contract):
    """Wrapper for node <-> gateway messages; ``sig`` covers every other field (spec §7.1)."""

    version: Literal[1]
    message_id: UUID
    node_id: Text100
    session_id: UUID | None = None
    seq: Seq
    sent_at: datetime
    kind: MessageKind
    payload: dict[str, Any]
    sig: Sig


# ---------------------------------------------------------------- message payloads

Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")]


class Enrol(_Contract):
    token: Token
    public_key: PublicKey


class ResumePosition(_Contract):
    reply_id: UUID
    last_seq: Seq


class Hello(_Contract):
    nonce: Token
    resume: tuple[ResumePosition, ...]


class SummaryPriority(StrEnum):
    URGENT = "URGENT"
    TODAY = "TODAY"
    DIGEST = "DIGEST"


class SummaryGroup(_Contract):
    priority: SummaryPriority
    group_key: Text200 | None
    outbox_ids: tuple[UUID, ...]
    line: Annotated[str, Field(min_length=1, max_length=500)] | None


class Welcome(_Contract):
    session_id: UUID | None
    summary: tuple[SummaryGroup, ...]


class Utterance(_Contract):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    screen_summary: Annotated[str, Field(max_length=2000)] | None


class ChunkType(StrEnum):
    SPEECH = "SPEECH"
    ACTION = "ACTION"


class ReplyChunk(_Contract):
    """SPEECH carries ``text`` (authz null); ACTION carries ``authz`` (text null)."""

    reply_id: UUID
    seq: Seq
    type: ChunkType
    text: Annotated[str, Field(min_length=1, max_length=4000)] | None
    authz: Authz | None
    final: bool

    @model_validator(mode="after")
    def _text_or_authz(self) -> ReplyChunk:
        if self.type is ChunkType.SPEECH and (self.text is None or self.authz is not None):
            raise ValueError("a SPEECH chunk has text and no authz")
        if self.type is ChunkType.ACTION and (self.authz is None or self.text is not None):
            raise ValueError("an ACTION chunk has authz and no text")
        return self


class OutboxPriority(StrEnum):
    URGENT = "URGENT"
    TODAY = "TODAY"
    DIGEST = "DIGEST"
    SILENT = "SILENT"


class OutboxItem(_Contract):
    outbox_id: UUID
    priority: OutboxPriority
    kind: Code
    body: Annotated[str, Field(min_length=1, max_length=4000)]
    expires_at: datetime | None
    authz: Authz | None


class ClearedReason(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class OutboxCleared(_Contract):
    outbox_id: UUID
    reason: ClearedReason


class OutboxRef(_Contract):
    outbox_id: UUID


class ReplyPosition(_Contract):
    """Every chunk of ``reply_id`` up to and including ``seq``."""

    reply_id: UUID
    seq: Seq


class Received(RootModel[OutboxRef | ReplyPosition]):
    pass


class Ack(RootModel[OutboxRef | ReplyPosition]):
    pass


class Decision(StrEnum):
    APPROVED = "APPROVED"
    DENIED = "DENIED"


class ApprovalDecisionMessage(_Contract):
    approval_id: UUID
    decision: Decision
    card_sig: Sig


class NodeAction(_Contract):
    authz: Authz


ResultCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z_]{0,39}$")]


class ApprovedActionReceipt(_Contract):
    execution_key: UUID
    result: ResultCode
    evidence: dict[str, Any]


class LiveActionReceipt(_Contract):
    reply_id: UUID
    seq: Seq
    result: ResultCode
    evidence: dict[str, Any]


class ActionReceipt(RootModel[ApprovedActionReceipt | LiveActionReceipt]):
    pass


class KeyRole(StrEnum):
    CORE = "core"
    GATEWAY = "gateway"


class Keyset(_Contract):
    role: KeyRole
    key_id: KeyId
    public_key: PublicKey
    authz: Authz


class Ping(_Contract):
    pass


class Pong(_Contract):
    pass


class Error(_Contract):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    detail: Annotated[str, Field(max_length=500)]


MODELS: dict[str, type[_Contract]] = {
    "task": Task,
    "commitment": Commitment,
    "audit-event": AuditEvent,
    "memory-entry": MemoryEntry,
    "receipt": Receipt,
    "approval": Approval,
    "node-envelope": NodeEnvelope,
}


MESSAGE_MODELS: dict[str, type[BaseModel]] = {
    "enrol": Enrol,
    "hello": Hello,
    "welcome": Welcome,
    "utterance": Utterance,
    "reply.chunk": ReplyChunk,
    "outbox.item": OutboxItem,
    "outbox.cleared": OutboxCleared,
    "received": Received,
    "ack": Ack,
    "approval.decision": ApprovalDecisionMessage,
    "node.action": NodeAction,
    "action.receipt": ActionReceipt,
    "keyset": Keyset,
    "ping": Ping,
    "pong": Pong,
    "error": Error,
    "authz": Authz,
}
