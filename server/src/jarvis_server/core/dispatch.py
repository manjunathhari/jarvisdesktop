"""Core job dispatch (spec §5.4, ADR-013): one way in, `ops.job`, for node requests and
internal server events alike (F2).

Transactions (controller ruling 5):
- Each job runs in its own transaction: the handler's work and `jobs.complete` commit together,
  so a job re-run after a crash finds either everything or nothing.
- A handler that raises: its transaction is rolled back (nothing it wrote survives), then
  `jobs.fail` records the failure in a separate transaction, and the error is re-raised.
- Inside the job transaction, `dbcall.audited_call_in_job` is the way to call an audited
  function (R11-1): dispatch marks the transaction with a context variable for the handler's
  run, and the helper refuses to run anywhere else (`NotInJob`).
- A handler that has to keep an audited refusal writes it and **returns normally**; the refusal
  then commits with the job's completion and is never rolled back (R7-1, R10-1). A handler
  never raises after writing a refusal it must keep.

`handle` needs an autocommit connection with no transaction open, so that the job transaction
is really its own and a failure cannot reach work the caller still holds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from jarvis_server import jobs
from jarvis_server.core import rules
from jarvis_server.dbcall import TransactionNotIdle, job_transaction

log = logging.getLogger("jarvis.core.dispatch")

Handler = Callable[[psycopg.Connection[Any], jobs.Job], None]

__all__ = ["Handler", "TransactionNotIdle", "UnknownJob", "handle", "register"]

_HANDLERS: dict[str, Handler] = {}


class UnknownJob(LookupError):
    """A job kind with no registered handler."""


def register(kind: str) -> Callable[[Handler], Handler]:
    """Decorator: make the function the handler of job kind `kind` (one handler per kind)."""

    def decorate(handler: Handler) -> Handler:
        if kind in _HANDLERS:
            raise ValueError(f"job kind {kind!r} already has a handler")
        _HANDLERS[kind] = handler
        return handler

    return decorate


def handle(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Run one claimed job in its own transaction; complete it, or fail it and re-raise."""
    status = conn.info.transaction_status
    if not conn.autocommit or status != TransactionStatus.IDLE:
        raise TransactionNotIdle(
            f"dispatch needs an idle autocommit connection (transaction status {status.name})"
        )
    try:
        handler = _HANDLERS.get(job.kind)
        if handler is None:
            raise UnknownJob(f"no handler for job kind {job.kind!r}")
        with conn.transaction(), job_transaction(conn):
            handler(conn, job)
            jobs.complete(conn, job.id)
    except Exception as e:
        # The job transaction (if any) has been rolled back on leaving the block above.
        with conn.transaction():
            outcome = jobs.fail(conn, job.id, f"{type(e).__name__}: {e}")
        log.warning("job %s (%s) failed: %s -> %s", job.id, job.kind, e, outcome)
        raise


@register("noop")
def _noop(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    log.info("noop job %s ran", job.id)


@register("system.event")
def _system_event(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Payload `{event_id, event_kind, person_id, context}` -> `rules.notify` (§6.3, §8)."""
    payload = job.payload
    event_id = UUID(payload["event_id"])
    person_id = UUID(payload["person_id"])
    event_kind = payload["event_kind"]
    context = payload["context"]
    if not isinstance(event_kind, str) or not isinstance(context, dict):
        raise ValueError("system.event payload: event_kind is a string, context an object")
    row = conn.execute("SELECT person_id FROM comm.event WHERE id = %s", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"system.event: no event {event_id}")
    if row[0] != person_id:
        raise ValueError("system.event: the event belongs to another person")
    rules.notify(
        conn, event_kind, person_id=person_id, event_id=event_id, context=_context(context)
    )


def _context(raw: dict[str, Any]) -> rules.NotifyContext:
    """A job payload is JSON: turn its `supersedes_id` and `target_node_id` strings back into
    UUIDs and `expires_at` into a datetime. Every other value goes on as it is; `rules.notify`
    checks the whole context (unknown keys, types, a naive `expires_at`, the authz shape)."""
    context = dict(raw)
    for key in ("supersedes_id", "target_node_id"):
        if isinstance(context.get(key), str):
            context[key] = UUID(context[key])
    if isinstance(context.get("expires_at"), str):
        context["expires_at"] = datetime.fromisoformat(context["expires_at"])
    return cast(rules.NotifyContext, context)
