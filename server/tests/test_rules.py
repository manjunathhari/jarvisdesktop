"""Deterministic notification rules (spec §8.1; Task 5 controller rulings 1-3).

Runs in its own database: comm.event rows block the 0009 downgrade, so they must never reach
the session database that P0-6 takes down and up. People are fictional test values.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Unpack

import psycopg
import pytest

from jarvis_server import comm
from jarvis_server.comm import OutboxState, Priority
from jarvis_server.core import rules, summary

from .conftest import Dsns, fresh_database
from .fakes.node_rules import Rejected, check_node_action

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


def _notify(
    app: Conn, person: uuid.UUID, kind: str, **context: Unpack[rules.NotifyContext]
) -> list[uuid.UUID]:
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
        ("budget.daily", Priority.URGENT),  # R12-14
        ("turn.cap_expired", Priority.TODAY),
        ("turn.down_expired", Priority.TODAY),  # R12-18
        ("turn.indeterminate", Priority.TODAY),
        ("turn.completed_after_hold", Priority.TODAY),  # controller ruling 1
        ("action.readonly_ok", Priority.SILENT),
        ("brain.config_error", Priority.TODAY),
    ],
)
def test_rule_priority(app: Conn, person: uuid.UUID, event: str, expected: Priority) -> None:
    ids = _notify(app, person, event, expires_at=_in(10))
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
        "brain.down.with_held", "brain.back", "budget.80", "budget.100", "budget.daily",
        "turn.cap_expired", "turn.down_expired",
        "turn.indeterminate", "turn.completed_after_hold", "action.readonly_ok",
        "brain.config_error", "action.approved", "memory.written", "approval.decision_refused",
    }  # fmt: skip


def test_approval_requested_ten_minutes_is_urgent_and_expires_with_it(
    app: Conn, person: uuid.UUID
) -> None:
    expires = _in(10)
    [item] = _notify(app, person, "approval.requested", expires_at=expires)
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
    [third] = _notify(app, person, "brain.down", supersedes_id=second)
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
    [expired] = _notify(app, person, "turn.cap_expired", supersedes_id=held)
    assert _row(app, held)["state"] == "SUPERSEDED"
    assert _row(app, expired)["supersedes_id"] == held


def test_completed_after_hold_carries_the_reply(app: Conn, person: uuid.UUID) -> None:
    [item] = _notify(app, person, "turn.completed_after_hold", text="The fictional answer.")
    assert _row(app, item)["body"] == {
        "event": "turn.completed_after_hold",
        "text": "The fictional answer.",
    }


def _outbox_count(app: Conn, event_id: uuid.UUID) -> int:
    row = app.execute("SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,))
    got = row.fetchone()
    assert got is not None
    return int(got[0])


def test_notify_context_types(app: Conn, person: uuid.UUID) -> None:
    """R11-3: every NotifyContext field is accepted; a naive expires_at writes nothing."""
    event_id = _event(app, person, "approval.requested")
    naive = datetime.now() + timedelta(minutes=10)  # deliberately naive
    with pytest.raises(ValueError, match="time zone"):
        rules.notify(
            app, "approval.requested", person_id=person, event_id=event_id,
            context={"expires_at": naive},
        )  # fmt: skip
    assert _outbox_count(app, event_id) == 0

    [prior] = _notify(app, person, "approval.requested", expires_at=_in(10))
    expires = _in(10)
    authz: rules.AuthzDict = {"object": {"kind": "approval.card"}, "key_id": "core-1", "sig": "x"}
    context: rules.NotifyContext = {
        "supersedes_id": prior,
        "expires_at": expires,
        "group_key": "approval:fictional-1",
        "text": "Approve the fictional purchase?",
        "authz": authz,
    }
    [item] = rules.notify(
        app, "approval.expiring", person_id=person,
        event_id=_event(app, person, "approval.expiring"), context=context,
    )  # fmt: skip
    row = _row(app, item)
    assert row["supersedes_id"] == prior and row["expires_at"] == expires
    assert row["group_key"] == "approval:fictional-1"
    assert row["body"] == {"event": "approval.expiring", "text": "Approve the fictional purchase?"}
    stored = app.execute("SELECT authz FROM comm.outbox WHERE id = %s", (item,)).fetchone()
    assert stored == (authz,)
    assert _row(app, prior)["state"] == "SUPERSEDED"


@pytest.mark.parametrize(
    "bad",
    [
        {"supersedes_id": "00000000-0000-4000-8000-0000000000aa"},  # a string, not a UUID
        {"expires_at": "2026-11-23T10:00:00+05:30"},  # a string, not a datetime
        {"group_key": 7},
        {"text": ""},
        {"reply_id": "r-1"},  # no free "refs" keys (R11-3)
    ],
)
def test_notify_context_rejects_other_types(
    app: Conn, person: uuid.UUID, bad: dict[str, Any]
) -> None:
    event_id = _event(app, person, "budget.80")
    with pytest.raises(ValueError, match="context"):
        rules.notify(app, "budget.80", person_id=person, event_id=event_id, context=bad)  # type: ignore[arg-type]
    assert _outbox_count(app, event_id) == 0


def test_group_key_fallback(app: Conn, person: uuid.UUID) -> None:
    """Option B (R11-3, 11b): omitted, None or '' -> the kind's fixed default, never 'None'."""
    default = rules.RULES["budget.80"].group_key
    [omitted] = _notify(app, person, "budget.80")
    [explicit_none] = _notify(app, person, "budget.80", group_key=None)
    [empty] = _notify(app, person, "budget.80", group_key="")
    [mine] = _notify(app, person, "budget.80", group_key="budget:2026-11")
    assert default == "budget"
    assert [_row(app, i)["group_key"] for i in (omitted, explicit_none, empty, mine)] == [
        "budget", "budget", "budget", "budget:2026-11",
    ]  # fmt: skip
    none_rows = app.execute(
        "SELECT count(*) FROM comm.outbox WHERE person_id = %s AND group_key = 'None'", (person,)
    ).fetchone()
    assert none_rows == (0,)
    # Two budget warnings without a key share the budget group ...
    [warn80] = _notify(app, person, "budget.80")
    [warn100] = _notify(app, person, "budget.100")
    assert _row(app, warn80)["group_key"] == _row(app, warn100)["group_key"] == "budget"
    # ... and different, unrelated kinds never share a group.
    [down] = _notify(app, person, "brain.down")
    [config] = _notify(app, person, "brain.config_error")
    [expired] = _notify(app, person, "approval.expired")
    [indeterminate] = _notify(app, person, "turn.indeterminate")
    keys = [_row(app, i)["group_key"] for i in (warn80, down, config, expired, indeterminate)]
    assert len(set(keys)) == len(keys)


AUTHZ_CASES = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts" / "signing-vectors" / "authz-cases.json"
    ).read_text()
)
TAMPERED = next(c for c in AUTHZ_CASES if c["name"] == "reject-tampered-args")


def test_authz_carried_untrusted(app: Conn, person: uuid.UUID) -> None:
    """The raw {object, key_id, sig} dict is stored unchanged; nothing on the server verifies
    it or marks it trusted, and the node rules still reject its bad signature (§7.1)."""
    assert TAMPERED["expect"] == "reject" and TAMPERED["reason"] == "bad_signature"
    raw = copy.deepcopy(TAMPERED["authz"])
    [item] = _notify(app, person, "approval.requested", expires_at=_in(10), authz=raw)
    assert raw == TAMPERED["authz"]  # the caller's dict was not changed either
    row = app.execute("SELECT authz, body FROM comm.outbox WHERE id = %s", (item,)).fetchone()
    assert row is not None
    stored, body = row
    assert stored == TAMPERED["authz"]
    assert set(stored) == {"object", "key_id", "sig"}
    assert set(stored["object"]) == set(TAMPERED["authz"]["object"])
    assert body == {"event": "approval.requested", "text": rules.RULES["approval.requested"].text}
    lines = summary.reconnect_summary(app, person)
    assert [line["outbox_ids"] for line in lines] == [[item]]
    for line in lines:
        assert not {"verified", "trusted", "authz", "sig"} & set(line)
    with pytest.raises(Rejected) as rejected:
        check_node_action(
            stored,
            trusted={k: bytes.fromhex(v) for k, v in TAMPERED["trusted_keys"].items()},
            me=uuid.UUID(TAMPERED["receiving_node_id"]),
            now=datetime.fromisoformat(TAMPERED["now"]),
        )
    assert rejected.value.reason == "bad_signature"


def test_rules_and_summary_load_no_signing_code() -> None:
    """No signature check on the server side of delivery: importing the rules and the summary
    in a fresh interpreter loads no signing or crypto module, directly or indirectly."""
    probe = (
        "import json, sys\n"
        "import jarvis_server.core.rules, jarvis_server.core.summary\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True
    )
    loaded = json.loads(out.stdout)
    assert "jarvis_server.core.rules" in loaded
    assert [m for m in loaded if m == "jarvis_server.signing" or m.startswith(
        ("cryptography", "nacl", "rfc8785", "jarvis_server.contracts"))] == []  # fmt: skip


@pytest.mark.parametrize(
    "authz",
    [
        {"object": {"kind": "approval.card"}, "key_id": "core-1"},  # missing sig
        {"key_id": "core-1", "sig": "x"},  # missing object
        {"object": {"kind": "approval.card"}, "key_id": "core-1", "sig": "x", "verified": True},
        ["object", "key_id", "sig"],  # not a dict
        {"object": "not-a-dict", "key_id": "core-1", "sig": "x"},
        {"object": {}, "key_id": 1, "sig": "x"},
    ],
)
def test_authz_shape_is_exact(app: Conn, person: uuid.UUID, authz: Any) -> None:
    event_id = _event(app, person, "approval.requested")
    events_before = app.execute("SELECT count(*) FROM comm.event").fetchone()
    with pytest.raises(ValueError, match="authz"):
        rules.notify(
            app, "approval.requested", person_id=person, event_id=event_id,
            context={"expires_at": _in(10), "authz": authz},
        )  # fmt: skip
    assert _outbox_count(app, event_id) == 0
    assert app.execute("SELECT count(*) FROM comm.event").fetchone() == events_before


def test_notify_is_idempotent_per_event(app: Conn, person: uuid.UUID) -> None:
    event_id = _event(app, person, "budget.80")
    first = rules.notify(app, "budget.80", person_id=person, event_id=event_id, context={})
    second = rules.notify(app, "budget.80", person_id=person, event_id=event_id, context={})
    assert first == second
    row = app.execute("SELECT count(*) FROM comm.outbox WHERE event_id = %s", (event_id,))
    assert row.fetchone() == (1,)
