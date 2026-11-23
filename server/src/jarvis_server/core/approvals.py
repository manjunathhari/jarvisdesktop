"""The approval flow (spec §9.3, §9.4; R10-1, R11-1, R12-21, R12-22; P2-A1).

**Proposing (`propose_delete`, in the turn).** Before anything is proposed, core lists the test
folder on the requesting desktop as a live action and waits for the listing (10 s). The wait
holds no lock and no open transaction: it runs on the turn's committing connection
(`budget.committing_connection`, autocommit), and each poll is a single READ COMMITTED
statement on `conv.inbound`, so it sees the gateway's `conv.record_inbound` row as soon as the
gateway's own transaction has committed. The `action.receipt` job that row enqueues later only
records the receipt's outcome. Time for the wait comes from `set_wait` (tests), else
`time.monotonic` / `time.sleep`. A file that isn't listed proposes nothing (None).

Then, in **one transaction**: the task through Atlas (one plan, one task, ADR-014), the
`conv.session_ticket` linking it to the session (v2 §8.4), the move to APPROVAL_PENDING, the
approval (10-minute expiry; its `card_authz` is the core-signed `approval.card`, carrying the
listing's name, size, SHA-256 and file ID; creation is audited by the database, R12-21), the
URGENT `approval.requested` card, and the scheduled `approval.expiring` (2 minutes before) and
`approval.expire` jobs.

**Deciding (job `approval.decision`).** In the job's one transaction, under `SELECT … FOR
UPDATE` on the approval, the §9.3 step 6 checks run in the fixed order of R12-21:
`unknown_approval`, `bad_card_sig`, `other_person`, `node_revoked`, `not_pending`, `expired`.
Expiry is checked here, before any decision UPDATE, so the backstop trigger never raises inside
a transaction that holds a refusal. A failed check: the `approval.decision_refused` item to the
deciding node's person with the approval's real state, the inbound's REFUSED outcome, and last
`ledger.refuse_decision` through `audited_call_in_job` (R11-1); then the handler returns, and
dispatch commits the refusal with the job. Nothing is decided or consumed.

On success: the decision (audited by its trigger), the open card superseded, and for APPROVED
the task to READY, `ledger.consume_approval` (a refusal is kept the same way: the handler
returns), IN_PROGRESS, the `ledger.action` row keyed by the execution key (one receipt per key),
and the signed `node.action` as an URGENT `action.approved` item targeted at the requesting node
only (R12-23), valid until the earlier of now + 2 min and the approval's expiry. DENIED cancels
the task. Every audited call in a handler goes through `audited_call_in_job`; no refusal is
ever turned into an exception.

**Scheduled jobs.** `approval.expiring` re-surfaces a PENDING card 2 minutes before expiry;
`approval.expire` marks a PENDING approval EXPIRED once it is past `expires_at`, expires its
card, moves the task to EXPIRED and notifies `approval.expired`.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from jarvis_server import comm, jobs, ledger, signing
from jarvis_server.atlas import ApprovedPlan, Atlas
from jarvis_server.contracts import ActorKind, EntityType, TaskState
from jarvis_server.core import actions, dispatch, keys, rules
from jarvis_server.dbcall import audited_call_in_job
from jarvis_server.ledger import Actor
from jarvis_server.policy import Tier

log = logging.getLogger("jarvis.core.approvals")

APPROVAL_EXPIRY = timedelta(minutes=10)
RESURFACE_BEFORE = timedelta(minutes=2)
LISTING_TIMEOUT = 10.0  # seconds
POLL_INTERVAL = 0.05  # seconds
LISTING_OK = "OK"
CORE_ACTOR = Actor(ActorKind.AGENT, "core")
ATLAS = Atlas()

CARD_KINDS = ("approval.requested", "approval.expiring")
_OPEN = ("PENDING", "DELIVERING", "DELIVERED")

# R12-21, in check order.
DECISION_REFUSALS = (
    "unknown_approval",
    "bad_card_sig",
    "other_person",
    "node_revoked",
    "not_pending",
    "expired",
)
_STATE_TEXT = {
    "PENDING": "It is still waiting for a decision.",
    "APPROVED": "It was already approved.",
    "DENIED": "It was already denied.",
    "EXPIRED": "It has expired.",
}

_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


def set_wait(clock: Callable[[], float] | None, sleep: Callable[[float], None] | None) -> None:
    """Tests: the clock and sleep the listing wait uses (None: the real ones)."""
    global _clock, _sleep
    _clock = clock or time.monotonic
    _sleep = sleep or time.sleep


class ListingUnavailable(Exception):
    """No usable listing within the timeout (none, late, or malformed); nothing proposed."""


# --- proposing ---------------------------------------------------------------------------------

_RECEIPT = """
SELECT payload, received_at <= %(valid_until)s FROM conv.inbound
WHERE kind = 'action.receipt' AND node_id = %(node)s AND person_id = %(person)s
  AND payload->>'reply_id' = %(reply)s AND payload->'seq' = to_jsonb(%(seq)s::integer)
ORDER BY received_at, id LIMIT 1
"""


def _await_listing(
    conn: psycopg.Connection[Any],
    *,
    node_id: UUID,
    person_id: UUID,
    reply_id: UUID,
    seq: int,
    valid_until: datetime,
) -> dict[str, dict[str, Any]]:
    deadline = _clock() + LISTING_TIMEOUT
    while True:
        row = conn.execute(
            _RECEIPT,
            {"valid_until": valid_until, "node": node_id, "person": person_id,
             "reply": str(reply_id), "seq": seq},
        ).fetchone()  # fmt: skip
        if row is not None:
            payload, in_time = row
            listing = actions.parse_listing(payload.get("evidence"))
            if not in_time or payload.get("result") != LISTING_OK or listing is None:
                raise ListingUnavailable("late or unusable listing")
            return listing
        if _clock() >= deadline:
            raise ListingUnavailable("no listing within the timeout")
        _sleep(POLL_INTERVAL)


def propose_delete(
    conn: psycopg.Connection[Any],
    *,
    session_id: UUID,
    person_id: UUID,
    node_id: UUID,
    name: str,
    now: datetime,
) -> UUID | None:
    """List, then propose deleting `name` (§9.3 steps 2 to 4): the approval's ID, or None if the
    file isn't listed. Raises `ListingUnavailable` if no usable listing came in time. `conn`
    must be an idle autocommit connection (the turn's committing connection)."""
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("propose_delete waits; it needs an idle autocommit connection")
    if not actions.NAME.fullmatch(name):
        return None  # never a path, a stream or a device name (§9.4)
    reply_id = uuid.uuid4()
    seq = actions.issue_live(
        conn, reply_id=reply_id, session_id=session_id, target_node_id=node_id,
        person_id=person_id, action_kind=actions.LISTING, args={}, now=now,
    )  # fmt: skip
    listing = _await_listing(
        conn, node_id=node_id, person_id=person_id, reply_id=reply_id, seq=seq,
        valid_until=now + actions.LIVE_VALIDITY,
    )  # fmt: skip
    file = listing.get(name)
    if file is None:
        return None
    approval_id = uuid.uuid4()
    expires_at = now + APPROVAL_EXPIRY
    summary = f"Delete test file `{name}`?"
    card = actions.sign(
        actions.authz_object(
            kind="approval.card", action_kind=actions.DELETE, args=file,
            target_node_id=node_id, person_id=person_id, issued_at=now,
            valid_until=expires_at, approval_id=approval_id, summary=summary,
        )
    )  # fmt: skip
    with conn.transaction():
        [task_id] = ATLAS.accept_plan(
            conn,
            ApprovedPlan(
                title=f"Delete test file {name}",
                definition_of_done="The file is in the Recycle Bin, confirmed by a fresh listing",
                source_ref=f"session:{session_id}",
                owner_person_id=person_id,
            ),
            by=CORE_ACTOR,
        )
        conn.execute(
            "INSERT INTO conv.session_ticket (session_id, task_id) VALUES (%s, %s)",
            (session_id, task_id),
        )
        ledger.transition(
            conn, EntityType.TASK, task_id, TaskState.APPROVAL_PENDING, CORE_ACTOR,
            "approval_requested",
        )  # fmt: skip
        conn.execute(
            "INSERT INTO ledger.approval (id, task_id, requested_from_person_id,"
            " requesting_node_id, tier, action_kind, action_args, summary, card_authz,"
            " expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (approval_id, task_id, person_id, node_id, Tier.IRREVERSIBLE.value, actions.DELETE,
             Jsonb(file), summary, Jsonb(card), expires_at),
        )  # fmt: skip
        event_id = comm.record_event(
            conn, "approval.requested", subject_type="approval", subject_id=str(approval_id),
            person_id=person_id, details={"task_id": str(task_id)},
        )  # fmt: skip
        rules.notify(
            conn, "approval.requested", person_id=person_id, event_id=event_id,
            context={"expires_at": expires_at, "authz": card, "text": summary,
                     "group_key": f"approval:{approval_id}"},
        )  # fmt: skip
        payload = {"approval_id": str(approval_id)}
        delay = expires_at - now
        jobs.enqueue(conn, "approval.expiring", payload, delay=delay - RESURFACE_BEFORE)
        jobs.enqueue(conn, "approval.expire", payload, delay=delay)
    return approval_id


# --- deciding ----------------------------------------------------------------------------------


def _open_cards(conn: psycopg.Connection[Any], approval_id: UUID) -> list[UUID]:
    rows = conn.execute(
        "SELECT o.id FROM comm.outbox o JOIN comm.event e ON e.id = o.event_id"
        " WHERE e.subject_type = 'approval' AND e.subject_id = %s AND o.kind = ANY(%s)"
        " AND o.state = ANY(%s) ORDER BY o.created_at, o.id",
        (str(approval_id), list(CARD_KINDS), list(_OPEN)),
    ).fetchall()
    return [UUID(str(r[0])) for r in rows]


def _card_verifies(card: Any, approval_id: UUID) -> bool:
    """The stored card is a valid `core-authz` from a trusted core key, for this approval."""
    key, key_id = keys.active_core_key()
    try:
        obj = signing.check_authz(card, {key_id: signing.public_bytes(key)})
    except signing.AuthzInvalid:
        return False
    return obj.get("kind") == "approval.card" and obj.get("approval_id") == str(approval_id)


def _check(
    conn: psycopg.Connection[Any],
    approval: Sequence[Any] | None,
    approval_id: UUID,
    *,
    node_id: UUID,
    person_id: UUID,
    card_sig: Any,
) -> str | None:
    """The first failing §9.3 step-6 check (R12-21 order), or None."""
    if approval is None:
        return "unknown_approval"
    _, requested_from, _, card, decision, expired = approval[:6]
    if card_sig != card.get("sig") or not _card_verifies(card, approval_id):
        return "bad_card_sig"
    if requested_from != person_id or card["object"].get("person_id") != str(person_id):
        return "other_person"
    revoked = conn.execute("SELECT revoked_at FROM gw.node WHERE id = %s", (node_id,)).fetchone()
    if revoked is None or revoked[0] is not None:
        return "node_revoked"
    if decision != "PENDING":
        return "not_pending"
    if expired:
        return "expired"
    return None


def _refuse(
    conn: psycopg.Connection[Any],
    *,
    inbound_id: UUID,
    approval_id: UUID,
    node_id: UUID,
    person_id: UUID,
    code: str,
    decision: str | None,
) -> None:
    """Tell the deciding node's person, mark the inbound, then the audited refusal, last."""
    if decision is None:
        text = "That approval doesn't exist."
    elif code == "expired":
        text = "That approval has expired."
    else:
        text = f"That decision could not be applied. {_STATE_TEXT[decision]}"
    event_id = comm.record_event(
        conn, "approval.decision_refused", subject_type="approval",
        subject_id=str(approval_id), person_id=person_id, details={"reason": code},
    )  # fmt: skip
    rules.notify(
        conn, "approval.decision_refused", person_id=person_id, event_id=event_id,
        context={"text": text},
    )  # fmt: skip
    conn.execute(
        "UPDATE conv.inbound SET outcome = 'REFUSED' WHERE id = %s AND outcome IS NULL",
        (inbound_id,),
    )
    audited_call_in_job(
        conn, "SELECT ledger.refuse_decision(%s, %s, %s)", (approval_id, node_id, code)
    )
    log.info("decision on approval %s refused: %s", approval_id, code)


def _answered(conn: psycopg.Connection[Any], inbound_id: UUID, outcome: str = "ANSWERED") -> None:
    conn.execute(
        "UPDATE conv.inbound SET outcome = %s WHERE id = %s AND outcome IS NULL",
        (outcome, inbound_id),
    )


@dispatch.register("approval.decision")
def on_decision(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Handler of `approval.decision`, payload `{inbound_id}` (§9.3 step 6)."""
    inbound_id = UUID(job.payload["inbound_id"])
    row = conn.execute(
        "SELECT node_id, person_id, kind, payload, outcome FROM conv.inbound"
        " WHERE id = %s FOR UPDATE",
        (inbound_id,),
    ).fetchone()
    if row is None or row[4] is not None:
        return  # pruned, or handled by an earlier run
    node_id, person_id, kind, payload = UUID(str(row[0])), UUID(str(row[1])), row[2], row[3]
    if kind != "approval.decision":
        raise ValueError(f"inbound {inbound_id} is a {kind}, not an approval.decision")
    approval_id = UUID(str(payload["approval_id"]))
    decision = str(payload["decision"])
    approval = conn.execute(
        "SELECT task_id, requested_from_person_id, requesting_node_id, card_authz, decision,"
        " now() >= expires_at, action_args, expires_at, least(now() + %s, expires_at)"
        " FROM ledger.approval WHERE id = %s FOR UPDATE",
        (actions.LIVE_VALIDITY, approval_id),
    ).fetchone()
    code = _check(
        conn, approval, approval_id, node_id=node_id, person_id=person_id,
        card_sig=payload.get("card_sig"),
    )  # fmt: skip
    if code is not None or approval is None:
        _refuse(
            conn, inbound_id=inbound_id, approval_id=approval_id, node_id=node_id,
            person_id=person_id, code=code or "unknown_approval",
            decision=None if approval is None else str(approval[4]),
        )  # fmt: skip
        return  # R11-1: the refusal is kept; nothing more
    task_id = UUID(str(approval[0]))
    requesting_node = UUID(str(approval[2]))
    args: dict[str, Any] = approval[6]
    expires_at: datetime = approval[7]
    valid_until: datetime = approval[8]
    conn.execute(
        "UPDATE ledger.approval SET decision = %s, decided_at = now(), decided_by_node_id = %s"
        " WHERE id = %s",
        (decision, node_id, approval_id),
    )
    for card_item in _open_cards(conn, approval_id):
        audited_call_in_job(conn, "SELECT comm.supersede(%s)", (card_item,))
    if decision == "DENIED":
        _answered(conn, inbound_id)
        if actions.transition_in_job(conn, task_id, "CANCELLED", "denied"):
            session_id = actions.ticket_session(conn, task_id)
            if session_id is not None:
                actions.speak(conn, session_id, f"Okay, I won't delete {args.get('name')}.")
        return
    if not actions.transition_in_job(conn, task_id, "READY", "approved"):
        _answered(conn, inbound_id, "REFUSED")
        return
    key, refusal = audited_call_in_job(
        conn, "SELECT execution_key, refusal FROM ledger.consume_approval(%s)", (approval_id,)
    )
    if refusal is not None:  # kept with its audit row (R10-1); nothing is issued
        _answered(conn, inbound_id, "REFUSED")
        return
    execution_key = UUID(str(key))
    if not actions.transition_in_job(conn, task_id, "IN_PROGRESS", "issued"):
        _answered(conn, inbound_id, "REFUSED")
        return
    conn.execute(
        "INSERT INTO ledger.action (task_id, actor_kind, actor_id, idempotency_key, status,"
        " source_ref) VALUES (%s, 'AGENT', 'core', %s, 'ISSUED', %s)",
        (task_id, str(execution_key), f"approval:{approval_id}"),
    )
    db_now = actions.db_now(conn)
    authz = actions.sign(
        actions.authz_object(
            kind="node.action", action_kind=actions.DELETE, args=args,
            target_node_id=requesting_node, person_id=person_id, issued_at=db_now,
            valid_until=valid_until, approval_id=approval_id, execution_key=execution_key,
        )
    )  # fmt: skip
    event_id = comm.record_event(
        conn, "action.approved", subject_type="approval", subject_id=str(approval_id),
        person_id=person_id, details={"task_id": str(task_id)},
    )  # fmt: skip
    rules.notify(
        conn, "action.approved", person_id=person_id, event_id=event_id,
        context={"authz": authz, "target_node_id": requesting_node, "expires_at": valid_until,
                 "text": f"Deleting {args.get('name')}", "group_key": f"action:{approval_id}"},
    )  # fmt: skip
    _answered(conn, inbound_id)
    log.info("approval %s: action issued (expires %s)", approval_id, expires_at)


# --- scheduled ---------------------------------------------------------------------------------


@dispatch.register("approval.expiring")
def on_expiring(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Re-surface a PENDING card 2 minutes before it expires (§8.1)."""
    approval_id = UUID(job.payload["approval_id"])
    row = conn.execute(
        "SELECT requested_from_person_id, card_authz, summary, expires_at, decision,"
        " now() < expires_at FROM ledger.approval WHERE id = %s FOR UPDATE",
        (approval_id,),
    ).fetchone()
    if row is None or row[4] != "PENDING" or not row[5]:
        return
    person_id, card, summary, expires_at = UUID(str(row[0])), row[1], row[2], row[3]
    cards = _open_cards(conn, approval_id)
    event_id = comm.record_event(
        conn, "approval.expiring", subject_type="approval", subject_id=str(approval_id),
        person_id=person_id, details={},
    )  # fmt: skip
    context: rules.NotifyContext = {
        "expires_at": expires_at, "authz": card, "text": f"{summary} Expires in 2 minutes.",
        "group_key": f"approval:{approval_id}",
    }  # fmt: skip
    if cards:
        context["supersedes_id"] = cards[-1]
    rules.notify(conn, "approval.expiring", person_id=person_id, event_id=event_id,
                 context=context)  # fmt: skip


@dispatch.register("approval.expire")
def on_expire(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Mark a PENDING approval EXPIRED once it is past its expiry (§5.4)."""
    approval_id = UUID(job.payload["approval_id"])
    row = conn.execute(
        "SELECT requested_from_person_id, task_id, decision, now() >= expires_at,"
        " expires_at - now() FROM ledger.approval WHERE id = %s FOR UPDATE",
        (approval_id,),
    ).fetchone()
    if row is None or row[2] != "PENDING":
        return
    person_id, task_id = UUID(str(row[0])), UUID(str(row[1]))
    if not row[3]:  # ran early: try again at the expiry
        jobs.enqueue(conn, "approval.expire", dict(job.payload), delay=row[4])
        return
    conn.execute("UPDATE ledger.approval SET decision = 'EXPIRED' WHERE id = %s", (approval_id,))
    for card_item in _open_cards(conn, approval_id):
        audited_call_in_job(conn, "SELECT comm.expire(%s)", (card_item,))
    if not actions.transition_in_job(conn, task_id, "EXPIRED", "approval_expired"):
        return
    event_id = comm.record_event(
        conn, "approval.expired", subject_type="approval", subject_id=str(approval_id),
        person_id=person_id, details={"task_id": str(task_id)},
    )  # fmt: skip
    rules.notify(conn, "approval.expired", person_id=person_id, event_id=event_id, context={})
