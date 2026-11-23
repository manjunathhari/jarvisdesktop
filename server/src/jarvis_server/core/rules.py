"""Deterministic notification rules (spec §8.1): an internal event becomes outbox items.

The event kinds below are a shared interface: later tasks emit exactly these strings, and an
unknown kind is a programming error (`UnknownEvent`), not an audited refusal.

The caller passes one typed `NotifyContext` (R11-3, 11b). Every field is optional:
- `supersedes_id`: the earlier item this one replaces, handed to `comm.create_outbox`. The rules
  never look for an item to supersede on their own, with one documented exception:
  `brain.back` supersedes the person's open "Claude down" item (controller ruling 2).
- `expires_at`: a timezone-aware datetime; the item expires then (an approval card expires
  with its approval). Required for `approval.requested`, whose priority depends on it.
- `group_key`: a non-empty key wins; omitted, `None` or empty falls back to the kind's fixed
  default group, never a placeholder such as "None" (comm.outbox.group_key stays NOT NULL).
- `text`: replaces the fixed text (the reply itself, for `turn.completed_after_hold`).
- `authz`: the raw signed dict `{object, key_id, sig}`, checked for exactly that shape and
  carried unchanged. It is never parsed into a model and rebuilt, and nothing here checks its
  signature or marks it trusted: the node does that under §7.1.

Any other key, or a value of the wrong type, raises `ValueError` before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypedDict, cast
from uuid import UUID

import psycopg

from jarvis_server import comm
from jarvis_server.comm import Priority


class UnknownEvent(LookupError):
    """An event kind with no notification rule (a programming error)."""


class AuthzDict(TypedDict):
    """The raw core-signed authorisation, exactly as signed (§7.1)."""

    object: dict[str, Any]
    key_id: str
    sig: str


class NotifyContext(TypedDict, total=False):
    supersedes_id: UUID
    expires_at: datetime
    group_key: str | None
    text: str
    authz: AuthzDict


_CONTEXT_KEYS = frozenset(NotifyContext.__annotations__)
_AUTHZ_KEYS = frozenset(AuthzDict.__annotations__)


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
    "brain.config_error": Rule(Priority.TODAY, "brain.config", "Claude configuration error"),
}

EVENT_KINDS: tuple[str, ...] = tuple(RULES)

# brain.back supersedes the person's newest open item of these kinds (§8.1 "Claude back").
_DOWN_KINDS = ("brain.down", "brain.down.with_held")
_OPEN = (
    comm.OutboxState.PENDING.value,
    comm.OutboxState.DELIVERING.value,
    comm.OutboxState.DELIVERED.value,
)


def _checked(context: NotifyContext) -> NotifyContext:
    """The context, its types checked at runtime (a TypedDict enforces nothing by itself)."""
    if not isinstance(context, dict):
        raise ValueError("context must be a NotifyContext dict")
    extra = set(context) - _CONTEXT_KEYS
    if extra:
        raise ValueError(f"context has keys outside NotifyContext: {sorted(extra)}")
    supersedes_id = context.get("supersedes_id")
    if supersedes_id is not None and not isinstance(supersedes_id, UUID):
        raise ValueError("context['supersedes_id'] must be a UUID")
    expires_at = context.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, datetime):
            raise ValueError("context['expires_at'] must be a datetime")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("context['expires_at'] must carry a time zone")
    group_key = context.get("group_key")
    if group_key is not None and not isinstance(group_key, str):
        raise ValueError("context['group_key'] must be a string or None")
    if "text" in context and (not isinstance(context["text"], str) or not context["text"]):
        raise ValueError("context['text'] must be a non-empty string")
    if "authz" in context:
        _check_authz(context["authz"])
    return context


def _check_authz(authz: Any) -> None:
    """Exactly {object: dict, key_id: str, sig: str}; the dict itself is passed on unchanged."""
    if not isinstance(authz, dict) or set(authz) != _AUTHZ_KEYS:
        raise ValueError("context['authz'] must be a dict with exactly object, key_id and sig")
    if not isinstance(authz["object"], dict):
        raise ValueError("context['authz']['object'] must be a dict")
    if not isinstance(authz["key_id"], str) or not isinstance(authz["sig"], str):
        raise ValueError("context['authz'] key_id and sig must be strings")


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
    context: NotifyContext,
) -> list[UUID]:
    """Apply the §8.1 rule for `event_kind`; return the ids of the outbox items it created.

    Idempotent per (event, kind): a second call for the same event returns the items the first
    created and writes nothing, so a re-run job changes nothing (§5.4).
    """
    rule = RULES.get(event_kind)
    if rule is None:
        raise UnknownEvent(f"no notification rule for event kind {event_kind!r}")
    context = _checked(context)
    expires_at = context.get("expires_at")
    supersedes_id = context.get("supersedes_id")
    group_key = context.get("group_key") or rule.group_key  # None or "" -> the kind's default
    authz = context.get("authz")
    body: dict[str, Any] = {"event": event_kind, "text": context.get("text", rule.text)}

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
            group_key=group_key,
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            authz=None if authz is None else cast("dict[str, Any]", authz),  # unchanged
        )
    return [outbox_id]
