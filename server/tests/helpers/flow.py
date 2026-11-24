"""Helpers for the policy and approval-flow tests (Task 11). Every person is fictional (D11).

Rows the gateway would write (`conv.inbound`, `gw.node`) are written as the owner, as
test_turn does; everything core does goes through the real code on a `jarvis_app` connection.
Each node made here has a real Ed25519 key, and each `conv.inbound` row carries the node's
signed envelope, as the gateway stores it (R12-34), unless a test passes another.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.types.json import Jsonb

from jarvis_server import jobs, signing
from jarvis_server.core import approvals, dispatch

from ..schema_registry import CONTRACTS, validator

Conn = psycopg.Connection[Any]
HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")

A_TXT = {
    "name": "a.txt",
    "size": 12,
    "sha256": hashlib.sha256(b"fictional a").hexdigest(),
    "file_id": "00000000a1b2c3d4:0000000000000000000000000000a001",
}
B_TXT = {
    "name": "b.txt",
    "size": 7,
    "sha256": hashlib.sha256(b"fictional b").hexdigest(),
    "file_id": "00000000a1b2c3d4:0000000000000000000000000000b002",
}


def conform(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """`payload`, after checking it against `contracts/messages/<name>.schema.json` (R12-25)."""
    schema = json.loads((CONTRACTS / "messages" / f"{name}.schema.json").read_text())
    validator(schema).validate(payload)
    return payload


def listing_receipt(
    reply_id: Any, seq: int, files: list[dict[str, Any]], result: str = "OK"
) -> dict[str, Any]:
    """A `pc.list_test_folder` receipt, in the contract's shape."""
    return conform(
        "pc.list_test_folder.receipt",
        {"reply_id": str(reply_id), "seq": seq, "result": result, "evidence": {"files": files}},
    )


def delete_receipt(
    key: str, result: str = "RECYCLED", *, file_id: str | None = A_TXT["file_id"],
    before_sha256: str | None = A_TXT["sha256"], recycled_at: str | None = "now",
) -> dict[str, Any]:  # fmt: skip
    """A `pc.delete_test_file` receipt, in the contract's shape."""
    when = datetime.now().astimezone().isoformat() if recycled_at == "now" else recycled_at
    return conform(
        "pc.delete_test_file.receipt",
        {"execution_key": key, "result": result,
         "evidence": {"file_id": file_id, "before_sha256": before_sha256, "recycled_at": when}},
    )  # fmt: skip


def new_person(owner: Conn, name: str = "Asha Fictional") -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, %s, %s, 'test')",
        (pid, name, HOUSEHOLD),
    )
    return pid


_NODE_KEYS: dict[uuid.UUID, Ed25519PrivateKey] = {}
SIGNED: Any = object()  # `inbound`'s default: the node's own signed envelope


def new_node(owner: Conn, person: uuid.UUID, kind: str = "DESKTOP") -> uuid.UUID:
    key = Ed25519PrivateKey.generate()
    row = owner.execute(
        "INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)"
        " VALUES (%s, %s, 'pc', %s, %s) RETURNING id",
        (person, kind, signing.public_bytes(key), f"ts:pc-{secrets.token_hex(4)}"),
    ).fetchone()
    assert row is not None
    node_id = uuid.UUID(str(row[0]))
    _NODE_KEYS[node_id] = key
    return node_id


def node_key(node: uuid.UUID, key: Ed25519PrivateKey) -> None:
    """Sign `node`'s planted messages with `key` (a node enrolled by a FakeNode)."""
    _NODE_KEYS[node] = key


def node_envelope(
    node: uuid.UUID,
    message_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    *,
    key: Ed25519PrivateKey | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """The `node-msg` envelope `node` sends (signed with its key, or `key`), with `fields`
    overriding the envelope's own before signing."""
    signer = key or _NODE_KEYS[node]
    env = {
        "version": 1, "message_id": str(message_id), "node_id": str(node), "seq": 2,
        "sent_at": datetime.now().astimezone().isoformat(), "kind": kind, "payload": payload,
        **fields,
    }  # fmt: skip
    key_id = signing.fingerprint(signing.public_bytes(signer))
    return signing.sign_envelope(signing.Purpose.NODE_MSG, signer, key_id, env)


def open_session(app: Conn, person: uuid.UUID) -> uuid.UUID:
    app.execute(
        "INSERT INTO conv.session (person_id) VALUES (%s)"
        " ON CONFLICT (person_id) WHERE closed_at IS NULL DO NOTHING",
        (person,),
    )
    row = app.execute(
        "SELECT id FROM conv.session WHERE person_id = %s AND closed_at IS NULL", (person,)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def db_now(conn: Conn) -> datetime:
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def inbound(
    owner: Conn,
    node: uuid.UUID,
    person: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
    envelope: Any = SIGNED,
) -> uuid.UUID:
    """A `conv.inbound` row as `conv.record_inbound` writes it (person from the node), with
    the node's signed envelope; `envelope` replaces it (None: none, a callable: built from
    the inbound ID)."""
    iid = uuid.uuid4()
    if envelope is SIGNED:
        env = node_envelope(node, iid, kind, payload)
    else:
        env = envelope(iid) if callable(envelope) else envelope
    owner.execute(
        "INSERT INTO conv.inbound (id, node_id, person_id, kind, payload, envelope, received_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s, now()))",
        (iid, node, person, kind, Jsonb(payload), None if env is None else Jsonb(env),
         received_at),
    )  # fmt: skip
    return iid


def claim(app: Conn, job_id: int) -> jobs.Job:
    row = app.execute(
        "UPDATE ops.job SET status = 'RUNNING', attempts = attempts + 1, locked_by = 'test',"
        " locked_until = now() + interval '5 minutes' WHERE id = %s"
        " RETURNING id, kind, payload, attempts, max_attempts",
        (job_id,),
    ).fetchone()
    assert row is not None
    return jobs.Job(id=row[0], kind=row[1], payload=row[2], attempts=row[3], max_attempts=row[4])


def run(app: Conn, kind: str, payload: dict[str, Any]) -> int:
    """Enqueue one job and run it through dispatch (it re-raises a handler's failure)."""
    job_id = jobs.enqueue(app, kind, payload)
    dispatch.handle(app, claim(app, job_id))
    return job_id


def run_queued(app: Conn, kind: str) -> int | None:
    """Run the oldest ready queued job of `kind` (one the gateway enqueued), if any."""
    row = app.execute(
        "SELECT id FROM ops.job WHERE kind = %s AND status = 'QUEUED' AND run_at <= now()"
        " ORDER BY id LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    dispatch.handle(app, claim(app, int(row[0])))
    return int(row[0])


def job_status(app: Conn, job_id: int) -> str:
    row = app.execute("SELECT status FROM ops.job WHERE id = %s", (job_id,)).fetchone()
    assert row is not None
    return str(row[0])


def planting_listing(
    owner: Conn, node: uuid.UUID, person: uuid.UUID, files: list[dict[str, Any]],
    *, received_at: datetime | None = None, result: str = "OK",
) -> Callable[[float], None]:  # fmt: skip
    """A `sleep` for `approvals.set_wait`: on its first call the node "answers" the newest live
    listing addressed to it, as the gateway would record the receipt."""
    planted: list[bool] = []

    def sleep(_: float) -> None:
        if planted:
            return
        row = owner.execute(
            "SELECT reply_id, seq FROM conv.stream_chunk WHERE target_node_id = %s"
            " AND type = 'ACTION' AND authz->'object'->>'action_kind' = 'pc.list_test_folder'"
            " ORDER BY created_at DESC LIMIT 1",
            (node,),
        ).fetchone()
        assert row is not None
        inbound(
            owner, node, person, "action.receipt",
            listing_receipt(row[0], row[1], files if result == "OK" else [], result),
            received_at=received_at,
        )  # fmt: skip
        planted.append(True)

    return sleep


def propose(
    app: Conn,
    owner: Conn,
    *,
    node: uuid.UUID,
    person: uuid.UUID,
    session: uuid.UUID,
    name: str = "a.txt",
    files: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """`approvals.propose_delete`, the listing answered at once (at `now`, if given)."""
    approvals.set_wait(
        None,
        planting_listing(owner, node, person, [A_TXT] if files is None else files,
                         received_at=now),
    )  # fmt: skip
    try:
        return approvals.propose_delete(
            app, session_id=session, person_id=person, node_id=node, name=name,
            now=now or db_now(app),
        )  # fmt: skip
    finally:
        approvals.set_wait(None, None)


def card_sig(app: Conn, approval_id: uuid.UUID) -> str:
    row = app.execute(
        "SELECT card_authz->>'sig' FROM ledger.approval WHERE id = %s", (approval_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def decide(
    app: Conn,
    owner: Conn,
    *,
    node: uuid.UUID,
    person: uuid.UUID,
    approval_id: uuid.UUID,
    decision: str = "APPROVED",
    sig: str | None = None,
) -> tuple[uuid.UUID, int]:
    """An `approval.decision` from `node`, and its job run: (inbound ID, job ID)."""
    iid = inbound(
        owner, node, person, "approval.decision",
        {"approval_id": str(approval_id), "decision": decision,
         "card_sig": sig if sig is not None else card_sig(app, approval_id)},
    )  # fmt: skip
    return iid, run(app, "approval.decision", {"inbound_id": str(iid)})


def approval_row(app: Conn, approval_id: uuid.UUID) -> dict[str, Any]:
    with app.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM ledger.approval WHERE id = %s", (approval_id,))
        row = cur.fetchone()
    assert row is not None
    return row


def task_state(app: Conn, approval_id: uuid.UUID) -> str:
    row = app.execute(
        "SELECT t.state::text FROM ledger.task t JOIN ledger.approval a ON a.task_id = t.id"
        " WHERE a.id = %s",
        (approval_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def receipt_audit(conn: Conn, inbound_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(outcome, reason, details) of the RECEIPT audit rows about one inbound receipt."""
    rows = conn.execute(
        "SELECT outcome::text, reason, details FROM audit.event WHERE entity_type = 'inbound'"
        " AND entity_id = %s AND action = 'RECEIPT' ORDER BY seq",
        (inbound_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def audit(conn: Conn, entity_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(action, outcome, reason, from, to) of one entity's audit rows, in order."""
    rows = conn.execute(
        "SELECT action::text, outcome::text, reason, from_state, to_state FROM audit.event"
        " WHERE entity_id = %s ORDER BY seq",
        (entity_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def items(app: Conn, person: uuid.UUID, kind: str) -> list[dict[str, Any]]:
    with app.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT * FROM comm.outbox WHERE person_id = %s AND kind = %s ORDER BY created_at",
            (person, kind),
        )
        return cur.fetchall()


def outcome_of(app: Conn, inbound_id: uuid.UUID) -> str | None:
    row = app.execute("SELECT outcome FROM conv.inbound WHERE id = %s", (inbound_id,)).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])
