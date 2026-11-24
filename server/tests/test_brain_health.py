"""Brain health (spec §10.2): DOWN after 2 consecutive failures, back after a success, and the
`brain.probe` job.

Runs in its own database: the state is one global `ops.heartbeat` row, and event and outbox
rows block the 0009 downgrade. People are fictional.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import comm, jobs, nodes
from jarvis_server.brain import (
    BrainCallFailed,
    BrainEvent,
    BrainRequest,
    CallPhase,
    FakeBrain,
    Speech,
    budget,
    health,
)
from jarvis_server.core import dispatch, rules
from jarvis_server.signing import fingerprint

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
MODEL = "claude-opus-5-5"
CFP = fingerprint(bytes(range(32)))
GFP = fingerprint(bytes(range(32, 64)))


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _fresh_state(owner: Conn) -> Iterator[None]:
    owner.execute("DELETE FROM ops.heartbeat WHERE process = 'brain'")
    owner.execute("UPDATE ops.brain_reservation SET state = 'RELEASED' WHERE state = 'ACTIVE'")
    yield
    health.set_probe_brain(None)


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Asha Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


def test_down_after_two_failures_back_after_success(app: Conn) -> None:
    assert health.is_down(app) is False  # no row yet: up
    assert health.record_failure(app) is False  # one failure: still up
    assert health.is_down(app) is False
    assert health.record_success(app) is False  # was up: nothing to report
    assert health.record_failure(app) is False  # the count was reset by the success
    assert health.record_failure(app) is True  # two in a row: it has just gone DOWN
    assert health.is_down(app) is True
    assert health.record_failure(app) is False  # already DOWN: not "just gone down" again
    assert health.is_down(app) is True
    assert health.record_success(app) is True  # it has just come back
    assert health.is_down(app) is False
    assert health.record_success(app) is False
    row = app.execute(
        "SELECT details FROM ops.heartbeat WHERE process = %s", (health.HEARTBEAT_NAME,)
    ).fetchone()
    assert row == ({"down": False, "consecutive_failures": 0},)


def _down(app: Conn) -> None:
    health.record_failure(app)
    health.record_failure(app)
    assert health.is_down(app)


def _run_probe(app: Conn) -> int:
    job_id = jobs.enqueue(app, "brain.probe")
    claimed = app.execute(
        "UPDATE ops.job SET status = 'RUNNING', attempts = 1, locked_by = 'test',"
        " locked_until = now() + interval '5 minutes' WHERE id = %s",
        (job_id,),
    )
    assert claimed.rowcount == 1
    dispatch.handle(app, jobs.Job(id=job_id, kind="brain.probe", payload={}, attempts=1,
                                  max_attempts=2))  # fmt: skip
    return job_id


def _probe_reservations(app: Conn, job_id: int) -> list[tuple[str, int | None]]:
    rows = app.execute(
        "SELECT state, actual_inr FROM ops.brain_reservation WHERE job_id = %s", (job_id,)
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def test_probe_when_up_does_nothing(app: Conn) -> None:
    brain = FakeBrain([Speech("ok")], usage=(10, 2))
    health.set_probe_brain(lambda: (brain, MODEL))
    job_id = _run_probe(app)
    assert brain.requests == []
    assert _probe_reservations(app, job_id) == []


def test_probe_success_brings_brain_back(app: Conn, person: uuid.UUID) -> None:
    _down(app)
    event = comm.record_event(app, "brain.down", subject_type="brain", subject_id="brain",
                              person_id=person, details={})  # fmt: skip
    [down_item] = rules.notify(app, "brain.down", person_id=person, event_id=event, context={})
    brain = FakeBrain([Speech("ok")], usage=(30, 2))
    health.set_probe_brain(lambda: (brain, MODEL))
    job_id = _run_probe(app)
    assert len(brain.requests) == 1
    req = brain.requests[0]
    assert req.tools == [] and req.max_tokens == health.PROBE_MAX_TOKENS
    assert health.is_down(app) is False
    # Reserved like any other call, then settled from the reported usage (30 in, 2 out).
    assert _probe_reservations(app, job_id) == [("SETTLED", 2)]  # ceil(1.2 + 0.4)
    # CLAUDE_BACK: a brain.back item that supersedes the person's open "down" item.
    back = app.execute(
        "SELECT priority, supersedes_id FROM comm.outbox WHERE person_id = %s"
        " AND kind = 'brain.back'",
        (person,),
    ).fetchall()
    assert back == [("DIGEST", down_item)]
    state = app.execute("SELECT state FROM comm.outbox WHERE id = %s", (down_item,)).fetchone()
    assert state == ("SUPERSEDED",)


@pytest.mark.parametrize(
    ("phase", "state"), [(CallPhase.CONNECT, "RELEASED"), (CallPhase.SEND, "LAPSED")]
)
def test_probe_failure_keeps_brain_down(app: Conn, phase: CallPhase, state: str) -> None:
    _down(app)
    brain = FakeBrain([BrainCallFailed(phase, status=529 if phase is CallPhase.SEND else None)])
    health.set_probe_brain(lambda: (brain, MODEL))
    job_id = _run_probe(app)
    assert len(brain.requests) == 1
    assert health.is_down(app) is True
    # CONNECT: nothing reached Claude, so the reservation is given back. SEND: it may have
    # been billed, so it lapses at full value (§10.1 step 4).
    assert _probe_reservations(app, job_id) == [(state, None)]


def test_probe_without_a_brain_fails_the_job(app: Conn) -> None:
    _down(app)
    with pytest.raises(health.NoProbeBrain):
        _run_probe(app)


def test_core_registers_both_job_kinds() -> None:
    """The core process imports the brain modules, so both job kinds have handlers."""
    probe = (
        "import json\n"
        "import jarvis_server.core_main\n"
        "from jarvis_server.core import dispatch\n"
        "print(json.dumps(sorted(k for k in dispatch._HANDLERS if k.startswith('brain.'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True
    )
    assert json.loads(out.stdout) == ["brain.probe", "brain.reservation_sweep"]


class Crash(BaseException):
    """Stands in for the process dying mid-call: no `except Exception` catches it."""


class RaisingBrain:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.usage: tuple[int, int] | None = None
        self.calls = 0

    def think(self, req: BrainRequest) -> Iterator[BrainEvent]:
        self.calls += 1
        raise self.error
        yield Speech("never")  # a generator, like a real brain


def test_crash_during_probe_call_leaves_a_committed_reservation(app: Conn) -> None:  # R12-13
    _down(app)
    brain = RaisingBrain(Crash())
    health.set_probe_brain(lambda: (brain, MODEL))
    with pytest.raises(Crash):
        job_id = jobs.enqueue(app, "brain.probe")
        app.execute(
            "UPDATE ops.job SET status = 'RUNNING', attempts = 1, locked_by = 'test',"
            " locked_until = now() + interval '5 minutes' WHERE id = %s",
            (job_id,),
        )
        dispatch.handle(app, jobs.Job(id=job_id, kind="brain.probe", payload={}, attempts=1,
                                      max_attempts=2))  # fmt: skip
    assert brain.calls == 1
    # The job's transaction rolled back, but the reservation was committed before the call.
    [(rid, state, reserved, lease)] = app.execute(
        "SELECT id, state, reserved_inr, lease_until FROM ops.brain_reservation WHERE job_id = %s",
        (job_id,),
    ).fetchall()
    assert state == "ACTIVE" and reserved > 0
    month = app.execute("SELECT month FROM ops.brain_reservation WHERE id = %s", (rid,)).fetchone()
    assert month is not None
    settled_before = app.execute(
        "SELECT settled_inr FROM ops.brain_budget WHERE month = %s", (month[0],)
    ).fetchone()
    assert settled_before is not None
    # The sweep later lapses it at its full value.
    assert budget.lapse_expired(app, lease + timedelta(seconds=1)) == 1
    assert _probe_reservations(app, job_id) == [("LAPSED", None)]
    settled_after = app.execute(
        "SELECT settled_inr FROM ops.brain_budget WHERE month = %s", (month[0],)
    ).fetchone()
    assert settled_after == (settled_before[0] + reserved,)


def test_probe_unexpected_exception_is_send_phase(app: Conn) -> None:  # §10.1 step 4
    _down(app)
    brain = RaisingBrain(RuntimeError("unplaced"))
    health.set_probe_brain(lambda: (brain, MODEL))
    job_id = _run_probe(app)  # the job completes: the failure is handled, not raised
    assert brain.calls == 1
    assert _probe_reservations(app, job_id) == [("LAPSED", None)]
    assert health.is_down(app) is True
    status = app.execute("SELECT status FROM ops.job WHERE id = %s", (job_id,)).fetchone()
    assert status == ("DONE",)


def _new_person(owner: Conn) -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Ravi Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


def _acknowledge(app: Conn, gateway: Conn, person: uuid.UUID, item: uuid.UUID) -> None:
    """Deliver and acknowledge `item` through a node of `person`, as the gateway does."""
    code = nodes.create_enrolment(
        app, person_id=person, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP
    )
    token, _, _ = nodes.parse_enrolment_code(code)
    node_id = nodes.enrol(
        gateway,
        token=token,
        public_key=secrets.token_bytes(32),
        tailscale_identity=f"ts:pc-{secrets.token_hex(4)}",
    )
    row = gateway.execute("SELECT conn_id FROM gw.open_connection(%s)", (node_id,)).fetchone()
    assert row is not None
    assert gateway.execute("SELECT comm.record_sent(%s, %s)", (row[0], item)).fetchone() == (True,)
    acked = gateway.execute("SELECT comm.acknowledge(%s, %s)", (row[0], item)).fetchone()
    assert acked == ("ACKNOWLEDGED",)


def _down_item(app: Conn, person: uuid.UUID) -> uuid.UUID:
    event = comm.record_event(app, "brain.down", subject_type="brain", subject_id="brain",
                              person_id=person, details={})  # fmt: skip
    [item] = rules.notify(app, "brain.down", person_id=person, event_id=event, context={})
    return item


def _back_items(app: Conn, person: uuid.UUID) -> list[tuple[str, uuid.UUID | None]]:
    rows = app.execute(
        "SELECT priority, supersedes_id FROM comm.outbox WHERE person_id = %s"
        " AND kind = 'brain.back'",
        (person,),
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def test_back_reaches_everyone_told_it_was_down(
    app: Conn, owner: Conn, gateway: Conn
) -> None:  # R12-17
    seen, unseen, never = _new_person(owner), _new_person(owner), _new_person(owner)
    _down(app)
    seen_item, unseen_item = _down_item(app, seen), _down_item(app, unseen)
    _acknowledge(app, gateway, seen, seen_item)
    brain = FakeBrain([Speech("ok")], usage=(30, 2))
    health.set_probe_brain(lambda: (brain, MODEL))
    _run_probe(app)
    assert health.is_down(app) is False
    # Both people told it was down hear it is back; the acknowledged item is left as it is,
    # the open one is superseded. The person never told it was down hears nothing.
    assert _back_items(app, seen) == [("DIGEST", None)]
    assert _back_items(app, unseen) == [("DIGEST", unseen_item)]
    assert _back_items(app, never) == []
    states = dict(
        app.execute(
            "SELECT id, state FROM comm.outbox WHERE id = ANY(%s)", ([seen_item, unseen_item],)
        ).fetchall()
    )
    assert states == {seen_item: "ACKNOWLEDGED", unseen_item: "SUPERSEDED"}
    # The next outage tells only whoever is sent its "down" item.
    _down(app)
    _down_item(app, unseen)
    _run_probe(app)
    assert len(_back_items(app, unseen)) == 2
    assert len(_back_items(app, seen)) == 1  # its earlier outage was already answered
