"""The approval flow, live actions and verified receipts (spec §7.1, §9.3, §9.4; R9-1, R10-1,
R11-1, R12-21 to R12-23; P2-A1 to P2-A6, P2-A9, P2-S4, C3).

Two kinds of test, in one database:
- the decision, receipt and expiry handlers, with the rows the gateway would write planted as
  the owner (as test_turn does) and the listing answered through `approvals.set_wait`;
- P2-A1 end to end, with FakeNode acting as the desktop over the real gateway app, and the core
  jobs run by the test (the turn in its own thread, since it waits for the node's listing).

People are fictional (D11).
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.websockets import WebSocketDisconnect

from jarvis_server import jobs, nodes, policy, signing
from jarvis_server.brain import Speech, ToolCall, budget, health
from jarvis_server.contracts import EntityType
from jarvis_server.core import actions, approvals, dispatch, keys, turn
from jarvis_server.ledger import audit_trail
from jarvis_server.memory import checks
from jarvis_server.memory.engine import SYSTEM, MemoryEngine

from .conftest import Dsns, fresh_database
from .fakes.fake_node import FakeNode
from .fakes.node_rules import Rejected, check_node_action
from .helpers import flow
from .helpers.flow import A_TXT, B_TXT
from .helpers.threads import InThread
from .test_gateway_handshake import MANJ, FakeClock, Gw, running_gateway
from .test_turn import Brains

Conn = psycopg.Connection[Any]


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _reset(owner: Conn, core_key: Any) -> Iterator[None]:
    """No runnable job, brain state or live reservation left by an earlier test; a core key."""
    owner.execute(
        "UPDATE ops.job SET status = 'DONE', locked_by = NULL, locked_until = NULL"
        " WHERE status IN ('QUEUED', 'RUNNING')"
    )
    owner.execute("DELETE FROM ops.heartbeat WHERE process = 'brain'")
    owner.execute("UPDATE ops.brain_reservation SET state = 'RELEASED' WHERE state = 'ACTIVE'")
    owner.execute(
        "UPDATE ops.brain_budget SET reserved_inr = 0, settled_inr = 0, cap_inr = %s",
        (budget.MONTHLY_CAP_PAISE,),
    )
    yield
    approvals.set_wait(None, None)
    turn.set_brain(None)
    turn.set_memory(None)
    health.set_probe_brain(None)


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    return flow.new_person(owner)


@pytest.fixture
def node(owner: Conn, person: uuid.UUID) -> uuid.UUID:
    return flow.new_node(owner, person)


@pytest.fixture
def session(app: Conn, person: uuid.UUID) -> uuid.UUID:
    return flow.open_session(app, person)


@pytest.fixture
def proposed(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> uuid.UUID:
    approval_id = flow.propose(app, owner, node=node, person=person, session=session)
    assert approval_id is not None
    return approval_id


def _issued(app: Conn, person: uuid.UUID) -> list[dict[str, Any]]:
    return flow.items(app, person, "action.approved")


# --- proposing (§9.3 steps 2 to 4) ------------------------------------------------------------


def test_proposal_stores_the_listed_file_and_signs_the_card(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID, proposed: uuid.UUID,
    core_key: Ed25519PrivateKey,
) -> None:  # fmt: skip
    row = flow.approval_row(app, proposed)
    assert row["action_args"] == A_TXT and row["tier"] == "IRREVERSIBLE"
    assert row["action_kind"] == "pc.delete_test_file" and row["decision"] == "PENDING"
    assert row["summary"] == "Delete test file `a.txt`?"
    assert row["expires_at"] - flow.db_now(app) <= timedelta(minutes=10)
    key_id = signing.fingerprint(signing.public_bytes(core_key))
    card = signing.check_authz(row["card_authz"], {key_id: signing.public_bytes(core_key)})
    assert card["kind"] == "approval.card" and card["approval_id"] == str(proposed)
    assert card["args"] == A_TXT and card["target_node_id"] == str(node)
    assert card["execution_key"] is None and card["person_id"] == str(person)
    assert datetime.fromisoformat(card["valid_until"]) == row["expires_at"]
    [item] = flow.items(app, person, "approval.requested")
    assert item["priority"] == "URGENT" and item["authz"] == row["card_authz"]
    assert item["expires_at"] == row["expires_at"] and item["target_node_id"] is None
    assert flow.task_state(app, proposed) == "APPROVAL_PENDING"
    scheduled = app.execute(
        "SELECT kind, round(extract(epoch FROM run_at - %s)) FROM ops.job"
        " WHERE payload->>'approval_id' = %s ORDER BY kind",
        (row["expires_at"] - timedelta(minutes=10), str(proposed)),
    ).fetchall()
    assert [(k, int(s)) for k, s in scheduled] == [("approval.expire", 600),
                                                   ("approval.expiring", 480)]  # fmt: skip


def test_delete_proposal_links_ticket_to_session(
    app: Conn, session: uuid.UUID, proposed: uuid.UUID
) -> None:
    task_id = flow.approval_row(app, proposed)["task_id"]
    rows = app.execute(
        "SELECT session_id FROM conv.session_ticket WHERE task_id = %s", (task_id,)
    ).fetchall()
    assert rows == [(session,)]


def test_unlisted_file_proposes_nothing(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    before = app.execute("SELECT count(*) FROM ledger.approval").fetchone()
    assert flow.propose(app, owner, node=node, person=person, session=session,
                        files=[B_TXT]) is None  # fmt: skip
    for bad in ("../a.txt", "a:b", "C:\\a.txt", ".hidden"):  # never even listed
        assert flow.propose(app, owner, node=node, person=person, session=session,
                            name=bad) is None  # fmt: skip
    assert app.execute("SELECT count(*) FROM ledger.approval").fetchone() == before
    assert flow.items(app, person, "approval.requested") == []


def test_listing_timeout_proposes_nothing(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    """The 10 s wait on a fake clock: no receipt, nothing proposed, no real sleeping."""
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    approvals.set_wait(lambda: clock[0], sleep)
    with pytest.raises(approvals.ListingUnavailable):
        approvals.propose_delete(app, session_id=session, person_id=person, node_id=node,
                                 name="a.txt", now=flow.db_now(app))  # fmt: skip
    assert clock[0] >= approvals.LISTING_TIMEOUT
    assert app.execute(
        "SELECT count(*) FROM ledger.approval WHERE requesting_node_id = %s", (node,)
    ).fetchone() == (0,)


def test_live_action_receipt_after_valid_until_refused(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    """C3, core side: a listing that arrives after its signed `valid_until` is never used, and
    the receipt job refuses it."""
    now = flow.db_now(app)
    late = now + actions.LIVE_VALIDITY + timedelta(seconds=1)
    approvals.set_wait(None, flow.planting_listing(owner, node, person, [A_TXT], received_at=late))
    with pytest.raises(approvals.ListingUnavailable):
        approvals.propose_delete(app, session_id=session, person_id=person, node_id=node,
                                 name="a.txt", now=now)  # fmt: skip
    receipt = app.execute(
        "SELECT id FROM conv.inbound WHERE node_id = %s AND kind = 'action.receipt'", (node,)
    ).fetchone()
    assert receipt is not None
    flow.run(app, "action.receipt", {"inbound_id": str(receipt[0])})
    assert flow.outcome_of(app, receipt[0]) == "REFUSED"
    event = app.execute(
        "SELECT details FROM comm.event WHERE kind = 'action.receipt_refused' AND subject_id = %s",
        (str(receipt[0]),),
    ).fetchone()
    assert event == ({"reason": "expired"},)


def test_live_receipt_from_another_node_refused(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    reply = uuid.uuid4()
    seq = actions.issue_live(app, reply_id=reply, session_id=session, target_node_id=node,
                             person_id=person, action_kind="module.news", args={},
                             now=flow.db_now(app))  # fmt: skip
    other = flow.new_node(owner, person)
    receipt = flow.inbound(
        owner,
        other,
        person,
        "action.receipt",
        {"reply_id": str(reply), "seq": seq, "result": "OK", "evidence": {}},
    )
    flow.run(app, "action.receipt", {"inbound_id": str(receipt)})
    assert flow.outcome_of(app, receipt) == "REFUSED"
    ok = flow.inbound(
        owner,
        node,
        person,
        "action.receipt",
        {"reply_id": str(reply), "seq": seq, "result": "OK", "evidence": {}},
    )
    flow.run(app, "action.receipt", {"inbound_id": str(ok)})
    assert flow.outcome_of(app, ok) == "ANSWERED"
    assert [i["priority"] for i in flow.items(app, person, "action.readonly_ok")] == ["SILENT"]


def test_issue_live_chunk(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID, core_key: Any
) -> None:
    reply = uuid.uuid4()
    now = flow.db_now(app)
    assert actions.issue_live(app, reply_id=reply, session_id=session, target_node_id=node,
                              person_id=person, action_kind="screen.clear", args={"tool": "x"},
                              now=now) == 1  # fmt: skip
    assert actions.issue_live(app, reply_id=reply, session_id=session, target_node_id=node,
                              person_id=person, action_kind="screen.clear", args={},
                              now=now) == 2  # fmt: skip
    row = app.execute(
        "SELECT session_id, target_node_id, valid_until, authz FROM conv.stream_chunk"
        " WHERE reply_id = %s AND seq = 1",
        (reply,),
    ).fetchone()
    assert row is not None
    assert row[:3] == (session, node, now + timedelta(minutes=2))
    key_id = signing.fingerprint(signing.public_bytes(core_key))
    obj = signing.check_authz(row[3], {key_id: signing.public_bytes(core_key)})
    assert obj["kind"] == "node.action" and obj["reply_id"] == str(reply)
    assert obj["approval_id"] is None and obj["execution_key"] is None
    assert datetime.fromisoformat(obj["valid_until"]) == now + timedelta(minutes=2)


# --- deciding (§9.3 step 6) ------------------------------------------------------------------


def test_approved_decision_issues_one_targeted_action(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID,
    core_key: Ed25519PrivateKey,
) -> None:  # fmt: skip
    inbound_id, job_id = flow.decide(app, owner, node=node, person=person, approval_id=proposed)
    assert flow.job_status(app, job_id) == "DONE" and flow.outcome_of(app, inbound_id) == "ANSWERED"
    row = flow.approval_row(app, proposed)
    assert row["decision"] == "APPROVED" and row["decided_by_node_id"] == node
    assert row["execution_key"] is not None
    [item] = _issued(app, person)
    assert item["priority"] == "URGENT" and item["target_node_id"] == node
    key_id = signing.fingerprint(signing.public_bytes(core_key))
    obj = signing.check_authz(item["authz"], {key_id: signing.public_bytes(core_key)})
    assert obj["kind"] == "node.action" and obj["action_kind"] == "pc.delete_test_file"
    assert obj["args"] == A_TXT and obj["target_node_id"] == str(node)
    assert obj["execution_key"] == str(row["execution_key"])
    assert obj["approval_id"] == str(proposed) and obj["reply_id"] is None
    valid_until = datetime.fromisoformat(obj["valid_until"])
    assert valid_until <= flow.db_now(app) + timedelta(minutes=2)
    assert valid_until <= row["expires_at"] and item["expires_at"] == valid_until
    assert flow.task_state(app, proposed) == "IN_PROGRESS"
    [card] = flow.items(app, person, "approval.requested")
    assert card["state"] == "SUPERSEDED"
    assert flow.audit(app, proposed) == [
        ("CREATE", "APPLIED", "created", None, "PENDING"),
        ("DECIDE", "APPLIED", "approved", "PENDING", "APPROVED"),
        ("CONSUME", "APPLIED", "consumed", None, None),
    ]
    action = app.execute(
        "SELECT status FROM ledger.action WHERE idempotency_key = %s",
        (str(row["execution_key"]),),
    ).fetchone()
    assert action == ("ISSUED",)


def test_denied_decision_cancels(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID
) -> None:
    flow.decide(app, owner, node=node, person=person, approval_id=proposed, decision="DENIED")
    row = flow.approval_row(app, proposed)
    assert row["decision"] == "DENIED" and row["execution_key"] is None
    assert flow.task_state(app, proposed) == "CANCELLED"
    assert _issued(app, person) == []


def test_no_approval_refused(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    """P2-A2: the policy never allows an IRREVERSIBLE action from a turn; a decision on an
    approval that doesn't exist is refused and audited; nothing is issued."""
    decision = policy.decide_tool_call(
        app, person_id=person, session_id=session, node_id=node, node_kind="DESKTOP",
        call=ToolCall("pc.delete_test_file", {"name": "a.txt"}), memory_ids=[],
    )  # fmt: skip
    assert decision == policy.NeedsApproval()
    missing = uuid.uuid4()
    inbound_id, job_id = flow.decide(app, owner, node=node, person=person, approval_id=missing,
                                     sig="A" * 86)  # fmt: skip
    assert flow.job_status(app, job_id) == "DONE" and flow.outcome_of(app, inbound_id) == "REFUSED"
    assert flow.audit(app, missing) == [("DECIDE", "REJECTED", "unknown_approval", None, None)]
    assert _issued(app, person) == []


def test_expired_approval_refused(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    """P2-A3: the clock is past the expiry. The decision is refused (`expired`), nothing is
    decided or issued; the expiry job then marks it EXPIRED."""
    then = flow.db_now(app) - timedelta(minutes=11)
    approval_id = flow.propose(app, owner, node=node, person=person, session=session, now=then)
    assert approval_id is not None
    inbound_id, job_id = flow.decide(app, owner, node=node, person=person,
                                     approval_id=approval_id)  # fmt: skip
    assert flow.job_status(app, job_id) == "DONE" and flow.outcome_of(app, inbound_id) == "REFUSED"
    row = flow.approval_row(app, approval_id)
    assert (row["decision"], row["execution_key"]) == ("PENDING", None)
    assert flow.audit(app, approval_id)[-1] == ("DECIDE", "REJECTED", "expired", "PENDING", None)
    flow.run(app, "approval.expire", {"approval_id": str(approval_id)})
    assert flow.approval_row(app, approval_id)["decision"] == "EXPIRED"
    assert flow.task_state(app, approval_id) == "EXPIRED"
    assert flow.audit(app, approval_id)[-1] == ("DECIDE", "APPLIED", "expired", "PENDING",
                                                "EXPIRED")  # fmt: skip
    [card] = flow.items(app, person, "approval.requested")
    assert card["state"] == "EXPIRED"
    [told] = flow.items(app, person, "approval.expired")
    assert told["priority"] == "TODAY"
    assert _issued(app, person) == []


def test_handler_returns_immediately_after_kept_refusal(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    """R11-1: on an expired approval the refusal is recorded and is the job's last write: no
    decision UPDATE, no node.action, no outbox item after it."""
    then = flow.db_now(app) - timedelta(minutes=11)
    approval_id = flow.propose(app, owner, node=node, person=person, session=session, now=then)
    assert approval_id is not None
    since = int(app.execute("SELECT max(seq) FROM audit.event").fetchone()[0])  # type: ignore[index]
    outbox_before = {i["id"] for i in flow.items(app, person, "approval.requested")}
    flow.decide(app, owner, node=node, person=person, approval_id=approval_id)
    rows = app.execute(
        "SELECT entity_type, action::text, outcome::text, reason FROM audit.event WHERE seq > %s"
        " ORDER BY seq",
        (since,),
    ).fetchall()
    assert rows[-1] == ("approval", "DECIDE", "REJECTED", "expired")  # the last write
    assert not [r for r in rows if r[0] == "approval" and r[2] == "APPLIED"]
    assert not [r for r in rows if r[0] == "task"]
    new_items = app.execute(
        "SELECT kind, body->>'text' FROM comm.outbox WHERE person_id = %s AND NOT (id = ANY(%s))",
        (person, list(outbox_before)),
    ).fetchall()
    assert new_items == [("approval.decision_refused", "That approval has expired.")]
    assert flow.approval_row(app, approval_id)["decided_at"] is None


def test_concurrent_decisions_one_action(
    dsns: Dsns, app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID
) -> None:
    """P2-A5: two decision jobs for one approval, the second waiting on the first's row lock:
    one node.action, one execution key; the second is refused `not_pending`."""
    sig = flow.card_sig(app, proposed)
    first, second = (
        flow.inbound(owner, node, person, "approval.decision",
                     {"approval_id": str(proposed), "decision": "APPROVED", "card_sig": sig})
        for _ in range(2)
    )  # fmt: skip
    jobs_ = [
        jobs.enqueue(app, "approval.decision", {"inbound_id": str(i)}) for i in (first, second)
    ]
    claimed = [flow.claim(app, j) for j in jobs_]
    gate = threading.Event()
    real = approvals._check

    def held_check(*args: Any, **kw: Any) -> Any:
        result = real(*args, **kw)
        gate.wait(10)  # job 1 holds the approval's row lock here
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(approvals, "_check", held_check)
        one = InThread(dsns.app, lambda c: dispatch.handle(c, claimed[0]))
        two = InThread(dsns.app, lambda c: dispatch.handle(c, claimed[1]))
        blocked = two.wait_until_blocked(dsns.superuser)  # on the row lock job 1 holds
        assert blocked in ("tuple", "transactionid")
        gate.set()
        one.join()
        two.join()
    outcomes = sorted(str(flow.outcome_of(app, i)) for i in (first, second))
    assert outcomes == ["ANSWERED", "REFUSED"]
    assert len(_issued(app, person)) == 1
    rows = flow.audit(app, proposed)
    assert [r for r in rows if r[0] == "CONSUME"] == [("CONSUME", "APPLIED", "consumed", None,
                                                       None)]  # fmt: skip
    assert ("DECIDE", "REJECTED", "not_pending", "APPROVED", None) in rows


def _wrong_sig(app: Conn, approval_id: uuid.UUID) -> str:
    sig = flow.card_sig(app, approval_id)
    return ("B" if sig[0] != "B" else "C") + sig[1:]


@pytest.mark.parametrize(
    "case", ["bad_card_sig", "forged_card", "other_person", "node_revoked", "not_pending",
             "expired", "unknown_approval"],
)  # fmt: skip
def test_every_approval_refusal_follows_the_rule(
    case: str, dsns: Dsns, app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID,
    session: uuid.UUID, core_key: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """P2-A9 and R11-1: each §9.3 check refuses; the job is DONE with the REJECTED row kept
    (seen from a separate connection); nothing is decided, consumed or issued."""
    then = flow.db_now(app) - timedelta(minutes=11) if case == "expired" else None
    approval_id = flow.propose(app, owner, node=node, person=person, session=session, now=then)
    assert approval_id is not None
    sender, who, sig, target = node, person, flow.card_sig(app, approval_id), approval_id
    code = case
    if case == "bad_card_sig":
        sig = _wrong_sig(app, approval_id)
    elif case == "forged_card":  # the sig matches the stored card, but no trusted key made it
        code = "bad_card_sig"
        monkeypatch.setattr(keys, "active_core_key", lambda: (Ed25519PrivateKey.generate(),
                                                              "a" * 16))  # fmt: skip
    elif case == "other_person":
        who = flow.new_person(owner, "Ravi Fictional")
        sender = flow.new_node(owner, who)
    elif case == "node_revoked":
        sender = flow.new_node(owner, person)
        nodes.revoke(app, sender)
    elif case == "not_pending":
        flow.decide(app, owner, node=node, person=person, approval_id=approval_id,
                    decision="DENIED")  # fmt: skip
    elif case == "unknown_approval":
        target = uuid.uuid4()
    before = flow.approval_row(app, approval_id)
    inbound_id, job_id = flow.decide(app, owner, node=sender, person=who, approval_id=target,
                                     sig=sig)  # fmt: skip
    assert flow.job_status(app, job_id) == "DONE"
    assert flow.outcome_of(app, inbound_id) == "REFUSED"
    with psycopg.connect(dsns.app, autocommit=True) as other:
        last = flow.audit(other, target)[-1]
    assert last[:3] == ("DECIDE", "REJECTED", code)
    after = flow.approval_row(app, approval_id)
    assert (after["decision"], after["execution_key"], after["decided_at"]) == (
        before["decision"], None, before["decided_at"],
    )  # fmt: skip
    assert _issued(app, person) == [] and _issued(app, who) == []
    [told] = flow.items(app, who, "approval.decision_refused")
    assert told["priority"] == "URGENT"


@pytest.mark.parametrize("refusal", ["already_consumed", "not_approved", "unknown_approval"])
def test_consume_refusal_in_the_handler_is_kept(
    refusal: str, dsns: Dsns, app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID,
    proposed: uuid.UUID, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """R10-1 inside the handler: a refusal from `consume_approval` is committed with its audit
    row and the handler returns (no node.action, no `ledger.action`). The §9.3 checks run under
    the same row lock first, so here `consume_approval` is pointed at an approval in the
    refusing state, as a retried job after a crash would meet it (already_consumed)."""
    other = None
    if refusal != "unknown_approval":
        other = flow.propose(app, owner, node=node, person=person,
                             session=flow.open_session(app, person))  # fmt: skip
        assert other is not None
        if refusal == "already_consumed":
            flow.decide(app, owner, node=node, person=person, approval_id=other)
    target = other or uuid.uuid4()
    real = approvals.audited_call_in_job

    def redirected(conn: Conn, sql: Any, params: Any) -> tuple[Any, ...]:
        if "consume_approval" in sql:
            params = (target,)
        return real(conn, sql, params)

    monkeypatch.setattr(approvals, "audited_call_in_job", redirected)
    actions_before = app.execute("SELECT count(*) FROM ledger.action").fetchone()
    issued_before = len(_issued(app, person))
    inbound_id, job_id = flow.decide(app, owner, node=node, person=person, approval_id=proposed)
    assert flow.job_status(app, job_id) == "DONE"
    assert flow.outcome_of(app, inbound_id) == "REFUSED"
    with psycopg.connect(dsns.app, autocommit=True) as c:
        assert flow.audit(c, target)[-1][:3] == ("CONSUME", "REJECTED", refusal)
    assert app.execute("SELECT count(*) FROM ledger.action").fetchone() == actions_before
    assert len(_issued(app, person)) == issued_before


def test_later_job_failure_cannot_erase_committed_refusal(
    dsns: Dsns, app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    _, first = flow.decide(app, owner, node=node, person=person, approval_id=proposed,
                           sig=_wrong_sig(app, proposed))  # fmt: skip
    assert flow.job_status(app, first) == "DONE"

    def broken() -> Any:
        raise RuntimeError("core_signing_key is unreadable")

    monkeypatch.setattr(keys, "active_core_key", broken)
    with pytest.raises(RuntimeError):
        flow.decide(app, owner, node=node, person=person, approval_id=proposed)
    with psycopg.connect(dsns.app, autocommit=True) as other:
        rows = flow.audit(other, proposed)
    assert [r for r in rows if r[1] == "REJECTED"] == [
        ("DECIDE", "REJECTED", "bad_card_sig", "PENDING", None)
    ]
    assert flow.approval_row(app, proposed)["decision"] == "PENDING"  # job 2 rolled back


def test_expiring_resurfaces_a_pending_card(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID
) -> None:
    flow.run(app, "approval.expiring", {"approval_id": str(proposed)})
    [card] = flow.items(app, person, "approval.requested")
    [again] = flow.items(app, person, "approval.expiring")
    assert card["state"] == "SUPERSEDED" and again["supersedes_id"] == card["id"]
    assert again["priority"] == "URGENT" and again["authz"] == card["authz"]
    flow.decide(app, owner, node=node, person=person, approval_id=proposed)
    assert flow.items(app, person, "approval.expiring")[0]["state"] == "SUPERSEDED"
    flow.run(app, "approval.expiring", {"approval_id": str(proposed)})  # decided: nothing more
    assert len(flow.items(app, person, "approval.expiring")) == 1


# --- receipts (§9.4) ------------------------------------------------------------------------


def _approve(app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID,
             approval_id: uuid.UUID) -> str:  # fmt: skip
    flow.decide(app, owner, node=node, person=person, approval_id=approval_id)
    return str(flow.approval_row(app, approval_id)["execution_key"])


def _receipt(owner: Conn, node: uuid.UUID, person: uuid.UUID, key: str, result: str = "RECYCLED",
             **evidence: Any) -> uuid.UUID:  # fmt: skip
    ev = {"file_id": A_TXT["file_id"], "sha256": A_TXT["sha256"],
          "recycled_at": datetime.now(UTC).isoformat(), **evidence}  # fmt: skip
    return flow.inbound(
        owner,
        node,
        person,
        "action.receipt",
        {"execution_key": key, "result": result, "evidence": ev},
    )


def _verification_listing(app: Conn, node: uuid.UUID) -> tuple[uuid.UUID, int]:
    row = app.execute(
        "SELECT reply_id, seq FROM conv.stream_chunk WHERE target_node_id = %s AND type = 'ACTION'"
        " ORDER BY created_at DESC LIMIT 1",
        (node,),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0])), int(row[1])


def test_receipt_then_fresh_listing_completes(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID,
    proposed: uuid.UUID,
) -> None:  # fmt: skip
    key = _approve(app, owner, person, node, proposed)
    receipt = _receipt(owner, node, person, key)
    flow.run(app, "action.receipt", {"inbound_id": str(receipt)})
    assert flow.task_state(app, proposed) == "IN_PROGRESS"  # not done before the listing
    reply, seq = _verification_listing(app, node)
    listing = flow.inbound(owner, node, person, "action.receipt",
                           {"reply_id": str(reply), "seq": seq, "result": "OK",
                            "evidence": {"files": [B_TXT]}})  # fmt: skip
    flow.run(app, "action.receipt", {"inbound_id": str(listing)})
    assert flow.task_state(app, proposed) == "COMPLETED"
    outcome = app.execute(
        "SELECT o.verified, o.verified_at IS NOT NULL, a.status FROM ledger.outcome o"
        " JOIN ledger.action a ON a.id = o.action_id WHERE a.idempotency_key = %s",
        (key,),
    ).fetchone()
    assert outcome == (True, True, "VERIFIED")
    spoken = app.execute(
        "SELECT text FROM conv.stream_chunk WHERE session_id = %s AND type = 'SPEECH'"
        " ORDER BY created_at DESC LIMIT 1",
        (session,),
    ).fetchone()
    assert spoken == ("Done: a.txt is in the Recycle Bin.",)


def test_one_receipt_per_key(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, proposed: uuid.UUID
) -> None:
    key = _approve(app, owner, person, node, proposed)
    first = _receipt(owner, node, person, key)
    flow.run(app, "action.receipt", {"inbound_id": str(first)})
    again = _receipt(owner, node, person, key)
    flow.run(app, "action.receipt", {"inbound_id": str(again)})
    assert flow.outcome_of(app, again) == "REFUSED"
    listings = app.execute(
        "SELECT count(*) FROM conv.stream_chunk WHERE target_node_id = %s AND type = 'ACTION'"
        " AND authz->'object'->>'action_kind' = 'pc.list_test_folder'",
        (node,),
    ).fetchone()
    assert listings == (2,)  # the proposal's listing and one verification listing
    stranger = flow.new_node(owner, person)
    other = _receipt(owner, stranger, person, key)
    flow.run(app, "action.receipt", {"inbound_id": str(other)})
    assert flow.outcome_of(app, other) == "REFUSED"  # only the requesting node's receipt counts


@pytest.mark.parametrize("variant", ["file_id", "sha256", "result", "still_listed", "listing_late"])
def test_receipt_mismatch_fails_task(
    variant: str, app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID,
    proposed: uuid.UUID,
) -> None:  # fmt: skip
    """P2-A6: a receipt with a different file ID or hash, a non-RECYCLED result, or a fresh
    listing that still has the file (or comes too late): the task is FAILED, never COMPLETED."""
    key = _approve(app, owner, person, node, proposed)
    extra: dict[str, Any] = {}
    result = "RECYCLED"
    if variant == "file_id":
        extra = {"file_id": B_TXT["file_id"]}
    elif variant == "sha256":
        extra = {"sha256": B_TXT["sha256"]}
    elif variant == "result":
        result = "MISMATCH"
    receipt = _receipt(owner, node, person, key, result, **extra)
    flow.run(app, "action.receipt", {"inbound_id": str(receipt)})
    if variant in ("still_listed", "listing_late"):
        reply, seq = _verification_listing(app, node)
        late = flow.db_now(app) + timedelta(minutes=3) if variant == "listing_late" else None
        listing = flow.inbound(owner, node, person, "action.receipt",
                               {"reply_id": str(reply), "seq": seq, "result": "OK",
                                "evidence": {"files": [A_TXT, B_TXT]}},
                               received_at=late)  # fmt: skip
        flow.run(app, "action.receipt", {"inbound_id": str(listing)})
    assert flow.task_state(app, proposed) == "FAILED"
    task_id = flow.approval_row(app, proposed)["task_id"]
    assert "COMPLETED" not in [e.to_state for e in audit_trail(app, EntityType.TASK, task_id)]
    verified = app.execute(
        "SELECT bool_or(o.verified) FROM ledger.outcome o JOIN ledger.action a"
        " ON a.id = o.action_id WHERE a.idempotency_key = %s",
        (key,),
    ).fetchone()
    assert verified == (False,)


# --- P2-A1 end to end, over the real gateway -----------------------------------------------


@pytest.fixture
def gw(dsns: Dsns, owner: Conn, tmp_path: Path, core_key: Ed25519PrivateKey) -> Iterator[Gw]:
    """A gateway with this test's core key. MANJ's open items from earlier tests (signed by
    their own throwaway core keys) are ended first, so a new node never meets them."""
    owner.execute(
        "UPDATE comm.outbox SET state = 'EXPIRED' WHERE person_id = %s"
        " AND state IN ('PENDING', 'DELIVERING', 'DELIVERED')",
        (MANJ,),
    )
    with running_gateway(dsns, tmp_path, core_key=core_key, clock=FakeClock(datetime.now(UTC)),
                         tick_interval=0.02) as g:  # fmt: skip
        yield g


@pytest.fixture
def manj_memory(tmp_path: Path) -> Iterator[MemoryEngine]:
    directory = checks.StaticDirectory(people={MANJ: flow.HOUSEHOLD})
    eng = MemoryEngine(tmp_path / "memory", directory, checks.ListAudit(), flow.HOUSEHOLD).open()
    eng.add_person("manj", MANJ, "Manj (fictional)", by=SYSTEM)
    turn.set_memory(eng)
    yield eng
    turn.set_memory(None)


def _until(fake: FakeNode, done: Callable[[list[dict[str, Any]]], bool],
           timeout: float = 15.0) -> list[dict[str, Any]]:  # fmt: skip
    out: list[dict[str, Any]] = []
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while not done(out):
        assert datetime.now(UTC) < deadline, [e["kind"] for e in out]
        with contextlib.suppress(TimeoutError):
            out.append(fake.recv(0.05))
    return out


def _actions(envs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in envs if e["kind"] == "node.action"]


def _outbox(envs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [
        e["payload"] for e in envs if e["kind"] == "outbox.item" and e["payload"]["kind"] == kind
    ]


def _spoken(envs: list[dict[str, Any]]) -> list[str]:
    return [e["payload"]["text"] for e in envs
            if e["kind"] == "reply.chunk" and e["payload"]["type"] == "SPEECH"]  # fmt: skip


def _run_turn(dsns: Dsns, app: Conn) -> InThread:
    row = app.execute(
        "SELECT id FROM ops.job WHERE kind = 'conv.turn' AND status = 'QUEUED' ORDER BY id DESC"
        " LIMIT 1"
    ).fetchone()
    assert row is not None
    job = flow.claim(app, int(row[0]))
    return InThread(dsns.app, lambda c: dispatch.handle(c, job), timeout=30)


def test_delete_flow_end_to_end(
    dsns: Dsns, app: Conn, gw: Gw, manj_memory: MemoryEngine, core_key: Ed25519PrivateKey
) -> None:
    """P2-A1: utterance -> Claude proposes the delete -> live listing -> URGENT signed card ->
    APPROVED with the card's sig -> one targeted node.action -> RECYCLED receipt -> fresh
    listing without the file -> COMPLETED, verified, and Jarvis says so."""
    desk = gw.enrolled()
    other = gw.enrolled()  # the same person's second device sees cards, never the action
    for fake in (desk, other):
        fake.sync()
    turn.set_brain(Brains([Speech("Let me check."),
                           ToolCall("pc_delete_test_file", {"name": "a.txt"})]))  # fmt: skip
    desk.send("utterance", {"text": "Delete a.txt from the test folder", "screen_summary": ""})
    desk.sync()
    worker = _run_turn(dsns, app)
    # 1. the live listing, to the desktop that spoke only
    got = _until(desk, lambda e: bool(_actions(e)))
    [listing] = [e["payload"]["authz"]["object"] for e in _actions(got)]
    assert listing["action_kind"] == "pc.list_test_folder"
    assert listing["target_node_id"] == str(desk.node_id) and desk.actions[-1] == listing
    desk.send("action.receipt", {"reply_id": listing["reply_id"], "seq": 1, "result": "OK",
                                 "evidence": {"files": [A_TXT, B_TXT]}})  # fmt: skip
    # 2. the URGENT card, signed by the core key, on both devices
    got += _until(desk, lambda e: bool(_outbox(e, "approval.requested")))
    [card] = _outbox(got, "approval.requested")
    assert card["priority"] == "URGENT"
    core_id = signing.fingerprint(signing.public_bytes(core_key))
    card_obj = signing.check_authz(card["authz"], {core_id: signing.public_bytes(core_key)})
    assert card_obj["kind"] == "approval.card" and card_obj["args"] == A_TXT
    assert _outbox(_until(other, lambda e: bool(_outbox(e, "approval.requested"))),
                   "approval.requested")[0]["outbox_id"] == card["outbox_id"]  # fmt: skip
    worker.join()
    got += _until(desk, lambda e: any("approval" in t for t in _spoken(e)))
    assert "That needs your approval." in "".join(_spoken(got))
    # 3. APPROVED with the card's signature -> the node.action, to the desktop only
    approval_id = card_obj["approval_id"]
    desk.send("approval.decision", {"approval_id": approval_id, "decision": "APPROVED",
                                    "card_sig": card["authz"]["sig"]})  # fmt: skip
    desk.sync()
    assert flow.run_queued(app, "approval.decision") is not None
    got = _until(desk, lambda e: bool(_outbox(e, "action.approved")))
    [item] = _outbox(got, "action.approved")
    action = item["authz"]["object"]
    assert action["kind"] == "node.action" and action["execution_key"] is not None
    assert action["target_node_id"] == str(desk.node_id) and action["args"] == A_TXT
    assert datetime.fromisoformat(action["valid_until"]) <= gw.clock() + timedelta(
        minutes=2, seconds=5
    )
    assert desk.actions[-1] == action  # passed the §7.1 node rules
    assert _outbox(other.sync() + other.drain(0.3), "action.approved") == []
    # 4. RECYCLED with the approved file ID and hash -> a fresh listing
    desk.send("action.receipt", {"execution_key": action["execution_key"], "result": "RECYCLED",
                                 "evidence": {"file_id": A_TXT["file_id"],
                                              "sha256": A_TXT["sha256"],
                                              "recycled_at": gw.clock().isoformat()}})  # fmt: skip
    desk.sync()
    assert flow.run_queued(app, "action.receipt") is not None  # the listing's, from step 1
    assert flow.run_queued(app, "action.receipt") is not None  # the delete's
    got = _until(desk, lambda e: bool(_actions(e)))
    [fresh] = [e["payload"]["authz"]["object"] for e in _actions(got)]
    assert (
        fresh["action_kind"] == "pc.list_test_folder" and fresh["reply_id"] != listing["reply_id"]
    )
    desk.send("action.receipt", {"reply_id": fresh["reply_id"], "seq": 1, "result": "OK",
                                 "evidence": {"files": [B_TXT]}})  # fmt: skip
    desk.sync()
    assert flow.run_queued(app, "action.receipt") is not None
    # 5. COMPLETED, verified, spoken
    got = _until(desk, lambda e: bool(_spoken(e)))
    assert _spoken(got) == ["Done: a.txt is in the Recycle Bin."]
    approval = uuid.UUID(approval_id)
    assert flow.task_state(app, approval) == "COMPLETED"
    task_id = flow.approval_row(app, approval)["task_id"]
    trail = [(e.action, e.to_state) for e in audit_trail(app, EntityType.TASK, task_id)]
    assert trail == [("CREATE", "DETECTED"), ("TRANSITION", "INTERPRETED"),
                     ("TRANSITION", "PROPOSED"), ("TRANSITION", "APPROVAL_PENDING"),
                     ("TRANSITION", "READY"), ("TRANSITION", "IN_PROGRESS"),
                     ("TRANSITION", "COMPLETED")]  # fmt: skip
    assert [r[:2] for r in flow.audit(app, approval)] == [
        ("CREATE", "APPLIED"), ("DECIDE", "APPLIED"), ("CONSUME", "APPLIED"),
    ]  # fmt: skip
    verified = app.execute(
        "SELECT o.verified FROM ledger.outcome o JOIN ledger.action a ON a.id = o.action_id"
        " WHERE a.idempotency_key = %s",
        (action["execution_key"],),
    ).fetchone()
    assert verified == (True,)
    assert desk.rejected == [] and other.rejected == []


def test_revoked_node_cannot_execute(app: Conn, gw: Gw, owner: Conn, core_key: Any) -> None:
    """P2-A4: an action approved for a node that is then revoked is never delivered: the node
    can't connect (refused at the handshake), and no other node is sent it. A decision from a
    revoked node is refused `node_revoked` (the rule test above)."""
    desk, other = gw.enrolled(), gw.enrolled()
    desk.sync()
    other.sync()
    assert desk.node_id is not None
    desk.close()
    session = flow.open_session(app, MANJ)
    approval_id = flow.propose(app, owner, node=desk.node_id, person=MANJ, session=session)
    assert approval_id is not None
    flow.decide(app, owner, node=desk.node_id, person=MANJ, approval_id=approval_id)
    [item] = [i for i in _issued(app, MANJ)
              if i["authz"]["object"]["approval_id"] == str(approval_id)]  # fmt: skip
    assert item["target_node_id"] == desk.node_id
    nodes.revoke(app, desk.node_id)
    again = gw.node(key=desk.key, peer_ip=desk.peer_ip, state=desk.state_path)
    with pytest.raises(WebSocketDisconnect):  # refused at the handshake: no welcome
        again.hello()
    assert again.actions == []
    assert item["id"] not in [e["payload"].get("outbox_id") for e in other.sync()]
    sent = app.execute(
        "SELECT count(*) FROM comm.delivery WHERE outbox_id = %s", (item["id"],)
    ).fetchone()
    assert sent == (0,)


def test_fake_node_rejects_action_for_another_node(gw: Gw) -> None:
    """P2-S4: a core-signed action for node B, put on A's connection by a test-only path."""
    a, b = gw.enrolled(), gw.enrolled()
    a.sync()
    assert b.node_id is not None
    a.inject(gw.gw_envelope(a, "node.action", {"authz": gw.authz(b.node_id)}))
    assert a.rejected == [("node.action", "wrong_target")] and a.actions == []


def test_fake_node_rejects_expired_action(gw: Gw) -> None:
    a = gw.enrolled()
    a.sync()
    assert a.node_id is not None
    obj = dict(gw.authz(a.node_id)["object"])
    obj["valid_until"] = (gw.clock() - timedelta(seconds=31)).isoformat()
    authz = signing.make_authz(gw.core_key, gw.core_key_id, obj)
    a.inject(gw.gw_envelope(a, "node.action", {"authz": authz}))
    assert a.rejected == [("node.action", "expired")] and a.actions == []
    with pytest.raises(Rejected, match="expired"):  # the same checker the node uses
        check_node_action(authz, trusted={gw.core_key_id: signing.public_bytes(gw.core_key)},
                          me=a.node_id, now=gw.clock())  # fmt: skip


def test_action_signed_by_gateway_key_only_rejected_by_fake_node(gw: Gw) -> None:
    """P2-S4: an action signed with the gateway's key, even under the core key's ID, is never
    a core authorisation; nor is one in an approved-action outbox item."""
    a = gw.enrolled()
    a.sync()
    assert a.node_id is not None
    obj = gw.authz(a.node_id)["object"]
    for key_id in (gw.core_key_id, gw.gw_key_id):
        forged = signing.make_authz(gw.gw_key, key_id, obj)
        a.inject(gw.gw_envelope(a, "node.action", {"authz": forged}))
    a.inject(gw.gw_envelope(a, "outbox.item", {
        "outbox_id": str(uuid.uuid4()), "priority": "URGENT", "kind": "action.approved",
        "body": "Deleting", "expires_at": None,
        "authz": signing.make_authz(gw.gw_key, gw.core_key_id, obj),
    }))  # fmt: skip
    assert a.rejected == [("node.action", "bad_signature"), ("node.action", "untrusted_key_id"),
                          ("outbox.item", "bad_signature")]  # fmt: skip
    assert a.actions == []


def test_gateway_refuses_to_send_expired_action(app: Conn, gw: Gw, core_key: Any) -> None:
    """C3, gateway side: an ACTION chunk past its signed `valid_until` is never written to a
    socket; a valid one is."""
    desk = gw.enrolled()
    desk.sync()
    assert desk.node_id is not None
    session = flow.open_session(app, MANJ)
    stale, fresh = uuid.uuid4(), uuid.uuid4()
    actions.issue_live(app, reply_id=stale, session_id=session, target_node_id=desk.node_id,
                       person_id=MANJ, action_kind="module.news", args={},
                       now=gw.clock() - timedelta(minutes=3))  # fmt: skip
    actions.issue_live(app, reply_id=fresh, session_id=session, target_node_id=desk.node_id,
                       person_id=MANJ, action_kind="module.news", args={},
                       now=gw.clock())  # fmt: skip
    got = _until(desk, lambda e: bool(_actions(e)))
    got += desk.sync()
    sent = [e["payload"]["authz"]["object"]["reply_id"] for e in _actions(got)]
    assert sent == [str(fresh)]
    assert desk.rejected == []
