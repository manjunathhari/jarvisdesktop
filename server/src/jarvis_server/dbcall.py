"""Calling audited database functions (spec §4.1, R7-1).

An audited function never raises for a refused request. It writes the REJECTED audit row and
returns a structured refusal (a reason code), because in PostgreSQL an error would roll the
audit row back with everything else. The caller commits that transaction first and raises only
afterwards: `audited_call` runs the call in its own transaction, which commits on exit, and
refuses to run inside a transaction the caller might still roll back.
"""

from __future__ import annotations

from collections.abc import Sequence
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
