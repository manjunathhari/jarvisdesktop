"""Conversation sessions (spec §5.2, P2-E09): at most one open session per person.

Any node of the person joins the open session, which is how a conversation resumes on another
node. A message after `IDLE` (30 minutes) of inactivity closes the open session and opens a new
one. The partial unique index `session_one_open` (migration 0009) is the guarantee: two first
messages at once both `INSERT … ON CONFLICT DO NOTHING`, the loser's insert waits for the
winner's row and then inserts nothing, and its re-select finds the winner's session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

IDLE = timedelta(minutes=30)

_OPEN = (
    "SELECT id, last_activity_at FROM conv.session"
    " WHERE person_id = %s AND closed_at IS NULL FOR UPDATE"
)


def open_or_join(conn: psycopg.Connection[Any], person_id: UUID, now: datetime) -> UUID:
    """The person's open session, touched at `now`; a new one if there is none or it has been
    idle for `IDLE` or longer. The session row stays locked until the caller's transaction
    ends (inside one), so messages of one session get their `seq` in order."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry a time zone")
    with conn.transaction():
        row = conn.execute(_OPEN, (person_id,)).fetchone()
        if row is not None and now - row[1] >= IDLE:
            conn.execute(
                "UPDATE conv.session SET closed_at = greatest(%s, started_at) WHERE id = %s",
                (now, row[0]),
            )
            row = None
        if row is None:
            inserted = conn.execute(
                "INSERT INTO conv.session (person_id, started_at, last_activity_at)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (person_id, now, now),
            ).fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            row = conn.execute(_OPEN, (person_id,)).fetchone()  # a racing first message won
            if row is None:
                raise RuntimeError(f"no open session for {person_id} after a conflict")
        conn.execute(
            "UPDATE conv.session SET last_activity_at = greatest(last_activity_at, %s)"
            " WHERE id = %s",
            (now, row[0]),
        )
        return UUID(str(row[0]))
