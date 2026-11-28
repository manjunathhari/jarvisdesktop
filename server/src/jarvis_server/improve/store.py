"""The team's Postgres store (migration 0017): tickets, moves, stage costs, the queue, the inbox.

The trigger improve.guard_move is the authority on moves; this module only reports its refusal as
IllegalMove.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from jarvis_server.improve import inbox
from jarvis_server.improve.ports import Job, Ticket
from jarvis_server.improve.tickets import State

_PAID_ATTEMPTS = 1
_WATCH_ATTEMPTS = 3

# Columns update() may set; `state` changes only through move().
_FIELDS = frozenset(
    {
        "paused_stage",
        "rounds_used",
        "cap_usd",
        "design",
        "design_fingerprint",
        "approved_fingerprint",
        "design_ready_at",
        "base_sha",
        "test_fingerprints",
        "tests_patch",
        "build_patch",
        "feedback",
        "pr_number",
        "pr_url",
        "reviewed_sha",
        "handled_comments",
        "told_ready",
        "watch_started_at",
    }
)


class IllegalMove(Exception):
    """The database refused the move (improve.guard_move)."""


class PgStore:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    def open_ticket(self, words: str, slug: str, evidence: Mapping[str, Any]) -> int:
        row = self.conn.execute(
            "INSERT INTO improve.ticket (words, slug, evidence) VALUES (%s, %s, %s) RETURNING id",
            (words, slug, Jsonb(dict(evidence))),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def get(self, ticket_id: int) -> Ticket:
        with self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT * FROM improve.ticket WHERE id = %s", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        return _ticket(row)

    def in_state(self, *states: State) -> list[Ticket]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM improve.ticket WHERE state = ANY(%s) ORDER BY id",
                ([s.value for s in states],),
            ).fetchall()
        return [_ticket(r) for r in rows]

    def move(self, ticket_id: int, to: State, actor: str, reason: str) -> None:
        try:
            with self.conn.transaction():
                row = self.conn.execute(
                    "SELECT state FROM improve.ticket WHERE id = %s FOR UPDATE", (ticket_id,)
                ).fetchone()
                if row is None:
                    raise IllegalMove(f"ticket {ticket_id} does not exist")
                self.conn.execute(
                    "UPDATE improve.ticket SET state = %s WHERE id = %s", (to.value, ticket_id)
                )
                self.conn.execute(
                    "INSERT INTO improve.ticket_event (ticket_id, from_state, to_state, actor,"
                    " reason) VALUES (%s, %s, %s, %s, %s)",
                    (ticket_id, row[0], to.value, actor, reason),
                )
        except psycopg.errors.RaiseException as e:
            raise IllegalMove(str(e).splitlines()[0]) from None

    def update(self, ticket_id: int, **fields: Any) -> None:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise ValueError(f"update() can't set {', '.join(sorted(unknown))}")
        values = {k: _column(k, v) for k, v in fields.items()}
        query = sql.SQL("UPDATE improve.ticket SET {} WHERE id = %(id)s").format(
            sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in values
            )
        )
        self.conn.execute(query, {**values, "id": ticket_id})

    def use_round(self, ticket_id: int) -> int:
        row = self.conn.execute(
            "UPDATE improve.ticket SET rounds_used = rounds_used + 1 WHERE id = %s"
            " RETURNING rounds_used",
            (ticket_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def record_stage(
        self,
        ticket_id: int,
        stage: str,
        *,
        cost_usd: float,
        turns: int,
        outcome: str,
        error: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO improve.stage_run (ticket_id, stage, outcome, cost_usd, turns, error)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (ticket_id, stage, outcome, cost_usd, turns, error[:4000] if error else None),
        )

    def spent(self, ticket_id: int) -> float:
        row = self.conn.execute(
            "SELECT coalesce(sum(cost_usd), 0) FROM improve.stage_run WHERE ticket_id = %s",
            (ticket_id,),
        ).fetchone()
        assert row is not None
        return float(row[0])

    def add_inbox(self, item: inbox.Item) -> None:
        self.conn.execute(
            "INSERT INTO improve.inbox (ticket_id, kind, title, steps, key_name)"
            " VALUES (%s, %s, %s, %s, %s)",
            (item.ticket_id, item.kind.value, item.title, list(item.steps), item.key_name),
        )

    def open_inbox(self) -> list[inbox.Item]:
        rows = self.conn.execute(
            "SELECT kind, title, steps, ticket_id, key_name FROM improve.inbox"
            " WHERE resolved_at IS NULL ORDER BY id"
        ).fetchall()
        return [inbox.Item(inbox.Kind(k), t, tuple(s), tid, key) for k, t, s, tid, key in rows]

    def resolve_inbox(self, ticket_id: int, kind: inbox.Kind) -> None:
        self.conn.execute(
            "UPDATE improve.inbox SET resolved_at = now()"
            " WHERE ticket_id = %s AND kind = %s AND resolved_at IS NULL",
            (ticket_id, kind.value),
        )

    def resolve_key(self, key_name: str) -> None:
        self.conn.execute(
            "UPDATE improve.inbox SET resolved_at = now()"
            " WHERE key_name = %s AND resolved_at IS NULL",
            (key_name,),
        )

    def enqueue(self, ticket_id: int, stage: str, *, delay_s: int = 0) -> int:
        attempts = _WATCH_ATTEMPTS if stage == "pr_watch" else _PAID_ATTEMPTS
        row = self.conn.execute(
            "INSERT INTO improve.job (ticket_id, stage, run_at, max_attempts)"
            " VALUES (%s, %s, now() + %s, %s) RETURNING id",
            (ticket_id, stage, timedelta(seconds=delay_s), attempts),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def claim(self, worker: str, lease_s: int) -> Job | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE improve.job SET status = 'RUNNING', attempts = attempts + 1,
                       locked_by = %s, locked_until = now() + %s
                WHERE id = (
                    SELECT id FROM improve.job
                    WHERE attempts < max_attempts AND (
                          (status = 'QUEUED' AND run_at <= now())
                       OR (status = 'RUNNING' AND locked_until < now()))
                    ORDER BY run_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
                RETURNING id, ticket_id, stage, attempts
                """,
                (worker, timedelta(seconds=lease_s)),
            ).fetchone()
        if row is None:
            return None
        return Job(id=row[0], ticket_id=row[1], stage=row[2], attempts=row[3])

    def heartbeat(self, job_id: int, lease_s: int) -> None:
        self.conn.execute(
            "UPDATE improve.job SET locked_until = now() + %s WHERE id = %s AND status = 'RUNNING'",
            (timedelta(seconds=lease_s), job_id),
        )

    def finish(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE improve.job SET status = 'DONE', finished_at = now(), locked_by = NULL,"
            " locked_until = NULL WHERE id = %s",
            (job_id,),
        )

    def fail(self, job_id: int, error: str) -> None:
        self.conn.execute(
            """
            UPDATE improve.job SET
                status = CASE WHEN attempts < max_attempts THEN 'QUEUED' ELSE 'FAILED' END,
                finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                last_error = %s, locked_by = NULL, locked_until = NULL
            WHERE id = %s
            """,
            (error[:2000], job_id),
        )


def _column(name: str, value: Any) -> Any:
    if name == "test_fingerprints":
        return Jsonb(dict(value))
    if name in ("feedback", "handled_comments"):
        return sorted(value) if name == "handled_comments" else list(value)
    return value


def _ticket(r: Mapping[str, Any]) -> Ticket:
    return Ticket(
        id=r["id"],
        words=r["words"],
        slug=r["slug"],
        evidence=r["evidence"],
        state=State(r["state"]),
        paused_from=State(r["paused_from"]) if r["paused_from"] else None,
        rounds_used=r["rounds_used"],
        design=r["design"],
        design_fingerprint=r["design_fingerprint"],
        approved_fingerprint=r["approved_fingerprint"],
        design_ready_at=r["design_ready_at"],
        base_sha=r["base_sha"],
        cap_usd=float(r["cap_usd"]),
        paused_stage=r["paused_stage"],
        test_fingerprints=r["test_fingerprints"],
        tests_patch=bytes(r["tests_patch"]) if r["tests_patch"] is not None else None,
        build_patch=bytes(r["build_patch"]) if r["build_patch"] is not None else None,
        feedback=tuple(r["feedback"]),
        pr_number=r["pr_number"],
        pr_url=r["pr_url"],
        reviewed_sha=r["reviewed_sha"],
        handled_comments=frozenset(r["handled_comments"]),
        told_ready=r["told_ready"],
        watch_started_at=r["watch_started_at"],
    )
