"""Deterministic notification rules (spec §8.1; Task 5 controller rulings 1-3).

Runs in its own database: comm.event rows block the 0009 downgrade, so they must never reach
the session database that P0-6 takes down and up. People are fictional test values.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import comm
from jarvis_server.comm import OutboxState, Priority
from jarvis_server.core import rules

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    """A new fictional person per test, so lookups never see another test's items."""
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Asha Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


def _event(app: Conn, person: uuid.UUID, kind: str) -> uuid.UUID:
    return comm.record_event(
        app, kind, subject_type="test", subject_id=f"t-{uuid.uuid4().hex[:8]}",
        person_id=person, details={"code": "fictional"},
    )  # fmt: skip


def _notify(app: Conn, person: uuid.UUID, kind: str, **context: Any) -> list[uuid.UUID]:
    return rules.notify(
        app, kind, person_id=person, event_id=_event(app, person, kind), context=context
    )


def _row(app: Conn, outbox_id: uuid.UUID) -> dict[str, Any]:
    cur = app.execute(
        "SELECT priority, kind, state, group_key, supersedes_id, expires_at, body, person_id"
        " FROM comm.outbox WHERE id = %s",
        (outbox_id,),
    )
    row = cur.fetchone()
    assert row is not None
    assert cur.description is not None
    return dict(zip([c.name for c in cur.description], row, strict=True))


def _in(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "event,expected",
    [
        ("approval.requested", Priority.URGENT),  # 10-min expiry -> URGENT (§8.1, C1)
        ("approval.expiring", Priority.URGENT),
        ("approval.expired", Priority.TODAY),
        ("brain.down", Priority.DIGEST),
        ("brain.down.with_held", Priority.TODAY),
        ("brain.back", Priority.DIGEST),
        ("budget.80", Priority.TODAY),
        ("budget.100", Priority.URGENT),
        ("turn.cap_expired", Priority.TODAY),
        ("turn.indeterminate", Priority.TODAY),
        ("turn.completed_after_hold", Priority.TODAY),  # controller ruling 1
        ("action.readonly_ok", Priority.SILENT),
        ("brain.config_error", Priority.TODAY),
    ],
)
def test_rule_priority(app: Conn, person: uuid.UUID, event: str, expected: Priority) -> None:
    ids = _notify(app, person, event, expires_at=_in(10).isoformat())
    assert len(ids) == 1
    row = _row(app, ids[0])
    assert row["priority"] == expected.value
    assert row["kind"] == event
    assert row["state"] == OutboxState.PENDING.value
    assert row["person_id"] == person
    assert row["body"]["event"] == event


def test_event_kinds_are_exactly_the_shared_interface() -> None:
    assert set(rules.EVENT_KINDS) == {
        "approval.requested", "approval.expiring", "approval.expired", "brain.down",
        "brain.down.with_held", "brain.back", "budget.80", "budget.100", "turn.cap_expired",
        "turn.indeterminate", "turn.completed_after_hold", "action.readonly_ok",
        "brain.config_error",
    }  # fmt: skip


def test_approval_requested_ten_minutes_is_urgent_and_expires_with_it(
    app: Conn, person: uuid.UUID
) -> None:
    expires = _in(10)
    [item] = _notify(app, person, "approval.requested", expires_at=expires.isoformat())
    row = _row(app, item)
    assert row["priority"] == "URGENT"
    assert row["expires_at"] == expires


def test_approval_requested_over_an_hour_is_today(app: Conn, person: uuid.UUID) -> None:
    [item] = _notify(app, person, "approval.requested", expires_at=_in(120))  # Phase 3 branch
    assert _row(app, item)["priority"] == "TODAY"


def test_approval_requested_needs_expiry(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person, "approval.requested")
    with pytest.raises(ValueError, match="expires_at"):
        rules.notify(app, "approval.requested", person_id=person, event_id=event_id, context={})
    row = app.execute("SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,))
    assert row.fetchone() == (0,)


def test_unknown_event_raises(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person, "budget.90")
    with pytest.raises(rules.UnknownEvent, match=r"budget\.90"):
        rules.notify(app, "budget.90", person_id=person, event_id=event_id, context={})
    row = app.execute("SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,))
    assert row.fetchone() == (0,)


def test_brain_back_supersedes_down(app: Conn, person: uuid.UUID) -> None:
    [down] = _notify(app, person, "brain.down")
    [back] = _notify(app, person, "brain.back")  # no supersedes_id given: the documented lookup
    assert _row(app, down)["state"] == "SUPERSEDED"
    back_row = _row(app, back)
    assert back_row["supersedes_id"] == down
    assert back_row["priority"] == "DIGEST"
    assert back_row["state"] == "PENDING"


def test_brain_back_supersedes_down_with_held(app: Conn, person: uuid.UUID) -> None:
    [down] = _notify(app, person, "brain.down.with_held")
    [back] = _notify(app, person, "brain.back")
    assert _row(app, down)["state"] == "SUPERSEDED"
    assert _row(app, back)["supersedes_id"] == down


def test_brain_back_without_down_supersedes_nothing(app: Conn, person: uuid.UUID) -> None:
    [back] = _notify(app, person, "brain.back")
    assert _row(app, back)["supersedes_id"] is None


def test_brain_back_ignores_other_people(app: Conn, owner: Conn, person: uuid.UUID) -> None:
    other = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Ravi Fictional', %s, 'test')",
        (other, HOUSEHOLD),
    )
    [theirs] = _notify(app, other, "brain.down")
    [back] = _notify(app, person, "brain.back")
    assert _row(app, back)["supersedes_id"] is None
    assert _row(app, theirs)["state"] == "PENDING"


def test_brain_down_does_not_look_up_on_its_own(app: Conn, person: uuid.UUID) -> None:
    """Ruling 2: only brain.back looks up; an in-place update of "down" comes via context."""
    [first] = _notify(app, person, "brain.down")
    [second] = _notify(app, person, "brain.down.with_held")
    assert _row(app, second)["supersedes_id"] is None
    assert _row(app, first)["state"] == "PENDING"
    [third] = _notify(app, person, "brain.down", supersedes_id=str(second))
    assert _row(app, third)["supersedes_id"] == second
    assert _row(app, second)["state"] == "SUPERSEDED"


def test_supersede_and_expiry_come_from_context(app: Conn, person: uuid.UUID) -> None:
    expires = _in(10)
    [card] = _notify(app, person, "approval.requested", expires_at=expires)
    [again] = _notify(app, person, "approval.expiring", supersedes_id=card, expires_at=expires)
    assert _row(app, card)["state"] == "SUPERSEDED"
    row = _row(app, again)
    assert row["supersedes_id"] == card and row["expires_at"] == expires
    [held] = _notify(app, person, "brain.down.with_held")
    [expired] = _notify(app, person, "turn.cap_expired", supersedes_id=str(held))
    assert _row(app, held)["state"] == "SUPERSEDED"
    assert _row(app, expired)["supersedes_id"] == held


def test_group_key_defaults_and_context(app: Conn, person: uuid.UUID) -> None:
    [budget] = _notify(app, person, "budget.80")
    assert _row(app, budget)["group_key"] == "budget"
    session = uuid.uuid4()
    [turn] = _notify(app, person, "turn.indeterminate", group_key=f"session:{session}")
    assert _row(app, turn)["group_key"] == f"session:{session}"


def test_completed_after_hold_carries_the_reply(app: Conn, person: uuid.UUID) -> None:
    [item] = _notify(
        app, person, "turn.completed_after_hold", text="The fictional answer.", reply_id="r-1"
    )
    body = _row(app, item)["body"]
    assert body["text"] == "The fictional answer."
    assert body["refs"] == {"reply_id": "r-1"}


def test_notify_is_idempotent_per_event(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person, "budget.80")
    first = rules.notify(app, "budget.80", person_id=person, event_id=event_id, context={})
    second = rules.notify(app, "budget.80", person_id=person, event_id=event_id, context={})
    assert first == second
    row = app.execute("SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,))
    assert row.fetchone() == (1,)
