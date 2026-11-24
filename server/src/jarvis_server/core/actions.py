"""Live actions, `memory.write` from conversation, and receipts (spec §6.2 step 8, §9.4, §12).

**Live actions (READ_ONLY).** `issue_live` writes one ACTION chunk of a reply: a core-signed
`node.action` authorisation (§7.1) for the node that spoke, carrying the turn's `session_id`
(R9-1), with `valid_until = now + 2 min`, and NOTIFYs `jarvis_stream`. The gateway sends it as
`node.action` to its target only, while it is valid, and never re-sends it; the node refuses
one past `valid_until` (+30 s); core refuses a receipt that arrived after it.

**`memory.write` (REVERSIBLE, §12).** A STATED entry about, and by, the authenticated person
(`memory_slug(person_id)`), with the turn's inbound ID as its source, and a SILENT
`memory.written` outbox item for history. Nothing in Claude's arguments sets `by`, the viewer
or the subject.

**Receipts (job `action.receipt`).** One per inbound message, which gets an outcome once.
Shapes are the shared contracts (R12-25): `pc.list_test_folder.receipt` and
`pc.delete_test_file.receipt`.
- a live receipt (`reply_id` + `seq`) is accepted only from the chunk's target node, for an
  ACTION chunk of that node's person, received no later than its `valid_until`. A listing that
  verifies a delete goes on to the verification below; a module or screen action gets a SILENT
  `action.readonly_ok`; a listing `approvals.propose_delete` waits for needs nothing more.
- an approved action's receipt (`execution_key`): one per key, through the `ledger.action` row
  issued with the action (`status` ISSUED, locked here), and only from the requesting node. It
  ends the `action.approved` item. RECYCLED with the approved file ID and SHA-256 -> RECEIVED,
  an unverified `ledger.outcome`, a fresh `pc.list_test_folder` to the same node, and
  `action.verify_timeout` at the listing timeout (10 s).
- the fresh listing verifies at source: only an `OK` listing without the file ID closes the
  task (outcome verified, COMPLETED, "Done"). A non-OK or malformed listing, the file still
  listed, a late listing, or none by the timeout: FAILED, and Jarvis says so (v2 §13.3).

Before any of this, a receipt must be the node's own signed message (R12-34,
`core.node_signature`): the stored envelope verifies under the node's key and matches the row.
Otherwise it is refused `bad_node_signature`, and nothing is taken, verified or failed.

Every rejected receipt is audited (R12-24): `ledger.refuse_receipt` with a fixed code, through
`dbcall.audited_call_in_job`, as the handler's last write (R11-1). Task transitions are audited
calls too, and come before the action and outcome writes: a refused transition is kept and
nothing else is written.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from jarvis_server import comm, contracts, jobs, signing
from jarvis_server.contracts import MemoryCategory, MemoryTag
from jarvis_server.core import dispatch, identity, keys, node_signature, rules
from jarvis_server.dbcall import audited_call_in_job
from jarvis_server.memory.engine import MemoryEngine

log = logging.getLogger("jarvis.core.actions")

LIVE_VALIDITY = timedelta(minutes=2)
LISTING_TIMEOUT = timedelta(seconds=10)  # the fixed listing timeout, for the fresh listing too
LISTING = "pc.list_test_folder"
DELETE = "pc.delete_test_file"
LISTING_OK = "OK"
CORE = ("AGENT", "core")

# ledger.refuse_receipt's fixed codes (R12-24), and those that mean "not verified".
RECEIPT_REFUSALS = (
    node_signature.BAD_NODE_SIGNATURE, "unknown_key", "reused_key", "unknown_action", "late",
    "malformed", "not_recycled", "file_mismatch", "listing_failed", "still_listed",
    "listing_missing",
)  # fmt: skip
VERIFY_FAILURES = frozenset({"late", "listing_failed", "still_listed", "listing_missing"})


# --- signing -----------------------------------------------------------------------------------


def rfc3339(t: datetime) -> str:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("signed times carry an offset (R6-4)")
    return t.isoformat()


def authz_object(
    *,
    kind: str,
    action_kind: str,
    args: dict[str, Any],
    target_node_id: UUID,
    person_id: UUID,
    issued_at: datetime,
    valid_until: datetime,
    approval_id: UUID | None = None,
    execution_key: UUID | None = None,
    reply_id: UUID | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """The §7.1 authorisation object: every field present, null when unused."""
    return {
        "v": 1,
        "kind": kind,
        "action_kind": action_kind,
        "args": args,
        "target_node_id": str(target_node_id),
        "person_id": str(person_id),
        "approval_id": None if approval_id is None else str(approval_id),
        "execution_key": None if execution_key is None else str(execution_key),
        "reply_id": None if reply_id is None else str(reply_id),
        "summary": summary,
        "issued_at": rfc3339(issued_at),
        "valid_until": rfc3339(valid_until),
    }


def sign(conn: psycopg.Connection[Any], obj: dict[str, Any]) -> rules.AuthzDict:
    """`{object, key_id, sig}` under the ACTIVE core key (purpose `core-authz`)."""
    key, key_id = keys.active_core_key(conn)
    return cast(rules.AuthzDict, signing.make_authz(key, key_id, obj))


# --- live actions ------------------------------------------------------------------------------


def issue_live(
    conn: psycopg.Connection[Any],
    *,
    reply_id: UUID,
    session_id: UUID,
    target_node_id: UUID,
    person_id: UUID,
    action_kind: str,
    args: dict[str, Any],
    now: datetime,
) -> int:
    """Write one signed ACTION chunk (the next `seq` of the reply) and NOTIFY; return its seq."""
    valid_until = now + LIVE_VALIDITY
    authz = sign(
        conn,
        authz_object(
            kind="node.action", action_kind=action_kind, args=args,
            target_node_id=target_node_id, person_id=person_id, issued_at=now,
            valid_until=valid_until, reply_id=reply_id,
        )
    )  # fmt: skip
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, authz, final,"
            " target_node_id, valid_until)"
            " SELECT %s, %s, coalesce(max(seq), 0) + 1, 'ACTION', %s, false, %s, %s"
            " FROM conv.stream_chunk WHERE reply_id = %s RETURNING seq",
            (reply_id, session_id, Jsonb(authz), target_node_id, valid_until, reply_id),
        ).fetchone()
        assert row is not None
        conn.execute("SELECT pg_notify('jarvis_stream', %s)", (str(reply_id),))
    return int(row[0])


def speak(conn: psycopg.Connection[Any], session_id: UUID, text: str) -> UUID:
    """A one-chunk spoken reply in `session_id` (a result Jarvis reports on its own)."""
    reply_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, text, final)"
            " VALUES (%s, %s, 1, 'SPEECH', %s, true)",
            (reply_id, session_id, text[:4000]),
        )
        conn.execute("SELECT pg_notify('jarvis_stream', %s)", (str(reply_id),))
    return reply_id


def valid_file_name(name: str) -> bool:
    """§9.4 path safety, server side too: a plain name, never a path, stream or device."""
    try:
        contracts.FolderFile.model_validate(
            {"name": name, "size": 0, "sha256": "0" * 64, "file_id": "0" * 16 + ":" + "0" * 32}
        )
    except ValidationError:
        return False
    return True


def parse_listing(payload: Any) -> dict[str, dict[str, Any]] | None:
    """The files, by name, of an `OK` listing receipt (contract `pc.list_test_folder.receipt`,
    R12-25); None for any other result, a payload not in that shape, or a listing that repeats
    a name or a file ID (JSON Schema can't express that uniqueness, so it is checked here)."""
    try:
        receipt = contracts.ListingReceipt.model_validate(payload)
    except ValidationError:
        return None
    if receipt.result != LISTING_OK:
        return None
    files = [f.model_dump() for f in receipt.evidence.files]
    # A folder holds each name once and each file ID once. A listing that repeats either could
    # hide the approved file behind another entry, so it is malformed: fail closed.
    names = {f["name"] for f in files}
    ids = {f["file_id"] for f in files}
    if len(names) != len(files) or len(ids) != len(files):
        return None
    return {f["name"]: f for f in files}


# --- memory.write ------------------------------------------------------------------------------


def memory_write(
    conn: psycopg.Connection[Any],
    engine: MemoryEngine,
    *,
    person_id: UUID,
    inbound_id: UUID,
    content: str,
    category: str,
    topics: Sequence[str],
) -> str:
    """A STATED entry by and about the authenticated person; a SILENT history item. The entry's
    memory ID. Raises the engine's refusal (`MemoryRefused`) if the entry is not acceptable."""
    slug = identity.memory_slug(engine, person_id)  # never from the call (P2-S2)
    memory_id = engine.write(
        slug, MemoryCategory(category), content, MemoryTag.STATED, f"inbound-{inbound_id}",
        by=slug, topics=list(topics),
    )  # fmt: skip
    with conn.transaction():
        event_id = comm.record_event(
            conn, "memory.written", subject_type="memory", subject_id=memory_id,
            person_id=person_id, details={"memory_id": memory_id},
        )  # fmt: skip
        rules.notify(
            conn, "memory.written", person_id=person_id, event_id=event_id,
            context={"text": f"Remembered: {content}"[:4000]},
        )  # fmt: skip
    return memory_id


# --- receipts ----------------------------------------------------------------------------------


def _set_outcome(conn: psycopg.Connection[Any], inbound_id: UUID, outcome: str) -> None:
    conn.execute(
        "UPDATE conv.inbound SET outcome = %s WHERE id = %s AND outcome IS NULL",
        (outcome, inbound_id),
    )


def refuse_receipt(
    conn: psycopg.Connection[Any],
    *,
    ref: UUID,
    node_id: UUID,
    code: str,
    inbound_id: UUID | None,
) -> None:
    """A rejected receipt (R12-24): the inbound's REFUSED outcome, then, last, the audited
    `ledger.refuse_receipt` row in the job's transaction (R11-1). The caller returns next."""
    if inbound_id is not None:
        _set_outcome(conn, inbound_id, "REFUSED")
    audited_call_in_job(conn, "SELECT ledger.refuse_receipt(%s, %s, %s)", (ref, node_id, code))
    log.info("receipt for %s refused: %s", ref, code)


def transition_in_job(
    conn: psycopg.Connection[Any], task_id: UUID, to_state: str, reason: str
) -> bool:
    """`ledger.transition` as an audited call in the job (R11-1). False: refused and audited;
    the caller keeps the refusal and stops."""
    (result,) = audited_call_in_job(
        conn,
        "SELECT ledger.transition('task', %s, %s, %s, %s, %s, NULL)",
        (task_id, to_state, *CORE, reason),
    )
    if result != "APPLIED":
        log.warning("task %s: transition to %s refused", task_id, to_state)
        return False
    return True


def ticket_session(conn: psycopg.Connection[Any], task_id: UUID) -> UUID | None:
    """The session a task's ticket links it to (v2 §8.4), where Jarvis reports on it."""
    row = conn.execute(
        "SELECT session_id FROM conv.session_ticket WHERE task_id = %s LIMIT 1", (task_id,)
    ).fetchone()
    return None if row is None else UUID(str(row[0]))


def db_now(conn: psycopg.Connection[Any]) -> datetime:
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    return cast(datetime, row[0])


def end_items(conn: psycopg.Connection[Any], approval_id: UUID, kinds: Sequence[str]) -> None:
    """Supersede the approval's open outbox items of `kinds`. One that ended meanwhile is
    answered 'terminal' with its own audit row, which commits with the job."""
    rows = conn.execute(
        "SELECT o.id FROM comm.outbox o JOIN comm.event e ON e.id = o.event_id"
        " WHERE e.subject_type = 'approval' AND e.subject_id = %s AND o.kind = ANY(%s)"
        " AND o.state IN ('PENDING', 'DELIVERING', 'DELIVERED')"
        " ORDER BY o.created_at, o.id",
        (str(approval_id), list(kinds)),
    ).fetchall()
    for (outbox_id,) in rows:
        audited_call_in_job(conn, "SELECT comm.supersede(%s)", (outbox_id,))


@dataclass(frozen=True)
class _Issued:
    """An issued approved action: its `ledger.action` row (locked) and approval."""

    action_id: UUID
    task_id: UUID
    status: str
    approval_id: UUID
    node_id: UUID
    person_id: UUID
    args: dict[str, Any]


_ISSUED = """
SELECT a.id, a.task_id, a.status, p.id, p.requesting_node_id, p.requested_from_person_id,
       p.action_args
FROM ledger.action a JOIN ledger.approval p ON p.execution_key::text = a.idempotency_key
WHERE {where} FOR UPDATE OF a
"""


def _issued(conn: psycopg.Connection[Any], where: LiteralString, value: str) -> _Issued | None:
    row = conn.execute(_ISSUED.format(where=where), (value,)).fetchone()
    if row is None:
        return None
    return _Issued(
        UUID(str(row[0])), UUID(str(row[1])), str(row[2]), UUID(str(row[3])),
        UUID(str(row[4])), UUID(str(row[5])), row[6],
    )  # fmt: skip


def _fail(
    conn: psycopg.Connection[Any],
    issued: _Issued,
    *,
    code: str,
    ref: UUID,
    inbound_id: UUID | None,
    evidence: dict[str, Any],
) -> None:
    """§9.4 "If any check fails, the task is FAILED and Jarvis says so". The transition comes
    first: refused, it is kept and nothing else is written. Then the action and its outcome
    (never verified), the spoken result, and last the audited receipt refusal."""
    if not transition_in_job(conn, issued.task_id, "FAILED", code):
        return
    conn.execute("UPDATE ledger.action SET status = 'FAILED' WHERE id = %s", (issued.action_id,))
    updated = conn.execute(
        "UPDATE ledger.outcome SET evidence = evidence || %s WHERE action_id = %s",
        (Jsonb({"check": code}), issued.action_id),
    )
    if updated.rowcount == 0:
        conn.execute(
            "INSERT INTO ledger.outcome (action_id, verified, evidence, source_ref)"
            " VALUES (%s, false, %s, %s)",
            (issued.action_id, Jsonb({**evidence, "check": code}),
             f"approval:{issued.approval_id}"),
        )  # fmt: skip
    session_id = ticket_session(conn, issued.task_id)
    if session_id is not None:
        name = issued.args.get("name", "the file")
        text = (
            f"I couldn't confirm that {name} was deleted."
            if code in VERIFY_FAILURES
            else f"I didn't delete {name}: the file had changed or couldn't be recycled."
        )
        speak(conn, session_id, text)
    refuse_receipt(conn, ref=ref, node_id=issued.node_id, code=code, inbound_id=inbound_id)


@dispatch.register("action.receipt")
def on_receipt(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Handler of `action.receipt`, payload `{inbound_id}`."""
    inbound_id = UUID(job.payload["inbound_id"])
    row = conn.execute(
        "SELECT node_id, person_id, kind, payload, outcome, received_at, envelope"
        " FROM conv.inbound WHERE id = %s FOR UPDATE",
        (inbound_id,),
    ).fetchone()
    if row is None or row[4] is not None:
        return  # pruned, or already handled (a re-run job)
    node_id, person_id, kind, payload, received_at = (
        UUID(str(row[0])), UUID(str(row[1])), row[2], row[3], row[5],
    )  # fmt: skip
    if kind != "action.receipt":
        raise ValueError(f"inbound {inbound_id} is a {kind}, not an action.receipt")
    # R12-34: before anything else, the receipt must be the node's own signed message. This
    # covers an approved action's receipt, the verification listing and the proposal listing.
    if not node_signature.signed_by_node(
        conn, inbound_id=inbound_id, node_id=node_id, kind=kind, payload=payload,
        envelope=row[6],
    ):  # fmt: skip
        refuse_receipt(conn, ref=inbound_id, node_id=node_id,
                       code=node_signature.BAD_NODE_SIGNATURE, inbound_id=inbound_id)  # fmt: skip
        return
    if "execution_key" in payload:
        _approved_receipt(conn, inbound_id, node_id, payload)
    else:
        _live_receipt(conn, inbound_id, node_id, person_id, payload, received_at)


def _live_receipt(
    conn: psycopg.Connection[Any],
    inbound_id: UUID,
    node_id: UUID,
    person_id: UUID,
    payload: dict[str, Any],
    received_at: datetime,
) -> None:
    reply_id = UUID(str(payload["reply_id"]))
    chunk = conn.execute(
        "SELECT k.authz, k.valid_until, k.target_node_id, s.person_id"
        " FROM conv.stream_chunk k JOIN conv.session s ON s.id = k.session_id"
        " WHERE k.reply_id = %s AND k.seq = %s AND k.type = 'ACTION'",
        (reply_id, payload["seq"]),
    ).fetchone()
    if chunk is None or chunk[2] != node_id or chunk[3] != person_id:
        refuse_receipt(conn, ref=inbound_id, node_id=node_id, code="unknown_action",
                       inbound_id=inbound_id)  # fmt: skip
        return
    action_kind = str(chunk[0]["object"]["action_kind"])
    issued = None
    if action_kind == LISTING:  # the fresh listing after a delete?
        issued = _issued(
            conn,
            "a.id = (SELECT o.action_id FROM ledger.outcome o"
            " WHERE o.evidence->>'listing_reply_id' = %s)",
            str(reply_id),
        )
    late = received_at > chunk[1]  # C3, core side: past its signed valid_until
    if issued is not None and issued.status == "RECEIVED":
        if late:
            _fail(conn, issued, code="late", ref=inbound_id, inbound_id=inbound_id, evidence={})
        else:
            _verify(conn, issued, payload, inbound_id)
        return
    if late:
        refuse_receipt(conn, ref=inbound_id, node_id=node_id, code="late", inbound_id=inbound_id)
        return
    _set_outcome(conn, inbound_id, "ANSWERED")
    if action_kind != LISTING:
        event_id = comm.record_event(
            conn, "action.readonly_ok", subject_type="stream", subject_id=str(reply_id),
            person_id=person_id, details={"result": str(payload["result"])},
        )  # fmt: skip
        rules.notify(conn, "action.readonly_ok", person_id=person_id, event_id=event_id,
                     context={})  # fmt: skip


def _approved_receipt(
    conn: psycopg.Connection[Any], inbound_id: UUID, node_id: UUID, payload: dict[str, Any]
) -> None:
    key = str(UUID(str(payload["execution_key"])))
    issued = _issued(conn, "a.idempotency_key = %s", key)
    if issued is None or issued.node_id != node_id:
        refuse_receipt(conn, ref=inbound_id, node_id=node_id, code="unknown_key",
                       inbound_id=inbound_id)  # fmt: skip
        return
    if issued.status != "ISSUED":  # one receipt per key
        refuse_receipt(conn, ref=inbound_id, node_id=node_id, code="reused_key",
                       inbound_id=inbound_id)  # fmt: skip
        return
    # The key's one receipt is taken now, whatever happens to the task below: a replay of it
    # is `reused_key` even if the task can't move (§9.4 "only for an issued key, once").
    conn.execute("UPDATE ledger.action SET status = 'RECEIVED' WHERE id = %s", (issued.action_id,))
    end_items(conn, issued.approval_id, ("action.approved",))  # answered: never re-sent
    try:
        receipt = contracts.DeleteReceipt.model_validate(payload)
    except ValidationError:
        _fail(conn, issued, code="malformed", ref=inbound_id, inbound_id=inbound_id,
              evidence={"result": str(payload.get("result"))})  # fmt: skip
        return
    evidence = {"result": receipt.result, "file_id": receipt.evidence.file_id,
                "before_sha256": receipt.evidence.before_sha256}  # fmt: skip
    if receipt.result != "RECYCLED":
        _fail(conn, issued, code="not_recycled", ref=inbound_id, inbound_id=inbound_id,
              evidence=evidence)  # fmt: skip
        return
    if (receipt.evidence.file_id, receipt.evidence.before_sha256) != (
        issued.args.get("file_id"), issued.args.get("sha256"),
    ):  # fmt: skip
        _fail(conn, issued, code="file_mismatch", ref=inbound_id, inbound_id=inbound_id,
              evidence=evidence)  # fmt: skip
        return
    session_id = ticket_session(conn, issued.task_id)
    if session_id is None:  # nowhere to list: it can't be verified, so it is never done
        _set_outcome(conn, inbound_id, "ANSWERED")  # the receipt itself was right
        _fail(conn, issued, code="listing_missing", ref=issued.approval_id,
              inbound_id=None, evidence=evidence)  # fmt: skip
        return
    listing_reply = uuid.uuid4()
    conn.execute(
        "INSERT INTO ledger.outcome (action_id, verified, evidence, source_ref)"
        " VALUES (%s, false, %s, %s)",
        (issued.action_id, Jsonb({**evidence, "listing_reply_id": str(listing_reply)}),
         f"approval:{issued.approval_id}"),
    )  # fmt: skip
    issue_live(
        conn, reply_id=listing_reply, session_id=session_id, target_node_id=node_id,
        person_id=issued.person_id, action_kind=LISTING, args={}, now=db_now(conn),
    )  # fmt: skip
    jobs.enqueue(conn, "action.verify_timeout", {"action_id": str(issued.action_id)},
                 delay=LISTING_TIMEOUT)  # fmt: skip
    _set_outcome(conn, inbound_id, "ANSWERED")


def _verify(
    conn: psycopg.Connection[Any], issued: _Issued, payload: dict[str, Any], inbound_id: UUID
) -> None:
    """The fresh listing after RECYCLED: an `OK` listing without the approved file ID is the
    only thing that closes the task (§9.4)."""
    listing = parse_listing(payload)
    file_id = issued.args.get("file_id")
    if listing is None:
        _fail(conn, issued, code="listing_failed", ref=inbound_id, inbound_id=inbound_id,
              evidence={})  # fmt: skip
        return
    if any(f["file_id"] == file_id for f in listing.values()):
        _fail(conn, issued, code="still_listed", ref=inbound_id, inbound_id=inbound_id,
              evidence={})  # fmt: skip
        return
    if not transition_in_job(conn, issued.task_id, "COMPLETED", "verified"):
        return  # kept; nothing is marked verified beside a task that didn't move
    conn.execute(
        "UPDATE ledger.outcome SET verified = true, verified_at = now(),"
        " evidence = evidence || %s WHERE action_id = %s",
        (Jsonb({"verified_by": "fresh_listing"}), issued.action_id),
    )
    conn.execute("UPDATE ledger.action SET status = 'VERIFIED' WHERE id = %s", (issued.action_id,))
    _set_outcome(conn, inbound_id, "ANSWERED")
    session_id = ticket_session(conn, issued.task_id)
    if session_id is not None:
        speak(conn, session_id, f"Done: {issued.args.get('name')} is in the Recycle Bin.")


@dispatch.register("action.verify_timeout")
def on_verify_timeout(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """The fresh listing's 10 s (the listing timeout): if it hasn't verified the action by now,
    the action is not verified, so the task is FAILED and Jarvis says so (§9.4)."""
    issued = _issued(conn, "a.id = %s", str(UUID(job.payload["action_id"])))
    if issued is None or issued.status != "RECEIVED":
        return  # verified or failed already
    _fail(conn, issued, code="listing_missing", ref=issued.approval_id, inbound_id=None,
          evidence={})  # fmt: skip


@dispatch.register("outbox.expire")
def on_outbox_expire(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Expire one outbox item once it is past its `expires_at` (§5.4); payload `{outbox_id}`.
    An item already ended is left as it is; one run early is re-queued for its expiry.
    `comm.expire` locks the item itself; one that ends in between is answered 'terminal' with
    its own audit row, which commits with the job."""
    outbox_id = UUID(job.payload["outbox_id"])
    row = conn.execute(
        "SELECT state, expires_at <= now(), expires_at - now() FROM comm.outbox WHERE id = %s",
        (outbox_id,),
    ).fetchone()
    if row is None or row[0] not in ("PENDING", "DELIVERING", "DELIVERED") or row[1] is None:
        return
    if not row[1]:
        jobs.enqueue(conn, "outbox.expire", jobs.requeue_payload(job.payload), delay=row[2])
        return
    audited_call_in_job(conn, "SELECT comm.expire(%s)", (outbox_id,))
