"""The review loop: what to do next with a ticket's open pull request.

JARVIS watches his PR on GitHub. CI failures and Manj's own review comments go back to the Builder
as feedback for a new round; Manj hears about the PR only when it is green with nothing open.
There is no merge step anywhere in this package: merging is Manj's.

Comments from anyone but the owner are ignored: they are data, never instructions to the team.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    WAIT = auto()
    TELL_READY = auto()
    REBUILD = auto()
    WATCH = auto()
    STOP = auto()
    FLAG_MISMATCH = auto()


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    body: str
    path: str | None
    line: int | None


@dataclass(frozen=True)
class Pr:
    state: str  # "open" or "closed"
    merged: bool
    head_sha: str
    merge_head_sha: str | None  # the head commit at the moment it was merged
    checks: str  # "success", "pending" or "failure"
    comments: tuple[Comment, ...]


@dataclass(frozen=True)
class Step:
    action: Action
    feedback: tuple[str, ...] = ()
    comment_ids: tuple[int, ...] = ()


CI_FAILED = "CI failed on the pushed commit; read its log and fix the cause."


def next_step(
    pr: Pr, *, owner: str, reviewed_sha: str, handled: frozenset[int], told: bool
) -> Step:
    if pr.merged:
        ok = pr.merge_head_sha == reviewed_sha
        return Step(Action.WATCH if ok else Action.FLAG_MISMATCH)
    if pr.state != "open":
        return Step(Action.STOP)
    if pr.head_sha != reviewed_sha:
        return Step(Action.FLAG_MISMATCH)
    new = tuple(c for c in pr.comments if c.author == owner and c.id not in handled)
    if new:
        return Step(Action.REBUILD, tuple(_feedback(c) for c in new), tuple(c.id for c in new))
    if pr.checks == "failure":
        return Step(Action.REBUILD, (CI_FAILED,))
    if pr.checks == "success" and not told:
        return Step(Action.TELL_READY)
    return Step(Action.WAIT)


def _feedback(c: Comment) -> str:
    where = (
        f" on {c.path} line {c.line}" if c.path and c.line else f" on {c.path}" if c.path else ""
    )
    return f"Review comment{where}: {c.body}"
