"""Reconnect summary (spec §8.3; P2-E07, P2-E08): built by code from the outbox alone.

Every line comes from a fixed template keyed by the item's kind; no stored body text is
repeated and no model is called, so the summary works while Claude is down. Only open items
count (PENDING, DELIVERING, DELIVERED, and not past `expires_at`): acknowledged, superseded and
expired items are left out. The order is §8.3's:

- URGENT first, one entry per item, oldest first;
- TODAY grouped by `group_key` (groups in order of their oldest item), one line per item;
- DIGEST one summary line per `group_key`;
- SILENT never (history only).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

import psycopg

# One fixed line per known item kind (the event kinds of core.rules).
LINE: dict[str, str] = {
    "approval.requested": "Approval waiting for your decision",
    "approval.expiring": "Approval expires in 2 minutes",
    "approval.expired": "Approval expired, not done",
    "brain.down": "Claude was unavailable",
    "brain.down.with_held": "Claude was unavailable; a request is on hold",
    "brain.back": "Claude is available again",
    "budget.80": "Claude budget 80% used this month",
    "budget.100": "Claude budget used up this month",
    "budget.daily": "Claude budget used up for today",
    "turn.cap_expired": "A held request expired",
    "turn.down_expired": "A held request expired while Claude was unavailable",
    "turn.indeterminate": "A request ended without a clear outcome",
    "turn.completed_after_hold": "A held request has been answered",
    "action.readonly_ok": "Action completed",
    "brain.config_error": "Claude configuration error",
}
GENERIC_LINE = "A notification is waiting"
DIGEST_LINE = "{label} ({count})"

QUERY = """
SELECT id, priority, kind, group_key FROM comm.outbox
WHERE person_id = %s
  AND state IN ('PENDING', 'DELIVERING', 'DELIVERED')
  AND priority IN ('URGENT', 'TODAY', 'DIGEST')
  AND (expires_at IS NULL OR expires_at > now())
ORDER BY created_at, id
"""


def _line(kind: str) -> str:
    return LINE.get(kind, GENERIC_LINE)


def _grouped(rows: list[tuple[UUID, str, str]]) -> dict[str, list[tuple[UUID, str]]]:
    groups: dict[str, list[tuple[UUID, str]]] = {}  # insertion order = oldest item first
    for outbox_id, kind, group_key in rows:
        groups.setdefault(group_key, []).append((outbox_id, kind))
    return groups


def reconnect_summary(conn: psycopg.Connection[Any], person_id: UUID) -> list[dict[str, Any]]:
    """The person's open outbox items as summary entries, in §8.3 order."""
    return build_summary(conn.execute(QUERY, (person_id,)).fetchall())


def build_summary(rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    """Summary entries from `QUERY`'s rows (`id, priority, kind, group_key`, in its order).
    Pure, so the async gateway runs the same query and builds the same entries."""
    by_priority: dict[str, list[tuple[UUID, str, str]]] = {"URGENT": [], "TODAY": [], "DIGEST": []}
    for outbox_id, priority, kind, group_key in rows:
        by_priority[priority].append((UUID(str(outbox_id)), kind, group_key))

    entries: list[dict[str, Any]] = [
        {
            "priority": "URGENT",
            "group_key": group_key,
            "kind": kind,
            "outbox_ids": [outbox_id],
            "text": _line(kind),
        }
        for outbox_id, kind, group_key in by_priority["URGENT"]
    ]
    for group_key, items in _grouped(by_priority["TODAY"]).items():
        entries.append(
            {
                "priority": "TODAY",
                "group_key": group_key,
                "count": len(items),
                "outbox_ids": [outbox_id for outbox_id, _ in items],
                "lines": [_line(kind) for _, kind in items],
            }
        )
    for group_key, items in _grouped(by_priority["DIGEST"]).items():
        latest_kind = items[-1][1]
        entries.append(
            {
                "priority": "DIGEST",
                "group_key": group_key,
                "count": len(items),
                "outbox_ids": [outbox_id for outbox_id, _ in items],
                "text": DIGEST_LINE.format(label=_line(latest_kind), count=len(items)),
            }
        )
    return entries
