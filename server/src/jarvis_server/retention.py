"""Retention (spec §5.7, R12-30, R12-31): the `retention.sweep` job.

The work is all in `ops.retention_sweep(p_now)` (migration 0012): it deletes past-retention
rows and writes one SYSTEM `RETENTION` audit row per table with the real count, in the job's
transaction, so the deletes and their audit rows commit together or not at all (R11-1, R11-2).
The scheduler queues the job daily at 03:30 IST; the handler sweeps at the database's `now()`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import psycopg

from jarvis_server import jobs
from jarvis_server.core import dispatch
from jarvis_server.dbcall import audited_call_in_job

log = logging.getLogger("jarvis.retention")

JOB = "retention.sweep"


def sweep(conn: psycopg.Connection[Any], now: datetime) -> dict[str, int]:
    """Delete what §5.7 no longer keeps at `now` (no later than the database's `now()`); return
    the rows deleted per table. Only inside dispatch's job transaction (`NotInJob` otherwise)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry a time zone")
    row = audited_call_in_job(
        conn,
        "SELECT jsonb_object_agg(table_name, deleted) FROM ops.retention_sweep(%s)",
        (now,),
    )
    return {str(table): int(n) for table, n in row[0].items()}


@dispatch.register(JOB)
def _sweep_job(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    deleted = sweep(conn, row[0])
    log.info(
        "retention sweep (job %s): %s",
        job.id,
        ", ".join(f"{table} {n}" for table, n in deleted.items()),
    )
