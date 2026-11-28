"""Ticket states and the moves between them, as in the design's states table.

This module is pure. The database keeps the same table (improve.allowed_move, migration 0017)
and a trigger refuses any other move, so a bug here can't skip one of Manj's two gates.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum


class State(StrEnum):
    OPEN = "OPEN"
    DESIGNING = "DESIGNING"
    DESIGN_READY = "DESIGN_READY"
    APPROVED = "APPROVED"
    PR_OPEN = "PR_OPEN"
    WATCHING = "WATCHING"
    DONE = "DONE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


TERMINAL = frozenset({State.DONE, State.STOPPED})
LIVE = frozenset(s for s in State if s not in TERMINAL and s is not State.PAUSED)

_FLOW: tuple[tuple[State, State], ...] = (
    (State.OPEN, State.DESIGNING),  # JARVIS opened it on Manj's yes
    (State.DESIGNING, State.DESIGN_READY),  # Analyst and Researcher finished
    (State.DESIGN_READY, State.APPROVED),  # Manj approved this exact version
    (State.DESIGN_READY, State.DESIGNING),  # he asked for changes
    (State.APPROVED, State.PR_OPEN),  # tests, build and review passed
    (State.PR_OPEN, State.WATCHING),  # he approved and merged
    (State.PR_OPEN, State.APPROVED),  # he asked for changes: a new build round
    (State.WATCHING, State.DONE),  # seven days with no repeat
    (State.WATCHING, State.DESIGNING),  # the complaint came back
)

ROUNDS = 3
DESIGN_WAIT = timedelta(days=14)
WATCH = timedelta(days=7)


def transitions() -> tuple[tuple[State, State], ...]:
    """Every allowed move except leaving PAUSED, which depends on where the ticket was."""
    anywhere = tuple((s, to) for s in State if s in LIVE for to in (State.PAUSED, State.STOPPED))
    return tuple(dict.fromkeys(_FLOW + anywhere))


_ALLOWED = frozenset(transitions())


def can_move(current: State, to: State, *, paused_from: State | None = None) -> bool:
    if current is State.PAUSED:
        return to is State.STOPPED or (paused_from is not None and to is paused_from)
    return (current, to) in _ALLOWED


def rounds_left(used: int) -> int:
    return max(ROUNDS - used, 0)


def design_expired(ready_at: datetime, *, now: datetime) -> bool:
    return now - ready_at > DESIGN_WAIT


def watch_over(started_at: datetime, *, now: datetime) -> bool:
    return now - started_at >= WATCH


def slug(words: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", words.casefold()).strip("-")
    return text[:40].rstrip("-") or "ticket"


def branch(ticket_id: int, ticket_slug: str) -> str:
    return f"improve/{ticket_id}-{ticket_slug}"
