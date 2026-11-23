"""Brain health (spec §10.2): DOWN after 2 consecutive failures, back after a success, and the
`brain.probe` job.

Runs in its own database: the state is one global `ops.heartbeat` row, and event and outbox
rows block the 0009 downgrade. People are fictional.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from jarvis_server import comm, jobs
from jarvis_server.brain import BrainCallFailed, CallPhase, FakeBrain, Speech, health
from jarvis_server.core import dispatch, rules

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
MODEL = "claude-opus-5-5"


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
