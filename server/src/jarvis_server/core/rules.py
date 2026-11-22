"""Deterministic notification rules (spec §8.1): an internal event becomes outbox items.

The event kinds below are a shared interface: later tasks emit exactly these strings, and an
unknown kind is a programming error (`UnknownEvent`), not an audited refusal.

`context` carries what the caller knows about the item (controller ruling 2):
- `expires_at` (datetime or RFC 3339 string): the item expires then, e.g. an approval card
  expires with its approval. Required for `approval.requested`, whose priority depends on it.
- `supersedes_id` (UUID or string): the earlier item this one replaces, handed to
  `comm.create_outbox`. The rules never look for an item to supersede on their own, with one
  documented exception: `brain.back` supersedes the person's open "Claude down" item.
- `group_key`: overrides the kind's default group.
- `text`: the item's text, replacing the fixed one (the reply itself, for
  `turn.completed_after_hold`).
- `authz`: core's signed authorisation, for approval cards (§5.3, §7.1).
- anything else: references (IDs and codes), stored under `body["refs"]`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from jarvis_server import comm
from jarvis_server.comm import Priority


class UnknownEvent(LookupError):
    """An event kind with no notification rule (a programming error)."""


@dataclass(frozen=True)
class Rule:
    priority: Priority
    group_key: str
    text: str


# Approval requested: URGENT if it expires within this, else TODAY (§8.1). Every Phase 2
# approval expires after 10 minutes, so every one is URGENT; TODAY is for Phase 3.
URGENT_WITHIN = timedelta(hours=1)

RULES: dict[str, Rule] = {
    "approval.requested": Rule(Priority.URGENT, "approval", "Approval needed"),
    "approval.expiring": Rule(Priority.URGENT, "approval", "Approval expires in 2 minutes"),
    "approval.expired": Rule(Priority.TODAY, "approval", "Expired, not done"),
    "brain.down": Rule(Priority.DIGEST, "brain", "Claude is unavailable"),
    "brain.down.with_held": Rule(
        Priority.TODAY, "brain", "Claude is unavailable; a request is on hold"
    ),
    "brain.back": Rule(Priority.DIGEST, "brain", "Claude is available again"),
    "budget.80": Rule(Priority.TODAY, "budget", "Claude budget 80% used this month"),
    "budget.100": Rule(
        Priority.URGENT, "budget", "Claude budget used up this month; Claude calls stopped"
    ),
    "turn.cap_expired": Rule(
        Priority.TODAY, "turn", "A held request expired before the budget allowed it"
    ),
    "turn.indeterminate": Rule(Priority.TODAY, "turn", "A request ended without a clear outcome"),
    "turn.completed_after_hold": Rule(Priority.TODAY, "turn", "A held request has been answered"),
    "action.readonly_ok": Rule(Priority.SILENT, "action", "Action completed"),
    "brain.config_error": Rule(Priority.TODAY, "brain", "Claude configuration error"),
}

EVENT_KINDS: tuple[str, ...] = tuple(RULES)

# brain.back supersedes the person's newest open item of these kinds (§8.1 "Claude back").
_DOWN_KINDS = ("brain.down", "brain.down.with_held")
_RESERVED = frozenset({"expires_at", "supersedes_id", "group_key", "text", "authz"})
_OPEN = (
    comm.OutboxState.PENDING.value,
    comm.OutboxState.DELIVERING.value,
    comm.OutboxState.DELIVERED.value,
)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value)
    else:
        raise ValueError("expires_at must be a datetime or an RFC 3339 string")
    if moment.tzinfo is None:
        raise ValueError("expires_at must carry a time zone")
    return moment


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError("supersedes_id must be a UUID")


def _ref(value: Any) -> Any:
    return str(value) if isinstance(value, UUID | datetime) else value


def _priority(conn: psycopg.Connection[Any], kind: str, expires_at: datetime | None) -> Priority:
    rule = RULES[kind]
    if kind != "approval.requested":
        return rule.priority
    if expires_at is None:
        raise ValueError("approval.requested needs context['expires_at']")
    row = conn.execute("SELECT %s <= now() + %s", (expires_at, URGENT_WITHIN)).fetchone()
    assert row is not None
    return Priority.URGENT if row[0] else Priority.TODAY


def _open_down_item(conn: psycopg.Connection[Any], person_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT id FROM comm.outbox WHERE person_id = %s AND kind = ANY(%s) AND state = ANY(%s)"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (person_id, list(_DOWN_KINDS), list(_OPEN)),
    ).fetchone()
    return None if row is None else UUID(str(row[0]))


def notify(
    conn: psycopg.Connection[Any],
    event_kind: str,
    *,
    person_id: UUID,
    event_id: UUID,
    context: Mapping[str, Any],
) -> list[UUID]:
    """Apply the §8.1 rule for `event_kind`; return the ids of the outbox items it created.

    Idempotent per (event, kind): a second call for the same event returns the items the first
    created and writes nothing, so a re-run job changes nothing (§5.4).
    """
    rule = RULES.get(event_kind)
    if rule is None:
        raise UnknownEvent(f"no notification rule for event kind {event_kind!r}")
    expires_at = _as_datetime(context.get("expires_at"))
    supersedes_id = _as_uuid(context.get("supersedes_id"))
    text = context.get("text", rule.text)
    if not isinstance(text, str) or not text:
        raise ValueError("context['text'] must be a non-empty string")
    authz = context.get("authz")
    refs = {k: _ref(v) for k, v in context.items() if k not in _RESERVED}
    body: dict[str, Any] = {"event": event_kind, "text": text}
    if refs:
        body["refs"] = refs

    with conn.transaction():
        # Serialise notifications of one event, then return what an earlier run created.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(event_id),))
        done = conn.execute(
            "SELECT id FROM comm.outbox WHERE event_id = %s AND kind = %s ORDER BY created_at, id",
            (event_id, event_kind),
        ).fetchall()
        if done:
            return [UUID(str(r[0])) for r in done]
        priority = _priority(conn, event_kind, expires_at)
        if event_kind == "brain.back" and supersedes_id is None:
            supersedes_id = _open_down_item(conn, person_id)
        outbox_id = comm.create_outbox(
            conn,
            person_id=person_id,
            event_id=event_id,
            priority=priority,
            kind=event_kind,
            body=body,
            group_key=str(context.get("group_key", rule.group_key)),
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            authz=authz,
        )
    return [outbox_id]
