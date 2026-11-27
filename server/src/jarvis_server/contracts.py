"""Python mirrors of the JSON Schemas in /contracts.

The schemas are the source of truth; tests/test_contracts.py keeps these models in step with them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    field_validator,
    model_validator,
)


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
    NODE = "node"
    INBOUND = "inbound"
    OUTBOX = "outbox"
    STREAM = "stream"
    APPROVAL = "approval"
    RETENTION = "retention"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    TRANSITION = "TRANSITION"
    UPDATE = "UPDATE"
    CONFIRM = "CONFIRM"
    ARCHIVE = "ARCHIVE"
    ENROL_CREATE = "ENROL_CREATE"
    ENROL = "ENROL"
    REVOKE = "REVOKE"
    CONNECT_REFUSED = "CONNECT_REFUSED"
    CONNECT = "CONNECT"
    DISCONNECT = "DISCONNECT"
    INBOUND = "INBOUND"
    SEND = "SEND"
    RECEIVE = "RECEIVE"
    ACK = "ACK"
    SUPERSEDE = "SUPERSEDE"
    EXPIRE = "EXPIRE"
    CHUNK_RECEIVE = "CHUNK_RECEIVE"
    CONSUME = "CONSUME"
    DECIDE = "DECIDE"
    RECEIPT = "RECEIPT"
    SET_VISIBILITY = "SET_VISIBILITY"
    RETENTION = "RETENTION"


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
    issued_at: AwareDatetime
    valid_until: AwareDatetime


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


NIL_UUID = UUID(int=0)


class MessageKind(StrEnum):
    CHALLENGE = "challenge"
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
    BOARD_STATE = "board.state"
    KEYSET = "keyset"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


NIL_NODE_ID_KINDS = frozenset({MessageKind.CHALLENGE, MessageKind.ENROL})


class NodeEnvelope(_Contract):
    """Wrapper for node <-> gateway messages; ``sig`` covers every other field (spec §7.1)."""

    version: Literal[1]
    message_id: UUID
    node_id: UUID
    session_id: UUID | None = None
    seq: Seq
    sent_at: AwareDatetime
    kind: MessageKind
    payload: dict[str, Any]
    sig: Sig

    @model_validator(mode="after")
    def _nil_node_id_only_before_the_node_is_known(self) -> NodeEnvelope:
        if self.node_id == NIL_UUID and self.kind not in NIL_NODE_ID_KINDS:
            raise ValueError("the nil node_id is allowed only on challenge and enrol")
        return self


def _absent_not_null(data: Any, *fields: str) -> Any:
    """Optional fields may be left out, but never sent as null (the schemas say the same)."""
    if isinstance(data, dict):
        for field in fields:
            if field in data and data[field] is None:
                raise ValueError(f"{field} may be absent but not null")
    return data


# ---------------------------------------------------------------- message payloads

Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")]
Nonce = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
"""32 random bytes, base64url without padding."""


class Challenge(_Contract):
    """The gateway's first message on every connection (spec §6.1 step 2, R6-1)."""

    nonce: Nonce
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    key_id: KeyId
    public_key: PublicKey


class Enrol(_Contract):
    token: Token
    public_key: PublicKey
    nonce: Nonce


class ResumePosition(_Contract):
    reply_id: UUID
    # Any integer; the gateway validates it (R6-5). Strict: a JSON number spelled with a
    # fraction or an exponent (`2.0`, `1e18`) is refused whatever its value (final review M-1).
    last_seq: StrictInt


class Hello(_Contract):
    nonce: Nonce
    resume: tuple[ResumePosition, ...]
    clock_skew_ms: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_null(cls, data: Any) -> Any:
        return _absent_not_null(data, "clock_skew_ms")


class SummaryPriority(StrEnum):
    URGENT = "URGENT"
    TODAY = "TODAY"
    DIGEST = "DIGEST"


class SummaryGroup(_Contract):
    priority: SummaryPriority
    group_key: Text200 | None
    outbox_ids: tuple[UUID, ...]
    line: Annotated[str, Field(min_length=1, max_length=500)] | None


class EnrolmentBlock(_Contract):
    """Sent in ``welcome`` after an enrolment only (spec §6.1 step 7, R6-6)."""

    node_id: UUID
    core_key_id: KeyId
    core_public_key: PublicKey


class Welcome(_Contract):
    session_id: UUID | None
    summary: tuple[SummaryGroup, ...]
    enrolment: EnrolmentBlock | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_null(cls, data: Any) -> Any:
        return _absent_not_null(data, "enrolment")


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
    in_reply_to: UUID | None  # unsigned: the utterance this reply answers (R12-37)

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
    expires_at: AwareDatetime | None
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
    seq: Seq  # unsigned: the chunk's seq, for the live receipt (R12-36)
    in_reply_to: UUID | None  # unsigned: the utterance the reply answers (R12-37)


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


# --- the desktop's test-folder actions (spec §9.3, §9.4; R12-25) --------------------------------

DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)  # fmt: skip


def _plain_file_name(name: str) -> str:
    if name.split(".", 1)[0].upper() in DEVICE_NAMES:
        raise ValueError("a device name is not a file name")
    return name


FileName = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"), AfterValidator(_plain_file_name)
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
WindowsFileId = Annotated[str, Field(pattern=r"^[0-9a-f]{16}:[0-9a-f]{32}$")]


class FolderFile(_Contract):
    """One file of the test folder: `pc.delete_test_file`'s args, and one listing entry."""

    name: FileName
    size: Annotated[int, Field(ge=0, strict=True)]
    sha256: Sha256Hex
    file_id: WindowsFileId


class ListTestFolderArgs(_Contract):
    """`pc.list_test_folder` takes no arguments."""


class ListingEvidence(_Contract):
    files: Annotated[list[FolderFile], Field(max_length=1000)]


class ListingReceipt(_Contract):
    reply_id: UUID
    seq: Seq
    result: Literal["OK", "REFUSED", "ERROR"]
    evidence: ListingEvidence


class DeleteEvidence(_Contract):
    file_id: WindowsFileId | None
    before_sha256: Sha256Hex | None
    recycled_at: AwareDatetime | None
    check: Code | None = None  # omitted when there is none; never null, as in the schema

    @field_validator("check", mode="before")
    @classmethod
    def _check_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("`check` is omitted when there is none, never null")
        return value


class DeleteReceipt(_Contract):
    execution_key: UUID
    result: Literal["RECYCLED", "MISMATCH", "NOT_FOUND", "REFUSED", "ERROR"]
    evidence: DeleteEvidence


# --- the Board (slice 1: plan `.10x/plans/2026-11-26-board-slice-1.md`) -------------------------

BOARD_TITLE_MAX = 120  # Unicode code points
# Of UTF-8. With control characters refused, content at most doubles when it is serialised
# (`"`, `\\`, line breaks), so a panel is under 62,000 bytes of a frame's 65,536.
BOARD_CONTENT_MAX_BYTES = 30_000
INT_MAX = 2_147_483_647


class PanelKind(StrEnum):
    TEXT = "text"
    CANVAS = "canvas"
    DRAWING = "drawing"


class PanelState(StrEnum):
    OPEN = "open"
    MINIMISED = "minimised"
    MAXIMISED = "maximised"
    CLOSED = "closed"


Revision = Annotated[int, Field(ge=1, le=INT_MAX, strict=True)]
# One line: no control character (C0 or DEL), and at least one character that is not a space.
PanelTitle = Annotated[
    str,
    Field(
        min_length=1,
        max_length=BOARD_TITLE_MAX,
        pattern=r"^[^\x00-\x1f\x7f]*[^\x00-\x20\x7f][^\x00-\x1f\x7f]*$",
    ),
]
# No control character except tab, line feed and carriage return (Postgres cannot store NUL).
PanelContent = Annotated[
    str,
    Field(
        min_length=1,
        max_length=BOARD_CONTENT_MAX_BYTES,
        pattern=r"^[^\x00-\x08\x0b\x0c\x0e-\x1f]+$",
    ),
]
Paise = Annotated[int, Field(ge=0, le=INT_MAX, strict=True)]


def board_content_sha256(content: str) -> str:
    """The `content_sha256` of a panel: SHA-256 of the content's UTF-8 bytes, lower-case hex."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- the Drawing panel and its picture (CAD board, slice 1: plan `.10x/plans/2026-11-28-...`) ----

BOARD_PICTURE_MAX_BYTES = 2_000_000  # the whole picture, uncompressed
BOARD_CHUNK_MAX_BYTES = 40_000  # one chunk, gzip bytes before base64
BOARD_CHUNKS_MAX = 64
DRAWING_STEPS_MAX = 40
DRAWING_STEP_LABEL_MAX = 80
_BASE64_STD = r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
# Base64 of 40,000 bytes is 53,336 characters.
_CHUNK_DATA_MAX_CHARS = (BOARD_CHUNK_MAX_BYTES + 2) // 3 * 4

PictureBytes = Annotated[int, Field(ge=1, le=BOARD_PICTURE_MAX_BYTES, strict=True)]
StepLabel = Annotated[
    str, Field(min_length=1, max_length=DRAWING_STEP_LABEL_MAX, pattern=r"^[^\x00-\x1f\x7f]+$")
]


class DrawingPicture(_Contract):
    """The picture a Drawing panel shows: named by hash, carried by `board.content` chunks."""

    sha256: Sha256Hex
    bytes: PictureBytes
    media_type: Literal["image/svg+xml"]


class DrawingContent(_Contract):
    """The `content` of a Drawing panel: what the picture is, never the picture itself."""

    engine: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,30}$")]
    engine_version: Annotated[str, Field(min_length=1, max_length=30, pattern=r"^[0-9A-Za-z.+-]+$")]
    units: Literal["mm"]
    items: Annotated[int, Field(ge=0, le=100_000, strict=True)]
    steps: Annotated[tuple[StepLabel, ...], Field(max_length=DRAWING_STEPS_MAX)]
    picture: DrawingPicture


def drawing_content(d: DrawingContent) -> str:
    """The panel's `content`: one canonical JSON text (sorted keys, no spaces, not ASCII-escaped),
    so the content hash is the same wherever it is computed."""
    return json.dumps(
        d.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def parse_drawing_content(content: str) -> DrawingContent:
    """The description in `content`; `ValueError` unless it is exactly `drawing_content` of it."""
    d = DrawingContent.model_validate(json.loads(content))
    if drawing_content(d) != content:
        raise ValueError("a drawing's content is not in canonical form")
    return d


class BoardShowArgs(_Contract):
    """One whole panel. The schema counts characters; the limit is in bytes, and the hash must
    be the content's, so the model checks both."""

    panel_id: UUID
    revision: Revision
    kind: PanelKind
    title: PanelTitle
    content: PanelContent
    content_sha256: Sha256Hex
    cost_paise: Paise
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _content_fits_and_matches(self) -> Self:
        if len(self.content.encode("utf-8")) > BOARD_CONTENT_MAX_BYTES:
            raise ValueError("content is over 30,000 bytes")
        if self.content_sha256 != board_content_sha256(self.content):
            raise ValueError("content_sha256 is not the content's hash")
        if self.kind is PanelKind.DRAWING:
            parse_drawing_content(self.content)
        return self


class BoardUpdateArgs(BoardShowArgs):
    """The whole panel at its new revision, in the shape of `board.show`, so a node that has
    lost the panel can restore it."""


class BoardSetStateArgs(_Contract):
    panel_id: UUID
    state: PanelState


class BoardFocusArgs(_Contract):
    panel_id: UUID


class BoardContentArgs(_Contract):
    """One chunk of a picture: the gzip of the whole SVG is cut into `count` chunks, each carried
    in its own `board.content` action. The schema counts characters; the model also decodes."""

    sha256: Sha256Hex  # of the whole picture, uncompressed
    media_type: Literal["image/svg+xml"]
    encoding: Literal["gzip"]
    bytes: PictureBytes  # uncompressed
    index: Annotated[int, Field(ge=0, le=BOARD_CHUNKS_MAX - 1, strict=True)]
    count: Annotated[int, Field(ge=1, le=BOARD_CHUNKS_MAX, strict=True)]
    data: Annotated[str, Field(min_length=4, max_length=_CHUNK_DATA_MAX_CHARS, pattern=_BASE64_STD)]

    @model_validator(mode="after")
    def _chunk_is_inside_the_set_and_fits(self) -> Self:
        if self.index >= self.count:
            raise ValueError("index is not below count")
        try:
            raw = base64.b64decode(self.data, validate=True)
        except binascii.Error as e:
            raise ValueError("data is not base64") from e
        if len(raw) > BOARD_CHUNK_MAX_BYTES:
            raise ValueError("a chunk is over 40,000 bytes")
        if base64.b64encode(raw).decode("ascii") != self.data:
            raise ValueError("data is not the canonical base64 of its bytes")
        return self


class BoardWorkingArgs(_Contract):
    """Board work has started (the voice turn queued a job). There is no "ended"."""

    state: Literal["started"]


class _Check(_Contract):
    check: Code | None = None  # omitted when there is none; never null, as in the schema

    @field_validator("check", mode="before")
    @classmethod
    def _check_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("`check` is omitted when there is none, never null")
        return value


class RenderEvidence(_Check):
    """Each is null only when the args gave the node no usable value."""

    panel_id: UUID | None
    revision: Revision | None
    content_sha256: Sha256Hex | None


class BoardShowReceipt(_Contract):
    """`RENDERED`: the Board drew this panel at this revision (not: it was handed over)."""

    reply_id: UUID
    seq: Seq
    result: Literal["RENDERED", "REFUSED", "ERROR"]
    evidence: RenderEvidence

    @model_validator(mode="after")
    def _rendered_names_what_was_drawn(self) -> Self:
        e = self.evidence
        if self.result == "RENDERED" and None in (e.panel_id, e.revision, e.content_sha256):
            raise ValueError("a RENDERED receipt names the panel, revision and hash")
        return self


class PanelEvidence(_Check):
    panel_id: UUID | None


class BoardSetStateReceipt(_Contract):
    reply_id: UUID
    seq: Seq
    result: Literal["OK", "REFUSED"]
    evidence: PanelEvidence

    @model_validator(mode="after")
    def _ok_names_the_panel(self) -> Self:
        if self.result == "OK" and self.evidence.panel_id is None:
            raise ValueError("an OK receipt names the panel")
        return self


class BoardWorkingReceipt(_Contract):
    reply_id: UUID
    seq: Seq
    result: Literal["OK", "REFUSED"]
    evidence: _Check


class ContentEvidence(_Check):
    """Each is null only when the args gave the node no usable value."""

    sha256: Sha256Hex | None
    index: Annotated[int, Field(ge=0, le=BOARD_CHUNKS_MAX - 1, strict=True)] | None


class BoardContentReceipt(_Contract):
    """`STORED`: the node kept this chunk of this picture (a repeat is stored again, harmlessly)."""

    reply_id: UUID
    seq: Seq
    result: Literal["STORED", "REFUSED"]
    evidence: ContentEvidence

    @model_validator(mode="after")
    def _stored_names_the_chunk(self) -> Self:
        e = self.evidence
        if self.result == "STORED" and None in (e.sha256, e.index):
            raise ValueError("a STORED receipt names the picture and the chunk")
        return self


class BoardState(_Contract):
    """Node to core: the person changed a panel's state on the Board by hand."""

    panel_id: UUID
    state: PanelState
    revision: Revision
    at: AwareDatetime


class KeyRole(StrEnum):
    CORE = "core"
    GATEWAY = "gateway"


class KeysetAnnouncement(_Contract):
    """The signed part of a `keyset`: what `check_keyset` checks (R12-26)."""

    role: KeyRole
    key_id: KeyId
    public_key: PublicKey
    authz: Authz


class Keyset(KeysetAnnouncement):
    """The `keyset` message: the announcement and the unsigned `outbox_id` of its
    `keys.keyset` item, which the node acks once the keyset is accepted (R12-26)."""

    outbox_id: UUID


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
    "challenge": Challenge,
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
    "board.state": BoardState,
    "keyset": Keyset,
    "ping": Ping,
    "pong": Pong,
    "error": Error,
    "authz": Authz,
}

# Schemas in contracts/messages that are not message kinds: the args and receipts of the
# test-folder actions (R12-25) and of the Board's actions, validated by core, not by the gateway.
DESKTOP_MODELS: dict[str, type[BaseModel]] = {
    "pc.list_test_folder.args": ListTestFolderArgs,
    "pc.delete_test_file.args": FolderFile,
    "pc.list_test_folder.receipt": ListingReceipt,
    "pc.delete_test_file.receipt": DeleteReceipt,
    # The Board's live actions: `authz.object.args`, and their `action.receipt` payloads.
    "board.show.args": BoardShowArgs,
    "board.update.args": BoardUpdateArgs,
    "board.set_state.args": BoardSetStateArgs,
    "board.focus.args": BoardFocusArgs,
    "board.working.args": BoardWorkingArgs,
    "board.content.args": BoardContentArgs,
    "board.show.receipt": BoardShowReceipt,
    "board.set_state.receipt": BoardSetStateReceipt,
    "board.working.receipt": BoardWorkingReceipt,
    "board.content.receipt": BoardContentReceipt,
}
