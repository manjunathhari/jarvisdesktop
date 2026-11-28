"""The team's tables (migration 0017) and the Postgres store, against a real database.

Skipped without JARVIS_TEST_SUPERUSER_DSN, like the server's other database tests; CI runs them.
Runs in its own database: tickets block the 0017 downgrade, so they must never reach the session
database that P0-6 takes down and up.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import migrate
from jarvis_server.improve import inbox
from jarvis_server.improve.store import IllegalMove, PgStore
from jarvis_server.improve.tickets import State, transitions

from ..conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def store(app: Conn) -> PgStore:
    return PgStore(app)


def _ticket(store: PgStore) -> int:
    return store.open_ticket("research is too thin", "research-is-too-thin", {"turns": ["a", "b"]})


def test_a_ticket_opens_and_reads_back(store: PgStore) -> None:
    tid = _ticket(store)
    t = store.get(tid)
    assert (t.state, t.words, t.slug, t.evidence) == (
        State.OPEN,
        "research is too thin",
        "research-is-too-thin",
        {"turns": ["a", "b"]},
    )
    assert t.cap_usd == 8.0 and t.rounds_used == 0


def test_the_database_holds_the_same_moves_as_the_code(owner: Conn) -> None:
    rows = owner.execute("SELECT from_state, to_state FROM improve.allowed_move").fetchall()
    assert {(State(a), State(b)) for a, b in rows} == set(transitions())


def test_the_database_refuses_a_move_outside_the_table(store: PgStore, app: Conn) -> None:
    tid = _ticket(store)
    with pytest.raises(IllegalMove):
        store.move(tid, State.APPROVED, "test", "skip the design")
    with pytest.raises(psycopg.errors.RaiseException):
        app.execute("UPDATE improve.ticket SET state = 'PR_OPEN' WHERE id = %s", (tid,))
    assert store.get(tid).state is State.OPEN


def test_approval_needs_the_fingerprint_of_this_design(store: PgStore, app: Conn) -> None:
    tid = _ticket(store)
    store.move(tid, State.DESIGNING, "jarvis", "start")
    store.update(tid, design="# d", design_fingerprint="a" * 64)
    store.move(tid, State.DESIGN_READY, "jarvis", "written")
    store.update(tid, approved_fingerprint="b" * 64)
    with pytest.raises(IllegalMove):
        store.move(tid, State.APPROVED, "owner", "yes")
    store.update(tid, approved_fingerprint="a" * 64)
    store.move(tid, State.APPROVED, "owner", "yes")
    assert store.get(tid).state is State.APPROVED


def test_pause_remembers_where_it_was_and_goes_back_only_there(store: PgStore) -> None:
    tid = _ticket(store)
    store.move(tid, State.DESIGNING, "jarvis", "start")
    store.move(tid, State.PAUSED, "jarvis", "cap")
    assert store.get(tid).paused_from is State.DESIGNING
    with pytest.raises(IllegalMove):
        store.move(tid, State.DESIGN_READY, "owner", "resume elsewhere")
    store.move(tid, State.DESIGNING, "owner", "resume")
    assert store.get(tid).paused_from is None


def test_every_move_is_recorded_with_who_and_why(store: PgStore, owner: Conn) -> None:
    tid = _ticket(store)
    store.move(tid, State.DESIGNING, "jarvis", "start")
    rows = owner.execute(
        "SELECT from_state, to_state, actor, reason FROM improve.ticket_event WHERE ticket_id = %s",
        (tid,),
    ).fetchall()
    assert rows == [("OPEN", "DESIGNING", "jarvis", "start")]


def test_fields_round_trip(store: PgStore) -> None:
    tid = _ticket(store)
    store.update(
        tid,
        tests_patch=b"\x00patch",
        test_fingerprints={"server/tests/test_x.py": "f" * 64},
        feedback=("one", "two"),
        handled_comments=frozenset({3, 7}),
        told_ready=True,
        base_sha="c" * 40,
    )
    t = store.get(tid)
    assert t.tests_patch == b"\x00patch"
    assert t.test_fingerprints == {"server/tests/test_x.py": "f" * 64}
    assert t.feedback == ("one", "two")
    assert t.handled_comments == frozenset({3, 7})
    assert t.told_ready and t.base_sha == "c" * 40


def test_update_refuses_the_state_column(store: PgStore) -> None:
    tid = _ticket(store)
    with pytest.raises(ValueError, match="state"):
        store.update(tid, state=State.DONE)


def test_rounds_stop_at_three(store: PgStore) -> None:
    tid = _ticket(store)
    assert [store.use_round(tid) for _ in range(3)] == [1, 2, 3]
    with pytest.raises(psycopg.errors.CheckViolation):
        store.use_round(tid)


def test_stage_costs_add_up(store: PgStore) -> None:
    tid = _ticket(store)
    store.record_stage(tid, "design", cost_usd=1.25, turns=10, outcome="PASSED", error=None)
    store.record_stage(tid, "tests", cost_usd=0.5, turns=4, outcome="FAILED", error="max_turns")
    assert store.spent(tid) == pytest.approx(1.75)


def test_the_inbox_keeps_open_items_and_resolves_by_kind(store: PgStore) -> None:
    tid = _ticket(store)
    store.add_inbox(inbox.design_ready(tid, "Research", "a" * 64))
    store.add_inbox(inbox.key_needed("improve_claude_key", "the team's key", ("Add it.",)))
    assert [i.kind for i in store.open_inbox()] == [inbox.Kind.DECISION, inbox.Kind.KEY]
    store.resolve_inbox(tid, inbox.Kind.DECISION)
    assert [i.key_name for i in store.open_inbox()] == ["improve_claude_key"]


def test_the_inbox_table_refuses_a_secret_value(app: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        app.execute(
            "INSERT INTO improve.inbox (kind, title, steps) VALUES ('KEY', %s, '{}')",
            ("Use sk-ant-api03-abcdefghijkl",),
        )


def test_the_queue_is_the_team_s_own_and_runs_a_paid_stage_once(
    store: PgStore, owner: Conn
) -> None:
    tid = _ticket(store)
    job_id = store.enqueue(tid, "design")
    assert owner.execute(
        "SELECT count(*) FROM ops.job WHERE payload->>'ticket' = %s", (str(tid),)
    ).fetchone() == (0,)
    job = store.claim("w1", lease_s=60)
    assert job is not None and (job.id, job.stage, job.attempts) == (job_id, "design", 1)
    assert store.claim("w2", lease_s=60) is None
    # The lease runs out (the orchestrator died mid-stage): a paid stage is not run twice.
    owner.execute(
        "UPDATE improve.job SET locked_until = now() - interval '1 s' WHERE id = %s", (job_id,)
    )
    assert store.claim("w2", lease_s=60) is None


def test_a_heartbeat_renews_the_lease(store: PgStore, owner: Conn) -> None:
    tid = _ticket(store)
    store.enqueue(tid, "build")
    job = store.claim("w1", lease_s=1)
    assert job is not None
    store.heartbeat(job.id, lease_s=3600)
    until = owner.execute(
        "SELECT locked_until - now() FROM improve.job WHERE id = %s", (job.id,)
    ).fetchone()
    assert until is not None and until[0] > timedelta(minutes=50)


def test_pr_watches_may_run_again_and_wait_their_delay(store: PgStore) -> None:
    tid = _ticket(store)
    store.enqueue(tid, "pr_watch", delay_s=3600)
    assert store.claim("w1", lease_s=60) is None
    store.enqueue(tid, "pr_watch")
    job = store.claim("w1", lease_s=60)
    assert job is not None and job.stage == "pr_watch"
    store.fail(job.id, "GitHub timed out")
    assert store.claim("w1", lease_s=60) is not None  # three tries for a free read


def test_finish_and_fail(store: PgStore, owner: Conn) -> None:
    tid = _ticket(store)
    a = store.enqueue(tid, "design")
    job = store.claim("w1", lease_s=60)
    assert job is not None
    store.fail(job.id, "boom")
    assert owner.execute(
        "SELECT status, last_error FROM improve.job WHERE id = %s", (a,)
    ).fetchone() == ("FAILED", "boom")


def test_in_state(store: PgStore) -> None:
    a, b = _ticket(store), _ticket(store)
    store.move(b, State.DESIGNING, "jarvis", "start")
    assert b in [t.id for t in store.in_state(State.DESIGNING)]
    assert a not in [t.id for t in store.in_state(State.DESIGNING)]


def test_the_gateway_cannot_see_the_team_s_schema(gateway: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT 1 FROM improve.ticket")


def test_0017_goes_down_and_up_and_refuses_once_a_ticket_exists() -> None:
    schema = "SELECT count(*) FROM pg_namespace WHERE nspname = 'improve'"
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0016")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(schema).fetchone() == (0,)
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.app, autocommit=True) as conn:
            PgStore(conn).open_ticket("keep me", "keep-me", {})
        with pytest.raises(Exception, match="cannot be undone"):
            migrate.downgrade(d.owner, "0016")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(schema).fetchone() == (1,)
