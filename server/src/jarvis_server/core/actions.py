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

**Receipts (job `action.receipt`).** One per inbound message, which gets an outcome once:
- a live receipt (`reply_id` + `seq`) is accepted only from the chunk's target node, for an
  ACTION chunk of that node's person, received no later than its `valid_until`. A listing that
  verifies a delete goes on to the verification below; a module or screen action gets a SILENT
  `action.readonly_ok`; a listing `approvals.propose_delete` waits for needs nothing more.
- an approved action's receipt (`execution_key`): one per key, through the `ledger.action` row
  issued with the action (`status` ISSUED, locked here). RECYCLED with the approved file ID and
  hash -> RECEIVED, an unverified `ledger.outcome`, and a fresh `pc.list_test_folder` to the
  same node. Anything else -> the task is FAILED and Jarvis says so.
- the fresh listing's receipt verifies at source: the file ID is absent -> the outcome is
  verified, the task COMPLETED and Jarvis says it is done; otherwise the task is FAILED. It is
  never marked done without that listing (v2 §13.3).

A refused receipt sets the inbound's outcome REFUSED and records event
`action.receipt_refused` (codes only). Task transitions inside the job are audited calls, made
through `dbcall.audited_call_in_job` (R11-1); a refused transition is kept and the handler
stops.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from jarvis_server import comm, jobs, signing
from jarvis_server.contracts import MemoryCategory, MemoryTag
from jarvis_server.core import dispatch, identity, keys, rules
from jarvis_server.dbcall import audited_call_in_job
from jarvis_server.memory.engine import MemoryEngine

log = logging.getLogger("jarvis.core.actions")

LIVE_VALIDITY = timedelta(minutes=2)
LISTING = "pc.list_test_folder"
DELETE = "pc.delete_test_file"
CORE = ("AGENT", "core")

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILE_ID = re.compile(r"[A-Za-z0-9:_-]{1,100}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # §9.4 path safety, server side too


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


def sign(obj: dict[str, Any]) -> rules.AuthzDict:
    """`{object, key_id, sig}` under the active core key (purpose `core-authz`)."""
    key, key_id = keys.active_core_key()
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


def parse_listing(evidence: Any) -> dict[str, dict[str, Any]] | None:
    """A listing's files by name, `{name, size, sha256, file_id}` each; None if malformed."""
    files = evidence.get("files") if isinstance(evidence, dict) else None
    if not isinstance(files, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        if not isinstance(f, dict) or set(f) != {"name", "size", "sha256", "file_id"}:
            return None
        name, size, sha, file_id = f["name"], f["size"], f["sha256"], f["file_id"]
        if not (
            isinstance(name, str) and NAME.fullmatch(name)
            and isinstance(size, int) and not isinstance(size, bool) and size >= 0
            and isinstance(sha, str) and _SHA256.fullmatch(sha)
            and isinstance(file_id, str) and _FILE_ID.fullmatch(file_id)
        ):  # fmt: skip
            return None
        out[name] = {"name": name, "size": size, "sha256": sha, "file_id": file_id}
    return out


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


def _refuse_receipt(
    conn: psycopg.Connection[Any], inbound_id: UUID, person_id: UUID, reason: str
) -> None:
    comm.record_event(
        conn, "action.receipt_refused", subject_type="inbound", subject_id=str(inbound_id),
        person_id=person_id, details={"reason": reason},
    )  # fmt: skip
    _set_outcome(conn, inbound_id, "REFUSED")
    log.info("receipt %s refused: %s", inbound_id, reason)


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


@dispatch.register("action.receipt")
def on_receipt(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Handler of `action.receipt`, payload `{inbound_id}`."""
    inbound_id = UUID(job.payload["inbound_id"])
    row = conn.execute(
        "SELECT node_id, person_id, kind, payload, outcome, received_at FROM conv.inbound"
        " WHERE id = %s FOR UPDATE",
        (inbound_id,),
    ).fetchone()
    if row is None or row[4] is not None:
        return  # pruned, or already handled (a re-run job)
    node_id, person_id, kind, payload, received_at = (
        UUID(str(row[0])), UUID(str(row[1])), row[2], row[3], row[5],
    )  # fmt: skip
    if kind != "action.receipt":
        raise ValueError(f"inbound {inbound_id} is a {kind}, not an action.receipt")
    if "execution_key" in payload:
        _approved_receipt(conn, inbound_id, node_id, person_id, payload)
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
        "SELECT k.authz, k.valid_until, k.target_node_id, s.person_id, k.session_id"
        " FROM conv.stream_chunk k JOIN conv.session s ON s.id = k.session_id"
        " WHERE k.reply_id = %s AND k.seq = %s AND k.type = 'ACTION'",
        (reply_id, payload["seq"]),
    ).fetchone()
    if chunk is None or chunk[2] != node_id or chunk[3] != person_id:
        _refuse_receipt(conn, inbound_id, person_id, "unknown_action")
        return
    action_kind = str(chunk[0]["object"]["action_kind"])
    verifying = None
    if action_kind == LISTING:
        verifying = conn.execute(
            "SELECT o.id, o.action_id, o.evidence FROM ledger.outcome o"
            " WHERE o.evidence->>'listing_reply_id' = %s AND NOT o.verified FOR UPDATE",
            (str(reply_id),),
        ).fetchone()
    if received_at > chunk[1]:  # C3, core side: past its signed valid_until
        _refuse_receipt(conn, inbound_id, person_id, "expired")
        if verifying is not None:
            _verify(conn, verifying, None, UUID(str(chunk[4])))
        return
    _set_outcome(conn, inbound_id, "ANSWERED")
    if verifying is not None:
        _verify(conn, verifying, parse_listing(payload.get("evidence")), UUID(str(chunk[4])))
    elif action_kind != LISTING:
        event_id = comm.record_event(
            conn, "action.readonly_ok", subject_type="stream", subject_id=str(reply_id),
            person_id=person_id, details={"result": str(payload["result"])},
        )  # fmt: skip
        rules.notify(conn, "action.readonly_ok", person_id=person_id, event_id=event_id,
                     context={})  # fmt: skip


def _approved_receipt(
    conn: psycopg.Connection[Any],
    inbound_id: UUID,
    node_id: UUID,
    person_id: UUID,
    payload: dict[str, Any],
) -> None:
    key = str(UUID(str(payload["execution_key"])))
    row = conn.execute(
        "SELECT a.id, a.task_id, a.status, p.id, p.requesting_node_id, p.action_args"
        " FROM ledger.action a JOIN ledger.approval p ON p.execution_key::text = a.idempotency_key"
        " WHERE a.idempotency_key = %s FOR UPDATE OF a",
        (key,),
    ).fetchone()
    if row is None or row[4] != node_id:
        _refuse_receipt(conn, inbound_id, person_id, "unknown_key")
        return
    action_id, task_id, status, approval_id, args = (
        UUID(str(row[0])), UUID(str(row[1])), row[2], UUID(str(row[3])), row[5],
    )  # fmt: skip
    if status != "ISSUED":  # one receipt per key
        _refuse_receipt(conn, inbound_id, person_id, "duplicate_receipt")
        return
    result = str(payload["result"])
    evidence = payload.get("evidence") or {}
    matches = (
        result == "RECYCLED"
        and evidence.get("file_id") == args.get("file_id")
        and evidence.get("sha256") == args.get("sha256")
    )
    session_id = ticket_session(conn, task_id)
    _set_outcome(conn, inbound_id, "ANSWERED")
    if not matches:
        conn.execute("UPDATE ledger.action SET status = 'FAILED' WHERE id = %s", (action_id,))
        conn.execute(
            "INSERT INTO ledger.outcome (action_id, verified, evidence, source_ref)"
            " VALUES (%s, false, %s, %s)",
            (action_id, Jsonb({"result": result, "check": "receipt"}), f"approval:{approval_id}"),
        )
        if not transition_in_job(conn, task_id, "FAILED", f"receipt_{result.lower()}"):
            return
        if session_id is not None:
            speak(conn, session_id, f"I didn't delete {args.get('name')}: the file had changed "
                  "or couldn't be recycled.")  # fmt: skip
        return
    listing_reply = uuid.uuid4()
    conn.execute("UPDATE ledger.action SET status = 'RECEIVED' WHERE id = %s", (action_id,))
    conn.execute(
        "INSERT INTO ledger.outcome (action_id, verified, evidence, source_ref)"
        " VALUES (%s, false, %s, %s)",
        (
            action_id,
            Jsonb({"result": result, "file_id": args["file_id"], "sha256": args["sha256"],
                   "listing_reply_id": str(listing_reply)}),
            f"approval:{approval_id}",
        ),
    )  # fmt: skip
    if session_id is None:  # no session to list in: it can't be verified, so never done
        conn.execute("UPDATE ledger.action SET status = 'FAILED' WHERE id = %s", (action_id,))
        transition_in_job(conn, task_id, "FAILED", "unverifiable")
        return
    issue_live(
        conn, reply_id=listing_reply, session_id=session_id, target_node_id=node_id,
        person_id=person_id, action_kind=LISTING, args={}, now=db_now(conn),
    )  # fmt: skip


def _verify(
    conn: psycopg.Connection[Any],
    outcome: Sequence[Any],
    listing: dict[str, dict[str, Any]] | None,
    session_id: UUID,
) -> None:
    """The fresh listing after RECYCLED: the file ID must be gone (§9.4)."""
    outcome_id, action_id, evidence = UUID(str(outcome[0])), UUID(str(outcome[1])), outcome[2]
    row = conn.execute(
        "SELECT task_id, status FROM ledger.action WHERE id = %s FOR UPDATE", (action_id,)
    ).fetchone()
    assert row is not None
    task_id, status = UUID(str(row[0])), row[1]
    if status != "RECEIVED":
        return
    gone = listing is not None and all(
        f["file_id"] != evidence["file_id"] for f in listing.values()
    )
    name = conn.execute(
        "SELECT action_args->>'name' FROM ledger.approval WHERE execution_key::text ="
        " (SELECT idempotency_key FROM ledger.action WHERE id = %s)",
        (action_id,),
    ).fetchone()
    file_name = "the file" if name is None else str(name[0])
    if not gone:
        conn.execute("UPDATE ledger.action SET status = 'FAILED' WHERE id = %s", (action_id,))
        if transition_in_job(conn, task_id, "FAILED", "not_verified"):
            speak(conn, session_id, f"I couldn't confirm that {file_name} was deleted.")
        return
    conn.execute(
        "UPDATE ledger.outcome SET verified = true, verified_at = now(),"
        " evidence = evidence || %s WHERE id = %s",
        (Jsonb({"verified_by": "fresh_listing"}), outcome_id),
    )
    conn.execute("UPDATE ledger.action SET status = 'VERIFIED' WHERE id = %s", (action_id,))
    if transition_in_job(conn, task_id, "COMPLETED", "verified"):
        speak(conn, session_id, f"Done: {file_name} is in the Recycle Bin.")
