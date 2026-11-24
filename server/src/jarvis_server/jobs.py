"""Postgres job queue (D5) and process heartbeats (spec §8).

Claiming uses FOR UPDATE SKIP LOCKED, so two workers never get the same job. A job whose lock
expired (worker died) can be claimed again until max_attempts is used up; v2's "retry once"
is the default max_attempts = 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

HEARTBEAT_INTERVAL = timedelta(seconds=30)
MISSED_BEATS = 3


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    conn: psycopg.Connection[Any],
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    delay: timedelta = timedelta(0),
    max_attempts: int = 2,
) -> int:
    row = conn.execute(
        "INSERT INTO ops.job (kind, payload, run_at, max_attempts)"
        " VALUES (%s, %s, now() + %s, %s) RETURNING id",
        (kind, Jsonb(payload or {}), delay, max_attempts),
    ).fetchone()
    assert row is not None
    return int(row[0])


JOB_KEY = "job_key"


def job_key(kind: str, item: str) -> str:
    """The deterministic key of the one job for `item` (an ID, or a period such as a minute)."""
    return f"{kind}:{item}"


def requeue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """A job's payload for a later run of the same work (a handler re-queueing itself), without
    its `job_key`: the key stays with the first job, so the unique index would refuse a copy,
    and an item's key keeps the scheduler from queueing it again (R12-32)."""
    return {k: v for k, v in payload.items() if k != JOB_KEY}


def enqueue_once(
    conn: psycopg.Connection[Any],
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    *,
    delay: timedelta = timedelta(0),
) -> int | None:
    """Enqueue `kind` with `key` in its payload (`job_key`), unless a job of that kind with the
    same key exists already, in any status: at most one job per due item (plan Task 13). Returns
    the new job's ID, or None.

    The unique index `ops.job_one_per_key` (migration 0013, R12-32) is the guarantee: a conflict
    means the job is already queued. A second caller racing an uncommitted insert of the same
    key waits for it, then gets None if it commits, or inserts if it rolls back. One statement,
    so it works alike in autocommit and inside a job transaction, and needs no lock of its own.
    Finished jobs are deleted after 30 days (§5.7), and only then can a key be queued again."""
    row = conn.execute(
        "INSERT INTO ops.job (kind, payload, run_at) VALUES (%s, %s, now() + %s)"
        " ON CONFLICT (kind, (payload->>'job_key')) WHERE payload ? 'job_key' DO NOTHING"
        " RETURNING id",
        (kind, Jsonb({**(payload or {}), JOB_KEY: key}), delay),
    ).fetchone()
    return None if row is None else int(row[0])


def claim(
    conn: psycopg.Connection[Any], worker: str, lease: timedelta = timedelta(minutes=5)
) -> Job | None:
    """Take the next ready job (or one whose lease expired), or None if there is none."""
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE ops.job SET status = 'RUNNING', attempts = attempts + 1,
                   locked_by = %s, locked_until = now() + %s
            WHERE id = (
                SELECT id FROM ops.job
                WHERE attempts < max_attempts AND (
                      (status = 'QUEUED' AND run_at <= now())
                   OR (status = 'RUNNING' AND locked_until < now()))
                ORDER BY run_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1)
            RETURNING id, kind, payload, attempts, max_attempts
            """,
            (worker, lease),
        ).fetchone()
    if row is None:
        return None
    return Job(id=row[0], kind=row[1], payload=row[2], attempts=row[3], max_attempts=row[4])


def complete(conn: psycopg.Connection[Any], job_id: int) -> None:
    conn.execute(
        "UPDATE ops.job SET status = 'DONE', finished_at = now(), locked_by = NULL,"
        " locked_until = NULL WHERE id = %s",
        (job_id,),
    )


def fail(
    conn: psycopg.Connection[Any], job_id: int, error: str, retry_in: timedelta = timedelta(0)
) -> str:
    """Record a failure. Re-queues if attempts remain, else marks FAILED. Returns the new status."""
    row = conn.execute(
        """
        UPDATE ops.job SET
            status = CASE WHEN attempts < max_attempts THEN 'QUEUED' ELSE 'FAILED' END,
            run_at = CASE WHEN attempts < max_attempts THEN now() + %s ELSE run_at END,
            finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
            last_error = %s, locked_by = NULL, locked_until = NULL
        WHERE id = %s RETURNING status
        """,
        (retry_in, error[:2000], job_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def sweep_abandoned(conn: psycopg.Connection[Any]) -> int:
    """Mark jobs FAILED whose lease expired with no attempts left. Returns how many."""
    cur = conn.execute(
        "UPDATE ops.job SET status = 'FAILED', finished_at = now(),"
        " last_error = coalesce(last_error, 'lease expired; no attempts left')"
        " WHERE status = 'RUNNING' AND locked_until < now() AND attempts >= max_attempts"
    )
    return cur.rowcount


def beat(
    conn: psycopg.Connection[Any], process: str, details: dict[str, Any] | None = None
) -> None:
    conn.execute(
        "INSERT INTO ops.heartbeat (process, last_seen, details) VALUES (%s, now(), %s)"
        " ON CONFLICT (process) DO UPDATE SET last_seen = now(), details = EXCLUDED.details",
        (process, Jsonb(details or {})),
    )


def silent_processes(
    conn: psycopg.Connection[Any],
    interval: timedelta = HEARTBEAT_INTERVAL,
    missed: int = MISSED_BEATS,
) -> list[str]:
    """Processes that have missed `missed` heartbeats (v2 §13.3: 3 missed = offline)."""
    rows = conn.execute(
        "SELECT process FROM ops.heartbeat WHERE last_seen < now() - %s ORDER BY process",
        (interval * missed,),
    ).fetchall()
    return [str(r[0]) for r in rows]
