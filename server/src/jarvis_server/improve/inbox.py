"""One inbox for Manj: everything the team needs from him, in one list, with exact steps.

An item may name a key (improve_claude_key) but never carry a value: building an item with
anything that looks like a secret raises, so a key can't reach the list, the Board or a log by
way of an error message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class Kind(StrEnum):
    KEY = "KEY"
    DECISION = "DECISION"
    BLOCKED_SITE = "BLOCKED_SITE"
    READY_TO_MERGE = "READY_TO_MERGE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    WARNING = "WARNING"


class LooksLikeASecret(ValueError):
    """An inbox item would have shown a secret's value."""


_SECRET = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|[A-Za-z0-9+_=-]{40,}"  # any long unbroken token (paths, with their slashes, pass)
)


def looks_like_a_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


@dataclass(frozen=True)
class Item:
    kind: Kind
    title: str
    steps: tuple[str, ...]
    ticket_id: int | None = None
    key_name: str | None = None

    def __post_init__(self) -> None:
        for text in (self.title, *self.steps):
            if looks_like_a_secret(text):
                raise LooksLikeASecret("an inbox item may name a key but never show one")


def key_needed(name: str, what: str, steps: Sequence[str]) -> Item:
    return Item(Kind.KEY, f"Add the {name} secret", (*steps, f"This is {what}."), key_name=name)


def design_ready(ticket_id: int, title: str, fingerprint: str) -> Item:
    short = fingerprint[:8]
    return Item(
        Kind.DECISION,
        f"Approve the design for ticket {ticket_id}: {title}",
        (
            f"Read the design (fingerprint {short}).",
            f"To approve, say 'approve the design for ticket {ticket_id}' or run:"
            f" jarvis-improver approve {ticket_id} {short}",
            f"To change it, run: jarvis-improver change {ticket_id} 'what to change'",
        ),
        ticket_id,
    )


def ready_to_merge(ticket_id: int, pr_url: str, title: str) -> Item:
    return Item(
        Kind.READY_TO_MERGE,
        f"Ticket {ticket_id} is ready for you: {title}",
        (
            f"Open the pull request: {pr_url}",
            "Review the diff, then approve and merge it on GitHub, or leave a review comment"
            " and JARVIS will send it back to the Builder.",
        ),
        ticket_id,
    )


def blocked_site(ticket_id: int, url: str) -> Item:
    host = urlsplit(url).hostname or "a site"
    return Item(
        Kind.BLOCKED_SITE,
        f"Ticket {ticket_id}: the Researcher couldn't open {host}",
        (
            f"If you want {host} used, open it yourself and paste what matters into the ticket:"
            f" jarvis-improver note {ticket_id} 'what you found'",
            "Or ignore this; the design will go ahead without it.",
        ),
        ticket_id,
    )


def paused(ticket_id: int, *, spent: float, cap: float) -> Item:
    return Item(
        Kind.PAUSED,
        f"Ticket {ticket_id} paused at ${spent:.2f} of its ${cap:.2f} cap",
        (
            f"To go on, run: jarvis-improver resume {ticket_id}",
            f"To drop it, run: jarvis-improver stop {ticket_id}",
        ),
        ticket_id,
    )


def stopped(ticket_id: int, why: str) -> Item:
    return Item(Kind.STOPPED, f"Ticket {ticket_id} stopped: {why}", (), ticket_id)


def warning(ticket_id: int | None, title: str, steps: Sequence[str] = ()) -> Item:
    return Item(Kind.WARNING, title, tuple(steps), ticket_id)


def render(items: Sequence[Item]) -> str:
    if not items:
        return "Nothing needs you."
    many = len(items) != 1
    lines = [f"{len(items)} thing{'s' if many else ''} need{'' if many else 's'} you:"]
    for n, item in enumerate(items, 1):
        lines.append(f"{n}. {item.title}")
        lines.extend(f"   - {step}" for step in item.steps)
    return "\n".join(lines)
