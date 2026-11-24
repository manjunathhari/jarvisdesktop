"""Calling audited database functions (spec §4.1, R7-1).

An audited function never raises for a refused request. It writes the REJECTED audit row and
returns a structured refusal (a reason code), because in PostgreSQL an error would roll the
audit row back with everything else. The caller commits that transaction first and raises only
afterwards: `audited_call` runs the call in its own transaction, which commits on exit, and
refuses to run inside a transaction the caller might still roll back.

Inside a job (R11-1) the job transaction is the audited call's transaction: a handler calls
audited functions through `audited_call_in_job`, which runs only inside dispatch's job
transaction (marked with `job_transaction`) and never opens, commits or rolls back anything. A
handler that gets a refusal it must keep returns normally, so dispatch commits the refusal's
audit row with the job.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, LiteralString

import psycopg
from psycopg.pq import TransactionStatus

# `RAISE EXCEPTION 'connection_fenced' USING ERRCODE = 'JF001'` (R7-5): a closed or superseded
# connection, or a revoked node. Distinct from ordinary permission denial (42501).
FENCED_SQLSTATE = "JF001"


class Refused(Exception):
    """A request the database refused and audited; `reason` is its reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EnrolmentRefused(Refused):
    """gw.enrol_node refused: bad_token, key_in_use or identity_in_use."""


class RevokeRefused(Refused):
    """gw.revoke_node refused: unknown_node or already_revoked."""


class ConnectRefused(Refused):
    """gw.open_connection refused: unknown_node or revoked."""


class OutboxRefused(Refused):
    """comm.record_received or comm.acknowledge refused: unknown_item (unknown, or another
    person's item; R9-5)."""


class ConsumeRefused(Refused):
    """ledger.consume_approval refused: unknown_approval, not_approved, expired or
    already_consumed."""


class NotInJob(Exception):
    """`audited_call_in_job` outside dispatch's job transaction; nothing was run."""


# The connection whose job transaction dispatch is running a handler in, if any.
_JOB_CONN: ContextVar[psycopg.Connection[Any] | None] = ContextVar("jarvis_job_conn", default=None)


class TransactionNotIdle(Exception):
    """An audited call was attempted with a transaction open; its audit row could be lost."""


def _check_idle(status: TransactionStatus) -> None:
    if status != TransactionStatus.IDLE:
        raise TransactionNotIdle(
            f"audited calls need an idle connection (transaction status {status.name})"
        )


def _row(row: Sequence[Any] | None) -> tuple[Any, ...]:
    if row is None:
        raise RuntimeError("audited call returned no row")
    return tuple(row)


def audited_call(
    conn: psycopg.Connection[Any], sql: LiteralString, params: Sequence[Any]
) -> tuple[Any, ...]:
    """Run one audited function call in its own committed transaction; return its row."""
    _check_idle(conn.info.transaction_status)
    with conn.transaction():
        row = conn.execute(sql, params).fetchone()
    return _row(row)


async def audited_call_async(
    conn: psycopg.AsyncConnection[Any], sql: LiteralString, params: Sequence[Any]
) -> tuple[Any, ...]:
    """`audited_call` for an async connection (the gateway)."""
    _check_idle(conn.info.transaction_status)
    async with conn.transaction():
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
    return _row(row)


@contextmanager
def job_transaction(conn: psycopg.Connection[Any]) -> Iterator[None]:
    """Dispatch only: mark `conn`'s open transaction as a job transaction for the block."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise NotInJob("job_transaction needs the job's open transaction")
    token = _JOB_CONN.set(conn)
    try:
        yield
    finally:
        _JOB_CONN.reset(token)


def audited_call_in_job(
    conn: psycopg.Connection[Any], sql: LiteralString, params: Sequence[Any]
) -> tuple[Any, ...]:
    """Run one audited function call in dispatch's open job transaction; return its row.

    Raises `NotInJob`, and runs nothing, unless `conn` is the connection of the job transaction
    dispatch is running a handler in, with that transaction still open.
    """
    if _JOB_CONN.get() is not conn or conn.info.transaction_status != TransactionStatus.INTRANS:
        raise NotInJob("audited_call_in_job runs only inside dispatch's job transaction")
    return _row(conn.execute(sql, params).fetchone())
