"""Retention (spec §5.7, R12-30, R12-31; plan Task 13, ruling 5).

`ops.retention_sweep(p_now)` (migration 0012) deletes past-retention rows and writes one SYSTEM
`RETENTION` audit row per table with the real count, in one transaction. `retention.sweep`
calls it from the `retention.sweep` job through `audited_call_in_job`.

- `conv.stream_chunk`, with its `conv.stream_cursor` rows: 24 h after the reply's final chunk;
  a reply with no final chunk, 24 h after its latest.
- `conv.inbound`: 30 days, unless an ACTIVE reservation or a QUEUED or RUNNING job names it.
- `comm.delivery`: its item is terminal, and 90 days after its last send or receipt (never
  sent: after the item's `state_changed_at`).
- `gw.node_connection`: closed, 90 days after `closed_at`.
- `ops.brain_usage`: 13 months.
- `ops.brain_reservation`: 13 months, not ACTIVE, no usage row left.
- `ops.job`: DONE or FAILED, 30 days after `finished_at`, unless a kept reservation names it
  (R12-32). QUEUED and RUNNING jobs never.

Everything else is kept. Rows are written as the owner with past timestamps, so the database's
own `now()` is the sweep's time. Runs in its own database; every person is fictional.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server import comm, jobs, migrate, retention
from jarvis_server.comm import Priority
from jarvis_server.core import board
from jarvis_server.dbcall import NotInJob, job_transaction

from .conftest import Dsns, fresh_database
from .helpers import flow

Conn = psycopg.Connection[Any]
H, D = timedelta(hours=1), timedelta(days=1)
TABLES = (
    "conv.stream_chunk",
    "conv.stream_cursor",
    "conv.inbound",
    "comm.delivery",
    "gw.node_connection",
    "ops.brain_usage",
    "ops.brain_reservation",
    "ops.job",
)
KEPT = ("conv.session", "conv.message", "comm.outbox", "comm.event", "audit.event")
SIGNATURE = "ops.retention_sweep(timestamp with time zone)"


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _empty(owner: Conn) -> Iterator[None]:
    """Each test starts with no retained rows and no runnable job."""
    owner.execute(
        "DELETE FROM conv.stream_cursor; DELETE FROM conv.stream_chunk; DELETE FROM comm.delivery;"
        " DELETE FROM gw.node_connection; DELETE FROM ops.brain_usage;"
        " DELETE FROM ops.brain_reservation; DELETE FROM conv.inbound;"
        " UPDATE ops.job SET status = 'DONE' WHERE status IN ('QUEUED', 'RUNNING');"
        " DELETE FROM ops.job WHERE finished_at < now() - interval '20 days'"
    )
    yield


def _now(conn: Conn) -> datetime:
    return flow.db_now(conn)


@dataclass
class World:
    person: uuid.UUID
    node: uuid.UUID
    session: uuid.UUID


@pytest.fixture
def w(app: Conn, owner: Conn) -> World:
    person = flow.new_person(owner)
    return World(person, flow.new_node(owner, person), flow.open_session(app, person))


def _seq(conn: Conn) -> int:
    row = conn.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


def _audited(conn: Conn, since: int) -> dict[str, int]:
    """The counts in the retention audit rows written after `since`."""
    rows = conn.execute(
        "SELECT details->>'table', (details->>'deleted')::bigint FROM audit.event"
        " WHERE seq > %s AND entity_type = 'retention' ORDER BY seq",
        (since,),
    ).fetchall()
    return {str(t): int(n) for t, n in rows}


def _sweep(app: Conn) -> dict[str, int]:
    """Run one `retention.sweep` job through dispatch; return the counts it audited."""
    since = _seq(app)
    flow.run(app, "retention.sweep", {})
    return _audited(app, since)


def _count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


# --- rows --------------------------------------------------------------------------------------


def _chunks(owner: Conn, w: World, ages: list[tuple[timedelta, bool]]) -> uuid.UUID:
    """One reply: a SPEECH chunk per (age, final), seq 1, 2, ...; plus its cursor row."""
    reply = uuid.uuid4()
    for seq, (age, final) in enumerate(ages, start=1):
        owner.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, text, final,"
            " created_at) VALUES (%s, %s, %s, 'SPEECH', 'fictional', %s, now() - %s)",
            (reply, w.session, seq, final, age),
        )
    owner.execute(
        "INSERT INTO conv.stream_cursor (reply_id, node_id, acked_seq) VALUES (%s, %s, 1)",
        (reply, w.node),
    )
    return reply


def _replies(conn: Conn) -> set[uuid.UUID]:
    return {r[0] for r in conn.execute("SELECT DISTINCT reply_id FROM conv.stream_chunk")}


def _cursors(conn: Conn) -> set[uuid.UUID]:
    return {r[0] for r in conn.execute("SELECT reply_id FROM conv.stream_cursor")}


def _inbound(owner: Conn, w: World, age: timedelta) -> uuid.UUID:
    return flow.inbound(owner, w.node, w.person, "utterance", {"text": "fictional"},
                        received_at=_now(owner) - age)  # fmt: skip


def _ids(conn: Conn, sql: str) -> set[Any]:
    return {r[0] for r in conn.execute(sql)}  # type: ignore[arg-type]


def _item(app: Conn, owner: Conn, w: World, state: str = "PENDING",
          changed_age: timedelta = D * 0) -> uuid.UUID:  # fmt: skip
    """An outbox item; ended as `state`, `changed_age` ago (as the owner: only the comm
    functions change an item's state)."""
    event = comm.record_event(app, "budget.80", subject_type="budget", subject_id="2026-11",
                              person_id=w.person, details={})  # fmt: skip
    item = comm.create_outbox(
        app, person_id=w.person, event_id=event, priority=Priority.TODAY, kind="budget.80",
        body={"text": "fictional"}, group_key="budget", expires_at=None,
    )  # fmt: skip
    if state != "PENDING":
        owner.execute(
            "UPDATE comm.outbox SET state = %s, state_changed_at = now() - %s WHERE id = %s",
            (state, changed_age, item),
        )
    return item


def _delivery(owner: Conn, item: uuid.UUID, node: uuid.UUID, sent_age: timedelta | None,
              received_age: timedelta | None = None) -> None:  # fmt: skip
    if sent_age is None:
        owner.execute(
            "INSERT INTO comm.delivery (outbox_id, node_id) VALUES (%s, %s)", (item, node)
        )
        return
    owner.execute(
        "INSERT INTO comm.delivery (outbox_id, node_id, attempts, first_sent_at, last_sent_at,"
        " received_at) VALUES (%s, %s, 1, now() - %s, now() - %s, now() - %s::interval)",
        (item, node, sent_age, sent_age, received_age),
    )


def _connection(owner: Conn, node: uuid.UUID, opened_age: timedelta,
                closed_age: timedelta | None) -> int:  # fmt: skip
    row = owner.execute(
        "INSERT INTO gw.node_connection (node_id, opened_at, closed_at, close_reason)"
        " VALUES (%s, now() - %s, now() - %s::interval, CASE WHEN %s THEN 'closed' END)"
        " RETURNING id",
        (node, opened_age, closed_age, closed_age is not None),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _reservation(owner: Conn, age: timedelta, state: str = "SETTLED",
                 turn: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    job = owner.execute("INSERT INTO ops.job (kind, status) VALUES ('conv.turn', 'DONE')"
                        " RETURNING id").fetchone()  # fmt: skip
    assert job is not None
    owner.execute(
        "INSERT INTO ops.brain_budget (month, cap_inr) VALUES"
        " (date_trunc('month', (now() - %s) AT TIME ZONE 'Asia/Kolkata')::date, 500000)"
        " ON CONFLICT DO NOTHING",
        (age,),
    )
    row = owner.execute(
        "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day, reserved_inr, state,"
        " actual_inr, created_at, lease_until) VALUES (%s, %s,"
        " date_trunc('month', (now() - %s) AT TIME ZONE 'Asia/Kolkata')::date,"
        " ((now() - %s) AT TIME ZONE 'Asia/Kolkata')::date, 100, %s,"
        " CASE WHEN %s = 'SETTLED' THEN 90 END, now() - %s, now() - %s + interval '5 minutes')"
        " RETURNING id",
        (job[0], turn, age, age, state, state, age, age),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _usage(owner: Conn, reservation: uuid.UUID, age: timedelta) -> uuid.UUID:
    row = owner.execute(
        "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens, output_tokens, cost_inr,"
        " at) VALUES (%s, 'claude-test', 10, 2, 90, now() - %s) RETURNING id",
        (reservation, age),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


# --- the windows --------------------------------------------------------------------------------


def test_retention_deletes_only_past_window(app: Conn, owner: Conn, w: World) -> None:
    """A chunk 25 h after its final → deleted; 23 h → kept; messages never."""
    old_final = _chunks(owner, w, [(H * 26, False), (H * 25, True)])
    young_final = _chunks(owner, w, [(H * 30, False), (H * 23, True)])
    owner.execute(
        "INSERT INTO conv.message (session_id, seq, role, person_id, node_id, text, created_at)"
        " VALUES (%s, 1, 'PERSON', %s, %s, 'fictional', now() - interval '400 days')",
        (w.session, w.person, w.node),
    )
    kept_before = {t: _count(app, t) for t in KEPT}
    counts = _sweep(app)
    assert _replies(app) == {young_final} and _cursors(app) == {young_final}
    assert old_final not in _replies(app)
    assert counts["conv.stream_chunk"] == 2 and counts["conv.stream_cursor"] == 1
    after = {t: _count(app, t) for t in KEPT}
    assert {t: after[t] for t in KEPT if t != "audit.event"} == {
        t: kept_before[t] for t in KEPT if t != "audit.event"
    }
    assert after["audit.event"] == kept_before["audit.event"] + len(TABLES)  # only added to


def test_reply_without_a_final_chunk_goes_24h_after_its_latest(
    app: Conn, owner: Conn, w: World
) -> None:
    """R12-31 (1)."""
    stale = _chunks(owner, w, [(H * 40, False), (H * 25, False)])
    live = _chunks(owner, w, [(H * 40, False), (H * 2, False)])
    _sweep(app)
    assert _replies(app) == {live} and stale not in _cursors(app)


def test_inbound_after_30_days_unless_still_needed(app: Conn, owner: Conn, w: World) -> None:
    old, young = _inbound(owner, w, D * 31), _inbound(owner, w, D * 29)
    held = _inbound(owner, w, D * 31)  # an ACTIVE reservation's turn
    _reservation(owner, D * 31, "ACTIVE", turn=held)
    queued = _inbound(owner, w, D * 31)  # a queued turn job still names it
    jobs.enqueue(app, "conv.turn", {"inbound_id": str(queued)})
    done = _inbound(owner, w, D * 31)  # its job finished: nothing needs it
    owner.execute("INSERT INTO ops.job (kind, payload, status) VALUES ('conv.turn', %s, 'DONE')",
                  (Jsonb({"inbound_id": str(done)}),))  # fmt: skip
    settled = _inbound(owner, w, D * 31)  # a SETTLED reservation keeps only the ID
    _reservation(owner, D * 31, "SETTLED", turn=settled)
    counts = _sweep(app)
    assert _ids(app, "SELECT id FROM conv.inbound") == {young, held, queued}
    assert counts["conv.inbound"] == 3
    assert {old, done, settled}.isdisjoint(_ids(app, "SELECT id FROM conv.inbound"))


def test_delivery_after_90_days_of_a_terminal_item(app: Conn, owner: Conn, w: World) -> None:
    other = flow.new_node(owner, w.person)
    gone = _item(app, owner, w, "ACKNOWLEDGED")
    _delivery(owner, gone, w.node, D * 91)  # sent 91 days ago: deleted
    _delivery(owner, gone, other, D * 95, received_age=D * 89)  # received 89 days ago: kept
    young = _item(app, owner, w, "EXPIRED")
    _delivery(owner, young, w.node, D * 89)
    live = _item(app, owner, w)  # still open: its delivery state is live, whatever its age
    _delivery(owner, live, w.node, D * 200)
    # R12-31 (2): never sent; counted from the item's end.
    never_old = _item(app, owner, w, "SUPERSEDED", changed_age=D * 91)
    _delivery(owner, never_old, w.node, None)
    never_young = _item(app, owner, w, "SUPERSEDED", changed_age=D * 89)
    _delivery(owner, never_young, w.node, None)
    never_open = _item(app, owner, w)
    _delivery(owner, never_open, w.node, None)
    counts = _sweep(app)
    left = set(app.execute("SELECT outbox_id, node_id FROM comm.delivery").fetchall())
    assert left == {(gone, other), (young, w.node), (live, w.node), (never_young, w.node),
                    (never_open, w.node)}  # fmt: skip
    assert counts["comm.delivery"] == 2
    items = {gone, young, live, never_old, never_young, never_open}
    assert items <= _ids(app, "SELECT id FROM comm.outbox")  # the items themselves are kept


def test_closed_connections_after_90_days_never_open_ones(app: Conn, owner: Conn, w: World) -> None:
    gone = _connection(owner, w.node, D * 100, D * 91)
    young = _connection(owner, w.node, D * 100, D * 89)
    other = flow.new_node(owner, w.person)
    still_open = _connection(owner, other, D * 200, None)
    counts = _sweep(app)
    assert _ids(app, "SELECT id FROM gw.node_connection") == {young, still_open}
    assert gone not in _ids(app, "SELECT id FROM gw.node_connection")
    assert counts["gw.node_connection"] == 1


def test_budget_history_after_13_months(app: Conn, owner: Conn) -> None:
    old = _reservation(owner, D * 400)  # past 13 months, with an old usage row: both go
    old_usage = _usage(owner, old, D * 400)
    young = _reservation(owner, D * 360)
    young_usage = _usage(owner, young, D * 360)
    active = _reservation(owner, D * 400, "ACTIVE")  # never while ACTIVE
    referenced = _reservation(owner, D * 400)  # a kept usage row still references it
    kept_usage = _usage(owner, referenced, D * 300)
    counts = _sweep(app)
    assert _ids(app, "SELECT id FROM ops.brain_reservation") == {young, active, referenced}
    assert _ids(app, "SELECT id FROM ops.brain_usage") == {young_usage, kept_usage}
    assert old_usage not in _ids(app, "SELECT id FROM ops.brain_usage")
    assert (counts["ops.brain_usage"], counts["ops.brain_reservation"]) == (1, 1)
    assert old not in _ids(app, "SELECT id FROM ops.brain_reservation")


def test_thirteen_months_is_calendar_months(app: Conn, owner: Conn) -> None:
    """The window is 13 calendar months back from now (at least 393 days), not 390 days."""
    inside = _reservation(owner, D * 392)
    outside = _reservation(owner, D * 400)
    _sweep(app)
    assert _ids(app, "SELECT id FROM ops.brain_reservation") == {inside}
    assert outside not in _ids(app, "SELECT id FROM ops.brain_reservation")


def _job(owner: Conn, status: str, *, finished_age: timedelta | None,
         age: timedelta = D * 60) -> int:  # fmt: skip
    """A job created `age` ago, finished `finished_age` ago (None: no finished_at)."""
    row = owner.execute(
        "INSERT INTO ops.job (kind, payload, status, run_at, created_at, finished_at)"
        " VALUES ('noop', '{}', %s, now() - %s, now() - %s, now() - %s::interval) RETURNING id",
        (status, age, age, finished_age),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _job_ids(conn: Conn) -> set[int]:
    return _ids(conn, "SELECT id FROM ops.job")


def test_finished_jobs_after_30_days(app: Conn, owner: Conn) -> None:
    """R12-32: DONE and FAILED jobs go 30 days after they finished, and stay at 29."""
    gone = {_job(owner, s, finished_age=D * 30) for s in ("DONE", "FAILED")}
    kept = {_job(owner, s, finished_age=D * 29) for s in ("DONE", "FAILED")}
    counts = _sweep(app)
    assert gone.isdisjoint(_job_ids(app)) and kept <= _job_ids(app)
    assert counts["ops.job"] == 2


def test_queued_and_running_jobs_are_never_deleted(app: Conn, owner: Conn) -> None:
    """R12-32: an unfinished job is kept however old, even with a stale finished_at."""
    old = D * 400
    unfinished = {
        _job(owner, s, finished_age=f, age=old) for s in ("QUEUED", "RUNNING") for f in (None, old)
    }
    counts = _sweep(app)
    assert unfinished <= _job_ids(app)
    assert counts["ops.job"] == 0


def test_a_job_a_kept_reservation_names_is_kept(app: Conn, owner: Conn) -> None:
    """R12-30: `ops.brain_reservation.job_id` references the job, and reservations are kept 13
    months, so their finished job stays until the reservation itself is deleted."""
    reservation = _reservation(owner, D * 40)
    owner.execute(
        "UPDATE ops.job SET finished_at = now() - interval '40 days' WHERE id ="
        " (SELECT job_id FROM ops.brain_reservation WHERE id = %s)",
        (reservation,),
    )
    row = owner.execute(
        "SELECT job_id FROM ops.brain_reservation WHERE id = %s", (reservation,)
    ).fetchone()
    assert row is not None
    counts = _sweep(app)
    assert row[0] in _job_ids(app) and counts["ops.job"] == 0
    # Once the reservation has gone (13 months), the same sweep deletes its job.
    owner.execute(
        "UPDATE ops.brain_reservation SET created_at = now() - interval '400 days' WHERE id = %s",
        (reservation,),
    )
    counts = _sweep(app)
    assert counts["ops.brain_reservation"] == 1 and counts["ops.job"] == 1
    assert row[0] not in _job_ids(app)


# --- audit and the time it may be given ---------------------------------------------------------


def test_retention_writes_counts_to_audit(app: Conn, owner: Conn, w: World) -> None:
    _chunks(owner, w, [(H * 30, True)])
    _inbound(owner, w, D * 40)
    _inbound(owner, w, D * 40)
    since = owner.execute("SELECT max(seq) FROM audit.event").fetchone()
    assert since is not None
    flow.run(app, "retention.sweep", {})
    rows = app.execute(
        "SELECT actor_kind::text, actor_id, entity_type, entity_id, action::text, outcome::text,"
        " reason, from_state, to_state, details FROM audit.event WHERE seq > %s ORDER BY seq",
        (since[0],),
    ).fetchall()
    assert [r[9]["table"] for r in rows] == list(TABLES)  # one row per table, in this order
    assert all(r[:3] == ("SYSTEM", "system", "retention") for r in rows)
    assert all(r[4:9] == ("RETENTION", "APPLIED", "retention-sweep", None, None) for r in rows)
    counts = {r[9]["table"]: r[9]["deleted"] for r in rows}
    assert counts == {**{t: "0" for t in TABLES}, "conv.stream_chunk": "1",
                      "conv.stream_cursor": "1", "conv.inbound": "2"}  # fmt: skip
    assert all(set(r[9]) == {"table", "deleted", "cutoff"} for r in rows)
    # The entity ID is fixed per table, so a table's runs line up.
    ids = {r[9]["table"]: r[3] for r in rows}
    flow.run(app, "retention.sweep", {})
    again = app.execute(
        "SELECT details->>'table', entity_id, details->>'deleted' FROM audit.event"
        " WHERE seq > %s AND entity_type = 'retention' ORDER BY seq OFFSET %s",
        (since[0], len(TABLES)),
    ).fetchall()
    assert [(t, ids[t], "0") for t in TABLES] == [tuple(r) for r in again]  # zero rows too


def test_an_earlier_time_only_narrows_the_windows(app: Conn, owner: Conn, w: World) -> None:
    """`retention.sweep(conn, now)` returns the counts it audited; an earlier `now` keeps more."""
    reply = _chunks(owner, w, [(H * 25, True)])
    _inbound(owner, w, D * 40)
    since = _seq(app)
    with app.transaction(), job_transaction(app):
        counts = retention.sweep(app, _now(app) - D * 2)
    assert counts == _audited(app, since) and set(counts) == set(TABLES)
    assert counts["conv.stream_chunk"] == 0 and reply in _replies(app)
    assert counts["conv.inbound"] == 1  # 40 days is past 30 even from two days ago


def test_a_later_time_is_refused(app: Conn, owner: Conn, w: World) -> None:
    """R12-30: `p_now > now()` is a malformed call, so nothing can be deleted early."""
    reply = _chunks(owner, w, [(H * 23, True)])
    since = owner.execute("SELECT max(seq) FROM audit.event").fetchone()
    for when in ("now() + interval '2 hours'", "NULL"):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            app.execute(f"SELECT * FROM ops.retention_sweep({when})")
    assert reply in _replies(app)
    assert owner.execute("SELECT max(seq) FROM audit.event").fetchone() == since


def test_sweep_runs_only_in_a_job(app: Conn) -> None:
    with pytest.raises(NotInJob):
        retention.sweep(app, _now(app))


# --- privileges (R12-30, P2-S6) -------------------------------------------------------------------


def test_only_the_app_may_execute_the_sweep(owner: Conn, gateway: Conn) -> None:
    row = owner.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " (SELECT array_agg(a.grantee::regrole::text ORDER BY 1) FROM aclexplode(p.proacl) a"
        "  WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner)"
        " FROM pg_proc p WHERE p.oid = %s::regprocedure",
        (SIGNATURE,),
    ).fetchone()
    assert row == (True, "jarvis_owner", ["search_path=pg_catalog, pg_temp"], ["jarvis_app"])
    for role in ("jarvis_gateway", "public"):
        assert owner.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, SIGNATURE)
        ).fetchone() == (False,), role
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        gateway.execute("SELECT * FROM ops.retention_sweep(now())")


def test_no_table_grant_was_added(owner: Conn) -> None:
    """The sweep deletes as its owner: the app still may not delete connection rows, and
    neither role may delete anything else it couldn't before."""
    for role, table, ok in (
        ("jarvis_app", "gw.node_connection", False),
        ("jarvis_app", "comm.outbox", False),
        ("jarvis_app", "conv.message", False),
        ("jarvis_gateway", "conv.stream_chunk", False),
        ("jarvis_app", "conv.inbound", True),  # unchanged since 0009
    ):
        assert owner.execute(
            "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, table)
        ).fetchone() == (ok,), (role, table)


# --- migration 0015 (receipt code `no_receipt`, 26 Nov) ----------------------------------------


def test_0015_down_and_up() -> None:
    """Down to 0014: `ledger.refuse_receipt` refuses `no_receipt` again. Up: it is accepted,
    audited against the approval, and still only jarvis_app may call the function."""
    call = "SELECT ledger.refuse_receipt(%s, %s, 'no_receipt')"
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0014")
        with psycopg.connect(d.app, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                conn.execute(call, (uuid.uuid4(), uuid.uuid4()))
            assert conn.execute(
                "SELECT ledger.refuse_receipt(%s, %s, 'listing_missing')",
                (uuid.uuid4(), uuid.uuid4()),
            ).fetchone() == ("listing_missing",)
        migrate.upgrade(d.owner, "head")
        ref = uuid.uuid4()
        with psycopg.connect(d.app, autocommit=True) as conn:
            assert conn.execute(call, (ref, uuid.uuid4())).fetchone() == ("no_receipt",)
            assert conn.execute(
                "SELECT entity_type, outcome::text, reason FROM audit.event WHERE entity_id = %s",
                (ref,),
            ).fetchall() == [("approval", "REJECTED", "no_receipt")]
        with psycopg.connect(d.owner, autocommit=True) as conn:
            fn = "ledger.refuse_receipt(uuid, uuid, text)"
            assert conn.execute(
                "SELECT prosecdef, has_function_privilege('jarvis_app', %s, 'EXECUTE'),"
                " has_function_privilege('jarvis_gateway', %s, 'EXECUTE'),"
                " has_function_privilege('public', %s, 'EXECUTE')"
                " FROM pg_proc WHERE oid = %s::regprocedure",
                (fn, fn, fn, fn),
            ).fetchone() == (True, True, False, False)


# --- migration 0016 (the Board's panel store) ---------------------------------------------------


def test_0016_down_and_up() -> None:
    """Down to 0015: the `board` schema is gone and `board.state` is no longer an inbound
    kind. Up: both are back, with the same grants on `conv.record_inbound`. With a panel
    stored, the downgrade refuses (nothing is destroyed)."""
    fn = "conv.record_inbound(bigint, uuid, text, jsonb, jsonb)"
    grants = (
        "SELECT prosecdef, has_function_privilege('jarvis_gateway', %s, 'EXECUTE'),"
        " has_function_privilege('jarvis_app', %s, 'EXECUTE'),"
        " has_function_privilege('public', %s, 'EXECUTE') FROM pg_proc WHERE oid = %s::regprocedure"
    )
    # Owner, search_path and the body: at 0015 the body is 0009's, word for word.
    shape = (
        "SELECT pg_get_userbyid(proowner), proconfig, prosrc FROM pg_proc"
        " WHERE oid = %s::regprocedure"
    )
    schema = "SELECT count(*) FROM pg_namespace WHERE nspname = 'board'"
    kinds = (
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'conv.inbound'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%%utterance%%'"
    )
    with fresh_database() as d:
        with psycopg.connect(d.owner, autocommit=True) as conn:
            before = conn.execute(grants, (fn, fn, fn, fn)).fetchone()
            assert before == (True, True, False, False)
            owner_, config, at_head = conn.execute(shape, (fn,)).fetchone()  # type: ignore[misc]
            assert (owner_, config) == ("jarvis_owner", ["search_path=pg_catalog, pg_temp"])
            assert at_head.count("WHEN 'board.state' THEN 'board.state'") == 1
            assert "board.state" in str(conn.execute(kinds).fetchone())
        migrate.downgrade(d.owner, "0015")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(schema).fetchone() == (0,)
            assert conn.execute(grants, (fn, fn, fn, fn)).fetchone() == before
            owner_, config, at_0015 = conn.execute(shape, (fn,)).fetchone()  # type: ignore[misc]
            assert (owner_, config) == ("jarvis_owner", ["search_path=pg_catalog, pg_temp"])
            assert at_0015 == at_head.replace("        WHEN 'board.state' THEN 'board.state'\n", "")
            assert "board" not in at_0015
            assert "board.state" not in str(conn.execute(kinds).fetchone())
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(schema).fetchone() == (1,)
            assert conn.execute(grants, (fn, fn, fn, fn)).fetchone() == before
            person = uuid.UUID(
                str(conn.execute("SELECT id FROM ledger.person LIMIT 1").fetchone()[0])
            )  # type: ignore[index]
        with psycopg.connect(d.app, autocommit=True) as conn:
            board.create(conn, person_id=person, session_id=None, kind="text",
                         title="A fictional panel", content="Fictional.", cost_paise=0)  # fmt: skip
        with pytest.raises(Exception, match="Board panels exist"):
            migrate.downgrade(d.owner, "0015")
        with psycopg.connect(d.app, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM board.panel").fetchone() == (1,)


# --- migration 0014 (R12-37) -------------------------------------------------------------------


def test_0014_down_and_up() -> None:
    """Down to 0013: no `in_reply_to` column. Up again: it is back, and NULL by default."""
    column = (
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_schema = 'conv' AND table_name = 'stream_chunk'"
        " AND column_name = 'in_reply_to'"
    )
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0013")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(column).fetchone() == (0,)
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(column).fetchone() == (1,)


# --- migration 0013 (R12-32) -------------------------------------------------------------------


def test_0013_down_and_up() -> None:
    """Down to 0012: no job index, and the 0012 sweep (seven tables, jobs kept). Up again: both."""
    index = "SELECT to_regclass('ops.job_one_per_key')"
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0012")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(index).fetchone() == (None,)
            old = _job(conn, "DONE", finished_age=D * 40)
        with psycopg.connect(d.app, autocommit=True) as conn:
            rows = conn.execute("SELECT * FROM ops.retention_sweep(now())").fetchall()
            assert [r[0] for r in rows] == list(TABLES[:-1])
            assert old in _job_ids(conn)
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.app, autocommit=True) as conn:
            assert conn.execute(index).fetchone() == ("ops.job_one_per_key",)
            rows = conn.execute("SELECT * FROM ops.retention_sweep(now())").fetchall()
            assert [r[0] for r in rows] == list(TABLES)
            assert old not in _job_ids(conn)


# --- migration 0012 ----------------------------------------------------------------------------


def test_0012_down_and_up_without_rows() -> None:
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0011")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            labels = [r[0] for r in conn.execute(
                "SELECT unnest(enum_range(NULL::audit.action))::text")]  # fmt: skip
            assert "RETENTION" not in labels and "SET_VISIBILITY" not in labels
            assert conn.execute("SELECT to_regprocedure(%s)", (SIGNATURE,)).fetchone() == (None,)
            with pytest.raises(psycopg.errors.InvalidParameterValue):  # 0007's rules again
                conn.execute(
                    "SELECT audit.record_memory('SYSTEM', 'system', 'm-0001', 'SET_VISIBILITY',"
                    " 'STATED', 'STATED', 'APPLIED', 'set-visibility', %s)",
                    ("a" * 40,),
                )
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.app, autocommit=True) as conn:
            rows = conn.execute("SELECT * FROM ops.retention_sweep(now())").fetchall()
            assert [r[0] for r in rows] == list(TABLES)


def test_0012_downgrade_refuses_once_retention_rows_exist() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.app, autocommit=True) as conn:
            conn.execute("SELECT * FROM ops.retention_sweep(now())")
        with pytest.raises(Exception, match="0012 cannot be undone"):
            migrate.downgrade(d.owner, "0011")
