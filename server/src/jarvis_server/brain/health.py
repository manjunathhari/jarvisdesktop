"""Brain health (spec §10.2): DOWN after 2 consecutive failures, back after one success.

The state is one `ops.heartbeat` row, process `brain`, with details
`{"down": bool, "consecutive_failures": int}`. Each change locks the row (it is created when
missing), so concurrent jobs count every failure and report each transition once.

`brain.probe` (scheduled every 2 minutes while the brain is DOWN) does nothing while the brain
is up. While it is DOWN it sends a minimal request, reserved like any other call (turn_id is
null) on its own committed connection (R12-13): success settles it, marks the brain back and
sends CLAUDE_BACK (`brain.back`, which supersedes each person's open "Claude down" item); a
CONNECT failure releases the reservation, a SEND failure (or any exception the brain raises
that is not a placed `BrainCallFailed`) lapses it at full value, and either counts as a
failure. Re-queueing the turns
held while the brain was down is the conversation turn's job (Task 10), not the probe's.

The probe gets its brain from `set_probe_brain`, set by the process that wires the brain up;
without one a probe job fails (`NoProbeBrain`) rather than pretending the brain is fine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from jarvis_server import comm, jobs
from jarvis_server.brain import budget, prices
from jarvis_server.brain.claude import count_input_tokens
from jarvis_server.brain.types import Brain, BrainCallFailed, BrainRequest, CallPhase
from jarvis_server.core import dispatch, rules

log = logging.getLogger("jarvis.brain.health")

HEARTBEAT_NAME = "brain"
DOWN_AFTER = 2  # consecutive failures (§10.2)

PROBE_MAX_TOKENS = 8
PROBE_REQUEST = BrainRequest(
    system="This is a health check. Reply with the single word OK.",
    history=[],
    utterance="OK?",
    screen_summary="",
    memory=[],
    tools=[],
    max_tokens=PROBE_MAX_TOKENS,
)

_DOWN_KINDS = ("brain.down", "brain.down.with_held")
_OPEN = (
    comm.OutboxState.PENDING.value,
    comm.OutboxState.DELIVERING.value,
    comm.OutboxState.DELIVERED.value,
)


class MeteredBrain(Brain, Protocol):
    """A brain that reports `(input_tokens, output_tokens)` after a completed call."""

    usage: tuple[int, int] | None


ProbeBrain = Callable[[], tuple[MeteredBrain, str]]  # -> (brain, model name)

_probe_brain: ProbeBrain | None = None


class NoProbeBrain(RuntimeError):
    """A probe ran with no brain configured."""


def set_probe_brain(provider: ProbeBrain | None) -> None:
    global _probe_brain
    _probe_brain = provider


def _locked(conn: psycopg.Connection[Any]) -> tuple[bool, int]:
    conn.execute(
        "INSERT INTO ops.heartbeat (process, details) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (HEARTBEAT_NAME, Jsonb({"down": False, "consecutive_failures": 0})),
    )
    row = conn.execute(
        "SELECT details FROM ops.heartbeat WHERE process = %s FOR UPDATE", (HEARTBEAT_NAME,)
    ).fetchone()
    assert row is not None
    details = row[0]
    return bool(details.get("down", False)), int(details.get("consecutive_failures", 0))


def _write(conn: psycopg.Connection[Any], down: bool, failures: int) -> None:
    conn.execute(
        "UPDATE ops.heartbeat SET last_seen = now(), details = %s WHERE process = %s",
        (Jsonb({"down": down, "consecutive_failures": failures}), HEARTBEAT_NAME),
    )


def record_failure(conn: psycopg.Connection[Any]) -> bool:
    """Count a failed call. True when this failure has just marked the brain DOWN."""
    with conn.transaction():
        down, failures = _locked(conn)
        failures += 1
        just_down = not down and failures >= DOWN_AFTER
        _write(conn, down or just_down, failures)
    return just_down


def record_success(conn: psycopg.Connection[Any]) -> bool:
    """Count a successful call. True when the brain was DOWN and has just come back."""
    with conn.transaction():
        down, _ = _locked(conn)
        _write(conn, False, 0)
    return down


def is_down(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "SELECT details FROM ops.heartbeat WHERE process = %s", (HEARTBEAT_NAME,)
    ).fetchone()
    return row is not None and bool(row[0].get("down", False))


def _announce_back(conn: psycopg.Connection[Any]) -> None:
    """CLAUDE_BACK for every person with an open "Claude down" item (§8.1)."""
    people = conn.execute(
        "SELECT DISTINCT person_id FROM comm.outbox WHERE kind = ANY(%s) AND state = ANY(%s)"
        " ORDER BY person_id",
        (list(_DOWN_KINDS), list(_OPEN)),
    ).fetchall()
    for (person,) in people:
        person_id = UUID(str(person))
        event_id = comm.record_event(
            conn, "brain.back", subject_type="brain", subject_id=HEARTBEAT_NAME,
            person_id=person_id, details={},
        )  # fmt: skip
        rules.notify(conn, "brain.back", person_id=person_id, event_id=event_id, context={})


def probe(conn: psycopg.Connection[Any], brain: MeteredBrain, model: str, *, job_id: int) -> bool:
    """One probe call for job `job_id` (claimed: its lease is the reservation's lease).
    True when the brain has just come back.

    The reservation and its settle / release / lapse each commit on their own connection
    (R12-13), so a crash during the call leaves a committed ACTIVE reservation for the sweep
    to lapse at full value, and no budget or turn lock is held during the call.
    """
    row = conn.execute(
        "SELECT now(), locked_until FROM ops.job WHERE id = %s", (job_id,)
    ).fetchone()
    if row is None or row[1] is None or row[1] <= row[0]:
        raise ValueError(f"probe job {job_id} holds no lease")
    now, lease_until = row
    ceiling = prices.cost_paise(model, count_input_tokens(PROBE_REQUEST), PROBE_MAX_TOKENS)
    with budget.committing_connection(conn) as own:
        owner, reservation = budget.reserve(
            own, job_id=job_id, turn_id=None, reserve_paise=ceiling, lease_until=lease_until,
            now=now,
        )  # fmt: skip
    if owner is not budget.Owner.RESERVED or reservation is None:
        log.info("probe job %s: no budget for a probe (%s)", job_id, owner)
        return False
    phase: CallPhase | None = None
    try:
        for _ in brain.think(PROBE_REQUEST):
            pass
    except BrainCallFailed as failure:
        phase = failure.phase
    except Exception as exc:  # unplaceable: the send phase (fail closed, §10.1 step 4)
        phase = CallPhase.SEND
        log.warning("probe job %s: unexpected %s from the brain", job_id, type(exc).__name__)
    usage = brain.usage
    if phase is None and usage is None:  # a completed stream with no usage: fail closed
        phase = CallPhase.SEND
    with budget.committing_connection(conn) as own:
        if phase is CallPhase.CONNECT:
            budget.release(own, reservation)
        elif phase is CallPhase.SEND:
            budget.lapse(own, reservation)
        else:
            assert usage is not None
            budget.settle(
                own, reservation, actual_paise=prices.cost_paise(model, *usage), usage=usage,
                model=model,
            )  # fmt: skip
    if phase is not None:
        record_failure(conn)
        log.info("probe job %s: brain still down (%s)", job_id, phase)
        return False
    back = record_success(conn)
    if back:
        _announce_back(conn)
    return back


@dispatch.register("brain.probe")
def _probe_job(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    if not is_down(conn):
        return
    if _probe_brain is None:
        raise NoProbeBrain("brain.probe ran with no brain configured")
    brain, model = _probe_brain()
    if probe(conn, brain, model, job_id=job.id):
        log.info("brain is back (probe job %s)", job.id)
