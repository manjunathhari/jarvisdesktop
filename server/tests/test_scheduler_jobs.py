"""The scheduler's jobs (spec §5.4, §8.2, §10.2; plan Task 13, rulings 1 to 3).

The scheduler only enqueues: each due item or period gets at most one job, deduplicated by a
deterministic job key in the payload (`<kind>:<item>`), whoever enqueued it first. The clock is
injected (`Scheduler.tick(conn, now)`); nothing here sleeps.

| Job | When |
|---|---|
| `approval.expiring` | 2 min before each PENDING approval's expiry |
| `approval.expire` | at expiry |
| `outbox.expire` | any open item past its `expires_at` |
| `brain.probe` | every 2 min while the brain is DOWN |
| `brain.reservation_sweep`, `keys.activate` | every 1 min |
| `outbox.resend_sweep` | every 60 s: wakes the gateway's own re-send sweep |
| `retention.sweep` | daily at 03:30 IST |

Runs in its own database; every person is fictional.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server import comm, jobs, scheduler_main
from jarvis_server.brain import budget, health
from jarvis_server.comm import Priority
from jarvis_server.core import approvals, dispatch
from jarvis_server.gateway import relay
from jarvis_server.scheduler_main import Scheduler

from .conftest import Dsns, fresh_database
from .helpers import flow

Conn = psycopg.Connection[Any]
IST = budget.IST
PERIODIC = ("brain.reservation_sweep", "keys.activate", "outbox.resend_sweep")


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _reset(owner: Conn, core_key: Any) -> Iterator[None]:
    """No runnable job and the brain up; a core key for approval cards."""
    owner.execute(
        "UPDATE ops.job SET status = 'DONE', locked_by = NULL, locked_until = NULL"
        " WHERE status IN ('QUEUED', 'RUNNING')"
    )
    owner.execute("DELETE FROM ops.heartbeat WHERE process = 'brain'")
    yield
    approvals.set_wait(None, None)


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    return flow.new_person(owner)


@pytest.fixture
def node(owner: Conn, person: uuid.UUID) -> uuid.UUID:
    return flow.new_node(owner, person)


def _proposed(app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID) -> uuid.UUID:
    session = flow.open_session(app, person)
    approval_id = flow.propose(app, owner, node=node, person=person, session=session)
    assert approval_id is not None
    return approval_id


def _expires(app: Conn, approval_id: uuid.UUID) -> datetime:
    return flow.approval_row(app, approval_id)["expires_at"]  # type: ignore[no-any-return]


def _keyed(app: Conn, kind: str, key: str) -> list[int]:
    rows = app.execute(
        "SELECT id FROM ops.job WHERE kind = %s AND payload->>'job_key' = %s ORDER BY id",
        (kind, key),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _of_kind(app: Conn, kind: str, since: int) -> list[dict[str, Any]]:
    rows = app.execute(
        "SELECT payload FROM ops.job WHERE kind = %s AND id > %s ORDER BY id", (kind, since)
    ).fetchall()
    return [r[0] for r in rows]


def _last_job(app: Conn) -> int:
    row = app.execute("SELECT coalesce(max(id), 0) FROM ops.job").fetchone()
    assert row is not None
    return int(row[0])


def _run(app: Conn, job_id: int) -> None:
    dispatch.handle(app, flow.claim(app, job_id))


def _drop(owner: Conn, kind: str, approval_id: uuid.UUID) -> None:
    """Lose an approval's own job (as if it was never queued): the scheduler must catch it."""
    owner.execute(
        "DELETE FROM ops.job WHERE kind = %s AND payload->>'approval_id' = %s",
        (kind, str(approval_id)),
    )


# --- dedup --------------------------------------------------------------------------------------


def test_enqueue_once_dedups_by_job_key(app: Conn) -> None:
    key = f"test.kind:{uuid.uuid4()}"
    first = jobs.enqueue_once(app, "noop", key, {"x": "1"})
    assert first is not None
    assert jobs.enqueue_once(app, "noop", key, {"x": "2"}) is None
    app.execute("UPDATE ops.job SET status = 'DONE' WHERE id = %s", (first,))
    assert jobs.enqueue_once(app, "noop", key) is None  # once per item, whatever its status
    assert _keyed(app, "noop", key) == [first]
    payload = app.execute("SELECT payload FROM ops.job WHERE id = %s", (first,)).fetchone()
    assert payload == ({"x": "1", "job_key": key},)
    assert jobs.job_key("approval.expire", "abc") == "approval.expire:abc"


JOB_INSERT = "INSERT INTO ops.job (kind, payload) VALUES (%s, %s) RETURNING id"


def test_job_key_index_refuses_a_second_job(app: Conn, owner: Conn) -> None:
    """R12-32: one job per (kind, job_key) is a database guarantee, whoever inserts it."""
    row = owner.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops' AND indexname = 'job_one_per_key'"
    ).fetchone()
    assert row is not None
    assert "UNIQUE INDEX" in row[0] and "(payload ? 'job_key'::text)" in row[0]
    key = f"noop:{uuid.uuid4()}"
    app.execute(JOB_INSERT, ("noop", Jsonb({"job_key": key})))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app.execute(JOB_INSERT, ("noop", Jsonb({"job_key": key, "x": "other payload"})))
    app.execute(JOB_INSERT, ("retention.sweep", Jsonb({"job_key": key})))  # another kind: fine
    for _ in range(2):  # jobs without a key are never deduplicated
        app.execute(JOB_INSERT, ("noop", Jsonb({"x": "same"})))
    assert len(_keyed(app, "noop", key)) == 1


def test_keyed_approval_expire_run_early_requeues_without_its_key(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    """The approval's own keyed `approval.expire` job, run before the expiry, re-queues itself
    for the expiry. The re-queued job carries no `job_key` (the first job keeps it, R12-32), so
    the index doesn't refuse it, and the scheduler still never adds a second one."""
    approval_id = _proposed(app, owner, person, node)
    key = jobs.job_key("approval.expire", str(approval_id))
    [first] = _keyed(app, "approval.expire", key)
    _run(app, first)
    assert flow.approval_row(app, approval_id)["decision"] == "PENDING"
    rows = app.execute(
        "SELECT payload, status FROM ops.job WHERE kind = 'approval.expire'"
        " AND payload->>'approval_id' = %s ORDER BY id",
        (str(approval_id),),
    ).fetchall()
    assert rows == [({"approval_id": str(approval_id), "job_key": key}, "DONE"),
                    ({"approval_id": str(approval_id)}, "QUEUED")]  # fmt: skip
    after = _expires(app, approval_id) + timedelta(seconds=1)
    assert jobs.enqueue_once(app, "approval.expire", key, {}) is None
    assert all(k[0] != "approval.expire" for k in _due_kinds(app, after))


def _due_kinds(app: Conn, now: datetime) -> list[tuple[str, Any]]:
    """The (kind, payload) of the jobs one scheduler tick at `now` creates."""
    created = Scheduler().tick(app, now)
    rows = app.execute(
        "SELECT kind, payload FROM ops.job WHERE id = ANY(%s) ORDER BY id", (created,)
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def test_enqueue_once_waits_on_the_index_not_a_lock(app: Conn, dsns: Dsns) -> None:
    """Two connections race for one key. The second waits on the first's uncommitted row (the
    unique index, `transactionid`), not on an advisory lock; it then gets None if the first
    commits, or the job if the first rolls back."""
    from .helpers.threads import InThread

    for outcome in ("commit", "rollback"):
        key = f"noop:{uuid.uuid4()}"
        with psycopg.connect(dsns.app) as first:  # not autocommit: the insert stays open
            first_id = jobs.enqueue_once(first, "noop", key)
            assert first_id is not None
            second = InThread(dsns.app, lambda c, k=key: jobs.enqueue_once(c, "noop", k))
            assert second.wait_until_blocked(dsns.superuser) == "transactionid"
            if outcome == "commit":
                first.commit()
            else:
                first.rollback()
            got = second.join()
        if outcome == "commit":
            assert got is None and _keyed(app, "noop", key) == [first_id]
        else:
            assert got is not None and _keyed(app, "noop", key) == [got]


def test_scheduler_enqueues_each_due_item_once(app: Conn, owner: Conn, person: uuid.UUID,
                                               node: uuid.UUID) -> None:  # fmt: skip
    """Two scheduler ticks (and a restarted scheduler) → one approval.expire job per approval."""
    a, b = (_proposed(app, owner, person, node) for _ in range(2))
    for approval_id in (a, b):
        _drop(owner, "approval.expire", approval_id)
    at = max(_expires(app, a), _expires(app, b))
    Scheduler().tick(app, at - timedelta(minutes=3))  # neither is due yet
    assert _keyed(app, "approval.expire", jobs.job_key("approval.expire", str(a))) == []
    s = Scheduler()
    s.tick(app, at)
    s.tick(app, at + timedelta(seconds=30))
    Scheduler().tick(app, at + timedelta(minutes=1))  # a restart: the database dedups
    for approval_id in (a, b):
        [job_id] = _keyed(app, "approval.expire", jobs.job_key("approval.expire", str(approval_id)))
        payload = app.execute("SELECT payload FROM ops.job WHERE id = %s", (job_id,)).fetchone()
        assert payload is not None and payload[0]["approval_id"] == str(approval_id)


def test_scheduled_expiry_runs_the_existing_handler(app: Conn, owner: Conn, person: uuid.UUID,
                                                    node: uuid.UUID) -> None:  # fmt: skip
    approval_id = _proposed(app, owner, person, node)
    _drop(owner, "approval.expire", approval_id)
    with owner.transaction():  # the expiry has passed in the database's time as well
        owner.execute("ALTER TABLE ledger.approval DISABLE TRIGGER approval_guard")
        owner.execute(
            "UPDATE ledger.approval SET expires_at = now() - interval '1 second' WHERE id = %s",
            (approval_id,),
        )
        owner.execute("ALTER TABLE ledger.approval ENABLE TRIGGER approval_guard")
    Scheduler().tick(app, flow.db_now(app))
    [job_id] = _keyed(app, "approval.expire", jobs.job_key("approval.expire", str(approval_id)))
    _run(app, job_id)
    assert flow.approval_row(app, approval_id)["decision"] == "EXPIRED"


def test_scheduler_does_not_duplicate_the_approvals_own_jobs(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    """Task 11 queues approval.expiring and approval.expire at creation, under the same job
    keys, so the scheduler adds nothing for them."""
    approval_id = _proposed(app, owner, person, node)
    at = _expires(app, approval_id)
    for kind in ("approval.expiring", "approval.expire"):
        assert len(_keyed(app, kind, jobs.job_key(kind, str(approval_id)))) == 1
    s = Scheduler()
    for when in (at - approvals.RESURFACE_BEFORE, at, at + timedelta(minutes=5)):
        s.tick(app, when)
        Scheduler().tick(app, when)
    for kind in ("approval.expiring", "approval.expire"):
        assert len(_keyed(app, kind, jobs.job_key(kind, str(approval_id)))) == 1


def test_expiring_is_due_two_minutes_before_expiry(app: Conn, owner: Conn, person: uuid.UUID,
                                                   node: uuid.UUID) -> None:  # fmt: skip
    approval_id = _proposed(app, owner, person, node)
    _drop(owner, "approval.expiring", approval_id)
    at, key = _expires(app, approval_id), jobs.job_key("approval.expiring", str(approval_id))
    Scheduler().tick(app, at - approvals.RESURFACE_BEFORE - timedelta(seconds=1))
    assert _keyed(app, "approval.expiring", key) == []
    Scheduler().tick(app, at - approvals.RESURFACE_BEFORE)
    assert len(_keyed(app, "approval.expiring", key)) == 1
    Scheduler().tick(app, at - timedelta(seconds=10))
    assert len(_keyed(app, "approval.expiring", key)) == 1
    # Once it has expired there is nothing left to re-surface.
    other = _proposed(app, owner, person, node)
    _drop(owner, "approval.expiring", other)
    Scheduler().tick(app, _expires(app, other))
    assert _keyed(app, "approval.expiring", jobs.job_key("approval.expiring", str(other))) == []


def test_decided_approval_is_not_scheduled(app: Conn, owner: Conn, person: uuid.UUID,
                                           node: uuid.UUID) -> None:  # fmt: skip
    approval_id = _proposed(app, owner, person, node)
    for kind in ("approval.expiring", "approval.expire"):
        _drop(owner, kind, approval_id)
    flow.decide(app, owner, node=node, person=person, approval_id=approval_id, decision="DENIED")
    Scheduler().tick(app, _expires(app, approval_id) + timedelta(minutes=1))
    for kind in ("approval.expiring", "approval.expire"):
        assert _keyed(app, kind, jobs.job_key(kind, str(approval_id))) == []


# --- outbox.expire, the general sweep (ruling 3) --------------------------------------------


def _item(app: Conn, person: uuid.UUID, expires_at: datetime | None) -> uuid.UUID:
    event = comm.record_event(
        app, "budget.80", subject_type="budget", subject_id="2026-11", person_id=person,
        details={},
    )  # fmt: skip
    return comm.create_outbox(
        app, person_id=person, event_id=event, priority=Priority.TODAY, kind="budget.80",
        body={"text": "fictional"}, group_key="budget", expires_at=expires_at,
    )  # fmt: skip


def test_outbox_expire_sweep_covers_every_expiring_item(app: Conn, person: uuid.UUID) -> None:
    now = flow.db_now(app)
    past = _item(app, person, now - timedelta(minutes=1))
    later = _item(app, person, now + timedelta(hours=1))
    never = _item(app, person, None)
    ended = _item(app, person, now - timedelta(minutes=1))
    app.execute("SELECT comm.supersede(%s)", (ended,))
    s = Scheduler()
    s.tick(app, now)
    s.tick(app, now + timedelta(seconds=30))
    key = jobs.job_key("outbox.expire", str(past))
    assert len(_keyed(app, "outbox.expire", key)) == 1
    for other in (later, never, ended):
        assert _keyed(app, "outbox.expire", jobs.job_key("outbox.expire", str(other))) == []
    # The existing per-item handler does the work.
    _run(app, _keyed(app, "outbox.expire", key)[0])
    row = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (past,)).fetchone()
    assert row == ("EXPIRED",)
    Scheduler().tick(app, now + timedelta(hours=2))  # `later` is due now; `past` is done
    assert len(_keyed(app, "outbox.expire", jobs.job_key("outbox.expire", str(later)))) == 1
    assert len(_keyed(app, "outbox.expire", key)) == 1


def test_outbox_expire_sweep_skips_an_item_with_its_own_job(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    """An approved action's item already has its outbox.expire job (Task 11), under the same
    job key, so the sweep doesn't add a second."""
    approval_id = _proposed(app, owner, person, node)
    flow.decide(app, owner, node=node, person=person, approval_id=approval_id)
    [item] = flow.items(app, person, "action.approved")
    key = jobs.job_key("outbox.expire", str(item["id"]))
    assert len(_keyed(app, "outbox.expire", key)) == 1
    Scheduler().tick(app, item["expires_at"] + timedelta(seconds=1))
    assert len(_keyed(app, "outbox.expire", key)) == 1


# --- periodic jobs ------------------------------------------------------------------------------


def test_periodic_jobs_once_per_minute(app: Conn) -> None:
    t0 = datetime(2031, 3, 4, 10, 20, 5, tzinfo=UTC)
    since = _last_job(app)
    s = Scheduler()
    for seconds in (0, 15, 30, 54):  # the same minute: one job of each kind
        s.tick(app, t0 + timedelta(seconds=seconds))
    Scheduler().tick(app, t0 + timedelta(seconds=40))  # a restart in the same minute
    for kind in PERIODIC:
        assert [p["job_key"] for p in _of_kind(app, kind, since)] == [f"{kind}:2031-03-04T10:20Z"]
    s.tick(app, t0 + timedelta(seconds=60))
    for kind in PERIODIC:
        assert len(_of_kind(app, kind, since)) == 2
    assert _of_kind(app, "brain.probe", since) == []  # the brain is up


def _brain_down(owner: Conn, down: bool) -> None:
    owner.execute(
        "INSERT INTO ops.heartbeat (process, last_seen, details) VALUES ('brain', now(), %s)"
        " ON CONFLICT (process) DO UPDATE SET details = EXCLUDED.details",
        (Jsonb({"down": down, "consecutive_failures": 2 if down else 0}),),
    )


def test_probe_every_two_minutes_while_down(app: Conn, owner: Conn) -> None:
    t0 = datetime(2031, 3, 4, 10, 20, 1, tzinfo=UTC)
    since = _last_job(app)
    _brain_down(owner, True)
    assert health.is_down(app)
    s = Scheduler()
    for seconds in (0, 30, 60, 118):
        s.tick(app, t0 + timedelta(seconds=seconds))
    assert [p["job_key"] for p in _of_kind(app, "brain.probe", since)] == [
        "brain.probe:2031-03-04T10:20Z"
    ]
    s.tick(app, t0 + timedelta(minutes=2))
    assert len(_of_kind(app, "brain.probe", since)) == 2
    _brain_down(owner, False)
    s.tick(app, t0 + timedelta(minutes=4))
    assert len(_of_kind(app, "brain.probe", since)) == 2


def _reservation_count(app: Conn) -> int:
    row = app.execute("SELECT count(*) FROM ops.brain_reservation").fetchone()
    assert row is not None
    return int(row[0])


def test_reminders_and_expiries_run_while_claude_is_down(
    app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    """P2-B1, "reminders, expiries and delivery continue": with the brain DOWN, one scheduler
    tick queues the probe and the due expiries, and the approval reminder, the approval expiry
    and the general outbox expiry all run through their handlers. Nothing calls Claude: no
    reservation is made, and the brain is still DOWN afterwards."""
    _brain_down(owner, True)
    assert health.is_down(app)
    reservations = _reservation_count(app)
    approval_id = _proposed(app, owner, person, node)
    reminder_key = jobs.job_key("approval.expiring", str(approval_id))
    [reminder] = _keyed(app, "approval.expiring", reminder_key)
    _run(app, reminder)
    [card] = flow.items(app, person, "approval.requested")
    [again] = flow.items(app, person, "approval.expiring")
    assert card["state"] == "SUPERSEDED" and again["priority"] == "URGENT"

    _drop(owner, "approval.expire", approval_id)
    with owner.transaction():  # the approval's expiry has passed in the database's time
        owner.execute("ALTER TABLE ledger.approval DISABLE TRIGGER approval_guard")
        owner.execute(
            "UPDATE ledger.approval SET expires_at = now() - interval '1 second' WHERE id = %s",
            (approval_id,),
        )
        owner.execute("ALTER TABLE ledger.approval ENABLE TRIGGER approval_guard")
    now = flow.db_now(app)
    stale = _item(app, person, now - timedelta(minutes=1))
    due = {k for k, _ in _due_kinds(app, now)}
    assert {"brain.probe", "approval.expire", "outbox.expire"} <= due
    [expire] = _keyed(app, "approval.expire", jobs.job_key("approval.expire", str(approval_id)))
    _run(app, expire)
    assert flow.approval_row(app, approval_id)["decision"] == "EXPIRED"
    [sweep] = _keyed(app, "outbox.expire", jobs.job_key("outbox.expire", str(stale)))
    _run(app, sweep)
    row = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (stale,)).fetchone()
    assert row == ("EXPIRED",)
    assert _reservation_count(app) == reservations
    assert health.is_down(app)


def test_retention_daily_at_0330_ist(app: Conn) -> None:
    since = _last_job(app)
    day = datetime(2031, 3, 5, tzinfo=IST)
    s = Scheduler()
    s.tick(app, day + timedelta(hours=3, minutes=29, seconds=59))
    assert _of_kind(app, "retention.sweep", since) == []
    # 03:30 IST is 22:00 UTC on the day before: the IST date names the run.
    assert (day + timedelta(hours=3, minutes=30)).astimezone(UTC).day == 4
    for at in (timedelta(hours=3, minutes=30), timedelta(hours=9), timedelta(hours=23, minutes=59)):
        s.tick(app, day + at)
        Scheduler().tick(app, day + at)
    assert [p["job_key"] for p in _of_kind(app, "retention.sweep", since)] == [
        "retention.sweep:2031-03-05"
    ]
    s.tick(app, day + timedelta(days=1, hours=3))  # 03:00 the next day: not yet
    s.tick(app, day + timedelta(days=1, hours=3, minutes=31))
    assert len(_of_kind(app, "retention.sweep", since)) == 2


def test_tick_needs_an_aware_time(app: Conn) -> None:
    with pytest.raises(ValueError, match="time zone"):
        Scheduler().tick(app, datetime(2031, 3, 5, 3, 30))


def test_scheduler_runs_no_handler(app: Conn) -> None:
    """Enqueue only: a tick leaves every job it wrote QUEUED, for core to run."""
    since = _last_job(app)
    Scheduler().tick(app, datetime(2031, 3, 8, 4, 0, tzinfo=IST))
    rows = app.execute("SELECT DISTINCT status FROM ops.job WHERE id > %s", (since,)).fetchall()
    assert rows == [("QUEUED",)]


# --- outbox.resend_sweep: a wake-up for the gateway's own sweep (ruling 2) -----------------------


def test_resend_sweep_job_wakes_the_gateway(app: Conn, dsns: Dsns) -> None:
    with psycopg.connect(dsns.gateway, autocommit=True) as listener:
        listener.execute(f"LISTEN {comm.RESEND_CHANNEL}")
        flow.run(app, "outbox.resend_sweep", {})
        got = [n.channel for n in listener.notifies(timeout=5, stop_after=1)]
    assert got == [comm.RESEND_CHANNEL]


def test_gateway_wake_runs_its_one_sweep() -> None:
    """The NOTIFY runs the same `Relay.sweep` as the gateway's 60 s ticker: no second path."""
    r = relay.Relay(pool=None, dsn="", clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]
    calls: list[str] = []

    async def sweep(*, wait: bool = False) -> None:
        calls.append("sweep")

    r.sweep = sweep  # type: ignore[method-assign]
    asyncio.run(r._on_notify(comm.RESEND_CHANNEL, ""))
    assert calls == ["sweep"]


def test_scheduler_main_loop_ticks_with_the_clock(
    app: Conn, dsns: Dsns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass of the loop: heartbeat, then a tick at the injected time."""
    monkeypatch.setattr(scheduler_main.config, "app_dsn", lambda: dsns.app)
    since = _last_job(app)
    at = datetime(2031, 3, 7, 3, 31, tzinfo=IST)
    scheduler_main.run(clock=lambda: at, should_stop=iter([False, True]).__next__,
                       sleep=lambda _: None)  # fmt: skip
    assert [p["job_key"] for p in _of_kind(app, "retention.sweep", since)] == [
        "retention.sweep:2031-03-07"
    ]
    row = app.execute("SELECT 1 FROM ops.heartbeat WHERE process = 'scheduler'").fetchone()
    assert row == (1,)
