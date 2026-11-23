"""Helpers for the policy and approval-flow tests (Task 11). Every person is fictional (D11).

Rows the gateway would write (`conv.inbound`, `gw.node`) are written as the owner, as
test_turn does; everything core does goes through the real code on a `jarvis_app` connection.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from jarvis_server import jobs
from jarvis_server.core import approvals, dispatch

Conn = psycopg.Connection[Any]
HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")

A_TXT = {
    "name": "a.txt",
    "size": 12,
    "sha256": hashlib.sha256(b"fictional a").hexdigest(),
    "file_id": "0x1c2d3e4f:0000000000000000000000000000a001",
}
B_TXT = {
    "name": "b.txt",
    "size": 7,
    "sha256": hashlib.sha256(b"fictional b").hexdigest(),
    "file_id": "0x1c2d3e4f:0000000000000000000000000000b002",
}


def new_person(owner: Conn, name: str = "Asha Fictional") -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, %s, %s, 'test')",
        (pid, name, HOUSEHOLD),
    )
    return pid


def new_node(owner: Conn, person: uuid.UUID, kind: str = "DESKTOP") -> uuid.UUID:
    row = owner.execute(
        "INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)"
        " VALUES (%s, %s, 'pc', %s, %s) RETURNING id",
        (person, kind, secrets.token_bytes(32), f"ts:pc-{secrets.token_hex(4)}"),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


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
) -> uuid.UUID:
    """A `conv.inbound` row as `conv.record_inbound` writes it (person from the node)."""
    iid = uuid.uuid4()
    owner.execute(
        "INSERT INTO conv.inbound (id, node_id, person_id, kind, payload, received_at)"
        " VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()))",
        (iid, node, person, kind, Jsonb(payload), received_at),
    )
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
            {"reply_id": str(row[0]), "seq": row[1], "result": result,
             "evidence": {"files": files}},
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
