"""The policy engine, Atlas, the core key, `memory.write` from conversation, in-job audited
refusals and the approval audit SQL (spec §9.1, §9.2, §11, §12; R11-1, R12-21, R12-23; P2-S2).

Runs in its own database. People are fictional (D11).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jarvis_server import comm, config, jobs, ledger, nodes, policy, signing
from jarvis_server.atlas import ApprovedPlan, Atlas
from jarvis_server.bootstrap import APP_ROLE
from jarvis_server.brain import Speech, ToolCall, budget, health
from jarvis_server.contracts import ActorKind, EntityType, MemoryCategory, MemoryTag
from jarvis_server.core import dispatch, keys, turn
from jarvis_server.dbcall import NotInJob, audited_call_in_job
from jarvis_server.memory import checks
from jarvis_server.memory.engine import SYSTEM, MemoryEngine
from jarvis_server.policy import Allow, NeedsApproval, Refuse, Tier

from .conftest import Dsns, fresh_database
from .helpers import flow
from .test_turn import Brains, _enqueue, _run, _utter

Conn = psycopg.Connection[Any]
CORE = ledger.Actor(ActorKind.AGENT, "core")


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _reset(owner: Conn) -> Iterator[None]:
    owner.execute("DELETE FROM ops.heartbeat WHERE process = 'brain'")
    owner.execute(
        "UPDATE ops.job SET status = 'DONE', locked_by = NULL, locked_until = NULL"
        " WHERE status IN ('QUEUED', 'RUNNING')"
    )
    owner.execute(
        "UPDATE ops.brain_budget SET reserved_inr = 0, settled_inr = 0, cap_inr = %s",
        (budget.MONTHLY_CAP_PAISE,),
    )
    yield
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
def engine(tmp_path: Path, person: uuid.UUID) -> Iterator[MemoryEngine]:
    """`manj` is the speaking (test) person; `daughter` another household member."""
    directory = checks.StaticDirectory(
        people={person: flow.HOUSEHOLD, checks.DAUGHTER_ID: flow.HOUSEHOLD}
    )
    eng = MemoryEngine(tmp_path / "memory", directory, checks.ListAudit(), flow.HOUSEHOLD).open()
    eng.add_person("manj", person, "Manj (fictional)", by=SYSTEM)
    eng.add_person("daughter", checks.DAUGHTER_ID, "Daughter (fictional)", by=SYSTEM)
    turn.set_memory(eng)
    yield eng
    turn.set_memory(None)


def _decide(app: Conn, person: uuid.UUID, session: uuid.UUID, node: uuid.UUID, kind: str,
            *, node_kind: str = "DESKTOP", memory_ids: list[str] | None = None) -> Any:  # fmt: skip
    return policy.decide_tool_call(
        app, person_id=person, session_id=session, node_id=node, node_kind=node_kind,
        call=ToolCall(kind, {}), memory_ids=memory_ids or [],
    )  # fmt: skip


# --- the table (§9.1) -----------------------------------------------------------------------


def test_tier_table_exact() -> None:
    assert dict(policy.TIERS) == {
        "module.news": Tier.READ_ONLY,
        "module.live_tv": Tier.READ_ONLY,
        "screen.*": Tier.READ_ONLY,
        "memory.write": Tier.REVERSIBLE,
        "pc.list_test_folder": Tier.READ_ONLY,
        "pc.delete_test_file": Tier.IRREVERSIBLE,
    }
    with pytest.raises(TypeError):
        policy.TIERS["shell.run"] = Tier.READ_ONLY  # type: ignore[index]
    assert policy.tier_of("screen.close_panel") is Tier.READ_ONLY
    assert policy.tier_of("screen.*") is None  # the row's name is not itself a kind
    for kind in ("screen", "screen.", "screen.Close", "module.news.x", "pc.delete", "shell.run"):
        assert policy.tier_of(kind) is None, kind


def test_unknown_action_refused(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    for kind in ("shell.run", "pc.delete_all", "show_news", "screen.*"):
        assert _decide(app, person, session, node, kind) == Refuse("unknown_action")


def test_checks_in_order(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID,
    engine: MemoryEngine,
) -> None:  # fmt: skip
    """§9.2: identity, scope, purpose, approval, memory; the first failure decides."""
    assert _decide(app, person, session, node, "module.news") == Allow(Tier.READ_ONLY)
    assert _decide(app, person, session, node, "screen.clear") == Allow(Tier.READ_ONLY)
    assert _decide(app, person, session, node, "memory.write") == Allow(Tier.REVERSIBLE)
    assert _decide(app, person, session, node, "pc.delete_test_file") == NeedsApproval()
    # 1. Identity: another person's node, a revoked node, a wrong node kind claim.
    other = flow.new_person(owner, "Ravi Fictional")
    other_node = flow.new_node(owner, other)
    assert _decide(app, person, session, other_node, "module.news") == Refuse("identity_mismatch")
    assert _decide(app, person, session, node, "module.news", node_kind="PHONE") == Refuse(
        "identity_mismatch"
    )
    # 2. Scope: before purpose, so an unknown kind in a foreign session is unknown_action.
    foreign_session = flow.open_session(app, other)
    assert _decide(app, person, foreign_session, node, "shell.run") == Refuse("unknown_action")
    phone = flow.new_node(owner, person, "PHONE")
    assert _decide(app, person, session, phone, "pc.delete_test_file", node_kind="PHONE") == (
        Refuse("node_kind_not_allowed")
    )
    assert _decide(app, person, session, phone, "memory.write", node_kind="PHONE") == Allow(
        Tier.REVERSIBLE
    )
    # 3. Purpose: the person's own open session only.
    assert _decide(app, person, foreign_session, node, "module.news") == Refuse("wrong_session")
    app.execute("UPDATE conv.session SET closed_at = now() WHERE id = %s", (session,))
    assert _decide(app, person, session, node, "module.news") == Refuse("wrong_session")
    # 1 again, revoked last so the node stays usable above.
    nodes.revoke(app, node)
    fresh = flow.open_session(app, person)
    assert _decide(app, person, fresh, node, "module.news") == Refuse("identity_mismatch")


def test_inferred_memory_blocks_action(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID, engine: MemoryEngine
) -> None:
    stated = engine.write("manj", MemoryCategory.IDENTITY, "Prefers aisle seats",
                          MemoryTag.STATED, "test", by="manj")  # fmt: skip
    inferred = engine.write("manj", MemoryCategory.IDENTITY, "Probably likes tea",
                            MemoryTag.INFERRED, "test", by="agent:pattern")  # fmt: skip
    ok = _decide(app, person, session, node, "pc.delete_test_file", memory_ids=[stated])
    assert ok == NeedsApproval()
    for kind in ("module.news", "memory.write", "pc.delete_test_file"):
        decision = _decide(app, person, session, node, kind, memory_ids=[stated, inferred])
        assert decision == Refuse("memory_not_actionable"), kind
    assert _decide(app, person, session, node, "module.news", memory_ids=["m-9999"]) == Refuse(
        "memory_not_actionable"
    )  # unknown entries are not actionable either


def test_no_memory_engine_fails_closed(
    app: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID
) -> None:
    policy.set_memory(None)
    assert _decide(app, person, session, node, "module.news", memory_ids=["m-0001"]) == Refuse(
        "memory_not_actionable"
    )


# --- Atlas (§11) ------------------------------------------------------------------------------


def test_atlas_one_plan_one_task(app: Conn, person: uuid.UUID) -> None:
    plan = ApprovedPlan(
        title="Delete test file a.txt", definition_of_done="The file is in the Recycle Bin",
        source_ref="test", owner_person_id=person,
    )  # fmt: skip
    ids = Atlas().accept_plan(app, plan, by=CORE)
    assert len(ids) == 1
    [task_id] = ids
    assert ledger.state_of(app, EntityType.TASK, task_id) == "PROPOSED"
    trail = [(e.action, e.from_state, e.to_state) for e in
             ledger.audit_trail(app, EntityType.TASK, task_id)]  # fmt: skip
    assert trail == [
        ("CREATE", None, "DETECTED"),
        ("TRANSITION", "DETECTED", "INTERPRETED"),
        ("TRANSITION", "INTERPRETED", "PROPOSED"),
    ]
    row = app.execute(
        "SELECT title, owner_person_id FROM ledger.task WHERE id = %s", (task_id,)
    ).fetchone()
    assert row == ("Delete test file a.txt", person)


def test_atlas_accepts_inside_the_callers_transaction(app: Conn, person: uuid.UUID) -> None:
    plan = ApprovedPlan("Fictional plan", "done", "test", person)
    before = app.execute("SELECT count(*) FROM ledger.task").fetchone()
    with pytest.raises(RuntimeError), app.transaction():
        Atlas().accept_plan(app, plan, by=CORE)
        raise RuntimeError("the caller's next step failed")
    assert app.execute("SELECT count(*) FROM ledger.task").fetchone() == before


# --- the core key -----------------------------------------------------------------------------


def test_active_core_key_reads_the_secret(core_key: Any) -> None:
    key, key_id = keys.active_core_key()
    assert signing.public_bytes(key) == signing.public_bytes(core_key)
    assert key_id == signing.fingerprint(signing.public_bytes(core_key))


def test_active_core_key_missing_secret_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="core_signing_key"):
        keys.active_core_key()


# --- memory.write from conversation (§12, P2-S2) -------------------------------------------


def test_memory_write_uses_authenticated_person(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, engine: MemoryEngine
) -> None:
    turn.set_brain(Brains([
        ToolCall("memory_write", {
            "content": "I prefer aisle seats", "by": "daughter", "viewer": "daughter",
            "subject": "daughter", "about": "daughter", "tag": "CONFIRMED",
            "category": "preference", "topics": ["travel"],
        }),
        Speech("Noted."),
    ]))  # fmt: skip
    tid = _utter(owner, node, person, text="Remember that I prefer aisle seats")
    _run(app, _enqueue(app, tid))
    found = engine.search("aisle", viewer=SYSTEM)
    assert [(e.subject, e.by, e.tag, e.category) for e in found] == [
        ("manj", "manj", MemoryTag.STATED, MemoryCategory.IDENTITY)
    ]  # never the daughter, never CONFIRMED: Claude's arguments set none of it
    assert found[0].content == "I prefer aisle seats"
    assert found[0].source == f"inbound-{tid}"
    assert found[0].topics == ()
    [item] = flow.items(app, person, "memory.written")
    assert item["priority"] == "SILENT"
    assert item["body"]["text"] == "Remembered: I prefer aisle seats"
    row = app.execute(
        "SELECT text FROM conv.message WHERE inbound_id = %s AND role = 'JARVIS'", (tid,)
    ).fetchone()
    assert row == (f"{turn.REMEMBERED_TEXT} Noted.",)


def test_memory_write_refused_by_memory_is_explained(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, engine: MemoryEngine
) -> None:
    turn.set_brain(Brains([ToolCall("memory_write", {"content": "   "})]))
    tid = _utter(owner, node, person, text="Remember nothing")
    _run(app, _enqueue(app, tid))
    row = app.execute(
        "SELECT text FROM conv.message WHERE inbound_id = %s AND role = 'JARVIS'", (tid,)
    ).fetchone()
    assert row == (turn.NOT_REMEMBERED_TEXT,)
    assert flow.items(app, person, "memory.written") == []


# --- in-job audited refusals (R11-1) -------------------------------------------------------

_SEEN: dict[str, Any] = {}


@dispatch.register("test.in_job_refusal")
def _refusing_handler(conn: Conn, job: jobs.Job) -> None:
    """Calls an audited function that refuses; keeps the refusal by returning."""
    key, refusal = audited_call_in_job(
        conn, "SELECT execution_key, refusal FROM ledger.consume_approval(%s)",
        (uuid.UUID(job.payload["approval_id"]),),
    )  # fmt: skip
    _SEEN["result"] = (key, refusal)


def test_in_job_refusal_commits_with_the_job(app: Conn, dsns: Dsns) -> None:
    approval_id = uuid.uuid4()
    job_id = flow.run(app, "test.in_job_refusal", {"approval_id": str(approval_id)})
    assert _SEEN["result"] == (None, "unknown_approval")
    assert flow.job_status(app, job_id) == "DONE"
    with psycopg.connect(dsns.app, autocommit=True) as other:
        assert flow.audit(other, approval_id) == [
            ("CONSUME", "REJECTED", "unknown_approval", None, None)
        ]


def test_audited_call_in_job_refuses_outside_job(app: Conn) -> None:
    before = app.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    sql = "SELECT execution_key, refusal FROM ledger.consume_approval(%s)"
    with pytest.raises(NotInJob):
        audited_call_in_job(app, sql, (uuid.uuid4(),))
    with pytest.raises(NotInJob), app.transaction():  # a transaction that is not a job's
        audited_call_in_job(app, sql, (uuid.uuid4(),))
    assert app.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone() == before


# --- approval audit SQL (R12-21) ------------------------------------------------------------


def test_refuse_decision_codes_and_malformed_calls(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    for code in ("unknown_approval", "bad_card_sig", "other_person", "node_revoked",
                 "not_pending", "expired"):  # fmt: skip
        approval_id = uuid.uuid4()
        (result,) = _one_row(
            app, "SELECT ledger.refuse_decision(%s, %s, %s)", (approval_id, node, code)
        )
        assert result == code
        rows = app.execute(
            "SELECT action::text, outcome::text, reason, details FROM audit.event"
            " WHERE entity_id = %s",
            (approval_id,),
        ).fetchall()
        assert rows == [("DECIDE", "REJECTED", code, {"node_id": str(node)})]
    for bad in (("not_approved",), ("already_consumed",), ("anything",), (None,)):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            app.execute("SELECT ledger.refuse_decision(%s, %s, %s)", (uuid.uuid4(), node, bad[0]))
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app.execute("SELECT ledger.refuse_decision(NULL, %s, 'expired')", (node,))


def _one_row(app: Conn, sql: Any, params: Any) -> tuple[Any, ...]:
    row = app.execute(sql, params).fetchone()
    assert row is not None
    return tuple(row)


def test_refuse_decision_is_definer_for_app_only(owner: Conn, gateway: Conn) -> None:
    row = owner.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " (SELECT array_agg(pg_get_userbyid(a.grantee)::text ORDER BY 1)"
        "  FROM aclexplode(p.proacl) a"
        "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
        " FROM pg_proc p WHERE p.oid = 'ledger.refuse_decision(uuid, uuid, text)'::regprocedure"
    ).fetchone()
    assert row == (True, "jarvis_owner", ["search_path=pg_catalog, pg_temp"], [APP_ROLE])
    public = owner.execute(
        "SELECT has_function_privilege('public', 'ledger.refuse_decision(uuid, uuid, text)',"
        " 'EXECUTE')"
    ).fetchone()
    assert public == (False,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute(
            "SELECT ledger.refuse_decision(gen_random_uuid(), gen_random_uuid(), 'expired')"
        )
    for trigger_fn in ("ledger.approval_audit_create()", "ledger.approval_audit_decide()"):
        acl = owner.execute(
            "SELECT p.prosecdef, (SELECT count(*) FROM aclexplode(p.proacl) a"
            "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (trigger_fn,),
        ).fetchone()
        assert acl == (True, 0), trigger_fn


def test_approval_creation_and_decisions_are_audited(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID, session: uuid.UUID,
    core_key: Any,
) -> None:  # fmt: skip
    approval_id = flow.propose(app, owner, node=node, person=person, session=session)
    assert approval_id is not None
    assert flow.audit(app, approval_id) == [("CREATE", "APPLIED", "created", None, "PENDING")]
    details = app.execute(
        "SELECT details FROM audit.event WHERE entity_id = %s", (approval_id,)
    ).fetchone()
    task = flow.approval_row(app, approval_id)["task_id"]
    assert details == ({"task_id": str(task), "requesting_node_id": str(node)},)
    app.execute(
        "UPDATE ledger.approval SET decision = 'DENIED', decided_at = now(),"
        " decided_by_node_id = %s WHERE id = %s",
        (node, approval_id),
    )
    assert flow.audit(app, approval_id)[-1] == ("DECIDE", "APPLIED", "denied", "PENDING", "DENIED")


# --- the outbox target (R12-23) -------------------------------------------------------------


def test_outbox_target_must_be_the_persons_node(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    other_node = flow.new_node(owner, flow.new_person(owner, "Ravi Fictional"))
    event = comm.record_event(app, "test.notice", subject_type="test", subject_id="t-1",
                              person_id=person, details={})  # fmt: skip
    kw: dict[str, Any] = {
        "person_id": person, "event_id": event, "priority": comm.Priority.TODAY,
        "kind": "test.notice", "body": {"text": "A fictional notice"}, "group_key": "test",
        "expires_at": None,
    }  # fmt: skip
    ok = comm.create_outbox(app, **kw, target_node_id=node)
    row = app.execute("SELECT target_node_id FROM comm.outbox WHERE id = %s", (ok,)).fetchone()
    assert row == (node,)
    with pytest.raises(psycopg.errors.CheckViolation):
        comm.create_outbox(app, **kw, target_node_id=other_node)
    with pytest.raises(psycopg.errors.CheckViolation):  # never re-addressed afterwards
        owner.execute("UPDATE comm.outbox SET target_node_id = NULL WHERE id = %s", (ok,))
