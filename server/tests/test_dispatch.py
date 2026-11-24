"""Core job dispatch (spec §5.4, §6.3; ADR-013; Task 5 controller ruling 5).

Each job runs in its own transaction with its completion. A handler that raises leaves nothing
written and the job failed (recorded in a separate transaction); a handler that keeps an
audited refusal writes it and returns normally, and the refusal stays committed.

Runs in its own database (comm.event rows block the 0009 downgrade). People are fictional.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import psycopg
import pytest

from jarvis_server import comm, core_main, jobs
from jarvis_server.comm import Priority
from jarvis_server.core import dispatch, rules

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _empty_queue(owner: Conn) -> None:
    owner.execute("DELETE FROM ops.job")


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Asha Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


def _event(app: Conn, person: uuid.UUID, kind: str = "budget.80") -> uuid.UUID:
    return comm.record_event(
        app, kind, subject_type="budget", subject_id=f"m-{secrets.token_hex(4)}",
        person_id=person, details={"code": "fictional"},
    )  # fmt: skip


def _system_event(
    person: uuid.UUID, event_id: uuid.UUID, kind: str = "budget.80", **context: Any
) -> dict[str, Any]:
    return {
        "event_id": str(event_id),
        "event_kind": kind,
        "person_id": str(person),
        "context": context,
    }


def _job(app: Conn, job_id: int) -> tuple[str, str | None]:
    row = app.execute("SELECT status, last_error FROM ops.job WHERE id = %s", (job_id,)).fetchone()
    assert row is not None
    return row[0], row[1]


def _claim(app: Conn, job_id: int) -> jobs.Job:
    job = jobs.claim(app, "test-worker")
    assert job is not None and job.id == job_id
    return job


def _count(conn: Conn, sql: LiteralString, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


# Stub handlers for the transaction rules (fictional kinds, test-only).
REFUSAL_SUBJECT = "refusal"


@dispatch.register("test.keeps_refusal")
def _keeps_refusal(conn: Conn, job: jobs.Job) -> None:
    """Writes an audit-like row (append-only comm.event) for a refusal and returns normally."""
    comm.record_event(
        conn, "test.refused", subject_type=REFUSAL_SUBJECT, subject_id=job.payload["tag"],
        person_id=uuid.UUID(job.payload["person_id"]), details={"reason": "fictional_refusal"},
    )  # fmt: skip


@dispatch.register("test.raises")
def _raises(conn: Conn, job: jobs.Job) -> None:
    person = uuid.UUID(job.payload["person_id"])
    event_id = comm.record_event(
        conn, "test.partial", subject_type=REFUSAL_SUBJECT, subject_id=job.payload["tag"],
        person_id=person, details={},
    )  # fmt: skip
    comm.create_outbox(
        conn, person_id=person, event_id=event_id, priority=Priority.TODAY, kind="test.partial",
        body={"text": "never committed"}, group_key="test", expires_at=None,
    )  # fmt: skip
    raise RuntimeError("fictional handler failure")


def test_server_initiated_event_creates_outbox_without_node(app: Conn, person: uuid.UUID) -> None:
    """P2-E01: an internal event becomes a durable outbox item; no node is involved."""
    assert _count(app, "SELECT count(*) FROM gw.node WHERE person_id = %s", (person,)) == 0
    event_id = _event(app, person)
    job_id = jobs.enqueue(app, "system.event", _system_event(person, event_id))
    dispatch.handle(app, _claim(app, job_id))
    assert _job(app, job_id) == ("DONE", None)
    rows = app.execute(
        "SELECT priority, kind, state, person_id FROM comm.outbox WHERE event_id = %s",
        (event_id,),
    ).fetchall()
    assert rows == [("TODAY", "budget.80", "PENDING", person)]


def test_system_event_passes_context(app: Conn, person: uuid.UUID) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=10)
    event_id = _event(app, person, "approval.requested")
    payload = _system_event(person, event_id, "approval.requested", expires_at=expires.isoformat())
    job_id = jobs.enqueue(app, "system.event", payload)
    dispatch.handle(app, _claim(app, job_id))
    row = app.execute(
        "SELECT priority, expires_at FROM comm.outbox WHERE event_id = %s", (event_id,)
    ).fetchone()
    assert row == ("URGENT", expires)


def test_system_event_job_is_idempotent(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person)
    for _ in range(2):  # the same event dispatched twice (a re-run after a crash)
        job_id = jobs.enqueue(app, "system.event", _system_event(person, event_id))
        dispatch.handle(app, _claim(app, job_id))
        assert _job(app, job_id) == ("DONE", None)
    sql = "SELECT count(*) FROM comm.outbox WHERE event_id = %s"
    assert _count(app, sql, (event_id,)) == 1


def test_system_event_for_another_persons_event_fails(
    app: Conn, owner: Conn, person: uuid.UUID
) -> None:
    other = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Ravi Fictional', %s, 'test')",
        (other, HOUSEHOLD),
    )
    event_id = _event(app, other)
    job_id = jobs.enqueue(app, "system.event", _system_event(person, event_id))
    with pytest.raises(ValueError, match="person"):
        dispatch.handle(app, _claim(app, job_id))
    status, error = _job(app, job_id)
    assert status == "QUEUED" and error is not None and error.startswith("ValueError:")
    assert _count(app, "SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,)) == 0


def test_system_event_unknown_event_kind_fails_job(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person, "budget.90")
    job_id = jobs.enqueue(
        app, "system.event", _system_event(person, event_id, "budget.90"), max_attempts=1
    )
    with pytest.raises(rules.UnknownEvent):
        dispatch.handle(app, _claim(app, job_id))
    status, error = _job(app, job_id)
    assert status == "FAILED" and error is not None and error.startswith("UnknownEvent:")


def test_unknown_job_kind_fails_job(app: Conn) -> None:
    job_id = jobs.enqueue(app, "no.such.kind", {"n": 1})
    with pytest.raises(dispatch.UnknownJob, match=r"no\.such\.kind"):
        dispatch.handle(app, _claim(app, job_id))
    status, error = _job(app, job_id)
    assert status == "QUEUED" and error == "UnknownJob: job failed"  # never its text (S-5)
    with pytest.raises(dispatch.UnknownJob):  # the retry fails the same way, then for good
        dispatch.handle(app, _claim(app, job_id))
    assert _job(app, job_id)[0] == "FAILED"
    assert app.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_handler_that_raises_leaves_nothing_and_fails_job(
    app: Conn, dsns: Dsns, person: uuid.UUID
) -> None:
    tag = f"t-{secrets.token_hex(4)}"
    job_id = jobs.enqueue(app, "test.raises", {"person_id": str(person), "tag": tag})
    with pytest.raises(RuntimeError, match="fictional handler failure"):
        dispatch.handle(app, _claim(app, job_id))
    with psycopg.connect(dsns.app, autocommit=True) as other:  # what is committed
        sql = "SELECT count(*) FROM comm.event WHERE subject_type = %s AND subject_id = %s"
        assert _count(other, sql, (REFUSAL_SUBJECT, tag)) == 0
        sql = "SELECT count(*) FROM comm.outbox WHERE person_id = %s"
        assert _count(other, sql, (person,)) == 0
        status, error = _job(other, job_id)
    assert status == "QUEUED" and error == "RuntimeError: job failed"  # never its text (S-5)
    assert app.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_handler_committed_refusal_is_kept(app: Conn, dsns: Dsns, person: uuid.UUID) -> None:
    tag = f"t-{secrets.token_hex(4)}"
    job_id = jobs.enqueue(app, "test.keeps_refusal", {"person_id": str(person), "tag": tag})
    dispatch.handle(app, _claim(app, job_id))
    with psycopg.connect(dsns.app, autocommit=True) as other:  # a separate connection sees it
        row = other.execute(
            "SELECT kind, details FROM comm.event WHERE subject_type = %s AND subject_id = %s",
            (REFUSAL_SUBJECT, tag),
        ).fetchall()
        assert row == [("test.refused", {"reason": "fictional_refusal"})]
        assert _job(other, job_id) == ("DONE", None)


def test_handle_needs_an_idle_connection(app: Conn) -> None:
    job_id = jobs.enqueue(app, "noop")
    job = _claim(app, job_id)
    with app.transaction(), pytest.raises(dispatch.TransactionNotIdle):
        dispatch.handle(app, job)
    assert _job(app, job_id)[0] == "RUNNING"  # untouched: the caller still owns its transaction


def test_register_twice_is_refused() -> None:
    with pytest.raises(ValueError, match=r"system\.event"):
        dispatch.register("system.event")(lambda conn, job: None)


def test_core_dispatches_through_handle() -> None:
    assert "HANDLERS" not in vars(core_main) and "_noop" not in vars(core_main)
    assert vars(core_main)["dispatch"] is dispatch


# --- the core loop: polling works without NOTIFY; NOTIFY only wakes it early --------------------


class _Loop:
    """core_main.run_loop in a thread, on its own connections; re-raises the thread's error."""

    def __init__(self, dsns: Dsns, *, poll_seconds: float, listen: bool) -> None:
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.listening = threading.Event()
        self._dsns = dsns
        self._thread = threading.Thread(target=self._run, args=(poll_seconds, listen), daemon=True)
        self._thread.start()
        assert self.listening.wait(10), "loop never started"

    def _run(self, poll_seconds: float, listen: bool) -> None:
        try:
            with psycopg.connect(self._dsns.app, autocommit=True) as conn:
                listener = core_main.open_listener(self._dsns.app) if listen else None
                try:
                    self.listening.set()
                    core_main.run_loop(
                        conn, listener, "test-loop", poll_seconds=poll_seconds,
                        should_stop=self.stop.is_set,
                    )  # fmt: skip
                finally:
                    if listener is not None:
                        listener.close()
        except BaseException as e:
            self.error = e
        finally:
            self.listening.set()

    def close(self, app: Conn) -> None:
        self.stop.set()
        app.execute("SELECT pg_notify('jarvis_jobs', 'stop')")  # wake a long wait at once
        self._thread.join(15)
        assert not self._thread.is_alive(), "loop did not stop"
        if self.error is not None:
            raise self.error


def _wait_done(app: Conn, job_id: int, seconds: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if _job(app, job_id)[0] == "DONE":
            return time.monotonic() - start
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} not done after {seconds}s: {_job(app, job_id)}")


@pytest.mark.parametrize("listen", [True, False])
def test_loop_processes_job_without_notify(
    app: Conn, dsns: Dsns, person: uuid.UUID, listen: bool
) -> None:
    loop = _Loop(dsns, poll_seconds=0.3, listen=listen)
    try:
        time.sleep(0.5)  # the loop has found the queue empty and is waiting
        event_id = _event(app, person)
        job_id = jobs.enqueue(app, "system.event", _system_event(person, event_id))  # no NOTIFY
        _wait_done(app, job_id, 10)
        sql = "SELECT count(*) FROM comm.outbox WHERE event_id = %s"
        assert _count(app, sql, (event_id,)) == 1
    finally:
        loop.close(app)


def test_notify_wakes_the_loop_early(app: Conn, dsns: Dsns, person: uuid.UUID) -> None:
    loop = _Loop(dsns, poll_seconds=60, listen=True)  # polling alone would take a minute
    try:
        time.sleep(0.5)
        event_id = _event(app, person)
        with app.transaction():
            job_id = jobs.enqueue(app, "system.event", _system_event(person, event_id))
            app.execute("SELECT pg_notify('jarvis_jobs', %s)", (str(job_id),))
        assert _wait_done(app, job_id, 10) < 10
    finally:
        loop.close(app)


def test_loop_survives_a_failing_job(app: Conn, dsns: Dsns, person: uuid.UUID) -> None:
    loop = _Loop(dsns, poll_seconds=0.2, listen=False)
    try:
        bad = jobs.enqueue(app, "no.such.kind", max_attempts=1)
        event_id = _event(app, person)
        good = jobs.enqueue(app, "system.event", _system_event(person, event_id))
        _wait_done(app, good, 10)
        assert _job(app, bad)[0] == "FAILED"
    finally:
        loop.close(app)


# --- final review S-5: no exception text (row content) in logs or ops.job.last_error ----------

SECRET = "Fictional private utterance 7731"


@dispatch.register("test.leaks_row_content")
def _leaks_row_content(conn: Conn, job: jobs.Job) -> None:
    """A database error whose text carries a value, as a constraint violation's
    `DETAIL: Failing row contains (...)` carries the row."""
    conn.execute("SELECT %s::integer", (SECRET,))


@dispatch.register("test.raises_with_content")
def _raises_with_content(conn: Conn, job: jobs.Job) -> None:
    raise ValueError(f"bad value {SECRET}")


@pytest.mark.parametrize(
    ("kind", "stored"),
    [
        ("test.leaks_row_content", "InvalidTextRepresentation: sqlstate 22P02"),
        ("test.raises_with_content", "ValueError: job failed"),
    ],
)
def test_failure_stores_and_logs_no_exception_text(
    app: Conn, caplog: pytest.LogCaptureFixture, kind: str, stored: str
) -> None:
    """Only the exception's class and a fixed code reach `last_error` and the log."""
    job_id = jobs.enqueue(app, kind, {}, max_attempts=1)
    with caplog.at_level("INFO"), pytest.raises(Exception):  # noqa: B017
        dispatch.handle(app, _claim(app, job_id))
    assert _job(app, job_id) == ("FAILED", stored)
    assert SECRET not in caplog.text
    assert stored in caplog.text


def test_loop_logs_no_exception_text(
    app: Conn, dsns: Dsns, caplog: pytest.LogCaptureFixture
) -> None:
    loop = _Loop(dsns, poll_seconds=0.2, listen=False)
    try:
        with caplog.at_level("INFO"):
            bad = jobs.enqueue(app, "test.leaks_row_content", max_attempts=1)
            deadline = time.monotonic() + 10
            while _job(app, bad)[0] != "FAILED":
                assert time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(0.2)  # the loop's own log line follows the job's failure
    finally:
        loop.close(app)
    assert "not completed" in caplog.text
    assert SECRET not in caplog.text
