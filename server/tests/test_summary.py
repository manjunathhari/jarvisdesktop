"""Reconnect summary (spec §8.3; P2-E07, P2-E08): built by code from the outbox, no Claude.

Runs in its own database (comm.event rows block the 0009 downgrade). People are fictional.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import comm, nodes
from jarvis_server.comm import Priority
from jarvis_server.core import summary
from jarvis_server.signing import fingerprint

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
CFP = fingerprint(bytes(range(32)))
GFP = fingerprint(bytes(range(32, 64)))


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Asha Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


def _item(
    app: Conn,
    person: uuid.UUID,
    priority: Priority,
    kind: str,
    group_key: str,
    *,
    expires_at: datetime | None = None,
    supersedes_id: uuid.UUID | None = None,
    target: uuid.UUID | None = None,
) -> uuid.UUID:
    event_id = comm.record_event(
        app, kind, subject_type="test", subject_id=f"t-{secrets.token_hex(4)}",
        person_id=person, details={},
    )  # fmt: skip
    return comm.create_outbox(
        app,
        person_id=person,
        event_id=event_id,
        priority=priority,
        kind=kind,
        body={"text": "Fictional free text that the summary must never repeat"},
        group_key=group_key,
        expires_at=expires_at,
        supersedes_id=supersedes_id,
        target_node_id=target,
    )


def _node(app: Conn, gateway: Conn, person: uuid.UUID) -> uuid.UUID:
    code = nodes.create_enrolment(
        app, person_id=person, kind="DESKTOP", name="pc", core_fp=CFP, gw_fp=GFP
    )
    token, _, _ = nodes.parse_enrolment_code(code)
    return nodes.enrol(
        gateway,
        token=token,
        public_key=secrets.token_bytes(32),
        tailscale_identity=f"ts:pc-{secrets.token_hex(4)}",
    )


@pytest.fixture
def node(app: Conn, gateway: Conn, person: uuid.UUID) -> uuid.UUID:
    """The node whose summary is built (every summary is one node's, R12-23)."""
    return _node(app, gateway, person)


def _acknowledge(app: Conn, gateway: Conn, person: uuid.UUID, item: uuid.UUID) -> None:
    node_id = _node(app, gateway, person)
    row = gateway.execute("SELECT conn_id FROM gw.open_connection(%s)", (node_id,)).fetchone()
    assert row is not None
    assert gateway.execute("SELECT comm.record_sent(%s, %s)", (row[0], item)).fetchone() == (True,)
    acked = gateway.execute("SELECT comm.acknowledge(%s, %s)", (row[0], item)).fetchone()
    assert acked == ("ACKNOWLEDGED",)


def test_summary_order(app: Conn, gateway: Conn, person: uuid.UUID, node: uuid.UUID) -> None:
    """Mixed items: URGENT first one by one, TODAY grouped by group_key, DIGEST one line per
    group, SILENT absent; acknowledged, superseded and expired items absent (P2-E07)."""
    soon = datetime.now(UTC) + timedelta(minutes=10)
    t1 = _item(app, person, Priority.TODAY, "approval.expired", "approval:a1")
    d1 = _item(app, person, Priority.DIGEST, "brain.down", "brain")
    u1 = _item(app, person, Priority.URGENT, "approval.requested", "approval:a2", expires_at=soon)
    t2 = _item(app, person, Priority.TODAY, "budget.80", "budget")
    _item(app, person, Priority.SILENT, "action.readonly_ok", "action")
    t3 = _item(app, person, Priority.TODAY, "turn.indeterminate", "approval:a1")
    d2 = _item(app, person, Priority.DIGEST, "brain.down", "brain")
    u2 = _item(app, person, Priority.URGENT, "budget.100", "budget")
    d3 = _item(app, person, Priority.DIGEST, "brain.back", "brain:other")
    # Not open: superseded, expired and acknowledged items never appear.
    old = _item(app, person, Priority.URGENT, "approval.expiring", "approval:a3")
    _item(app, person, Priority.TODAY, "approval.expired", "approval:a3", supersedes_id=old)
    gone = _item(app, person, Priority.TODAY, "budget.80", "budget:old")
    assert app.execute("SELECT comm.expire(%s)", (gone,)).fetchone() == ("applied",)
    seen = _item(app, person, Priority.URGENT, "budget.100", "budget:seen")
    _acknowledge(app, gateway, person, seen)
    [new_expired] = [
        r[0]
        for r in app.execute(
            "SELECT id FROM comm.outbox WHERE supersedes_id = %s", (old,)
        ).fetchall()
    ]

    lines = summary.reconnect_summary(app, person, node)

    assert [line["priority"] for line in lines] == [
        "URGENT", "URGENT", "TODAY", "TODAY", "TODAY", "DIGEST", "DIGEST",
    ]  # fmt: skip
    assert [line["outbox_ids"] for line in lines] == [
        [u1],
        [u2],
        [t1, t3],  # approval:a1, the earlier group first, items in order
        [t2],  # budget
        [new_expired],  # approval:a3: the item that superseded the old one is open
        [d1, d2],  # brain: one summary line for the group
        [d3],
    ]
    assert [line["group_key"] for line in lines] == [
        "approval:a2", "budget", "approval:a1", "budget", "approval:a3", "brain", "brain:other",
    ]  # fmt: skip
    # Fixed templates only, never the stored body text.
    assert lines[0]["text"] == summary.LINE["approval.requested"]
    assert lines[1]["text"] == summary.LINE["budget.100"]
    assert lines[2]["lines"] == [
        summary.LINE["approval.expired"],
        summary.LINE["turn.indeterminate"],
    ]
    assert lines[5]["text"] == summary.DIGEST_LINE.format(label=summary.LINE["brain.down"], count=2)
    assert lines[5]["count"] == 2
    assert lines[6]["count"] == 1
    for line in lines:
        assert "Fictional free text" not in repr(line)


def test_summary_skips_items_past_expiry(app: Conn, person: uuid.UUID, node: uuid.UUID) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    _item(app, person, Priority.URGENT, "approval.requested", "approval:x", expires_at=past)
    assert summary.reconnect_summary(app, person, node) == []


def test_summary_unknown_kind_uses_generic_template(
    app: Conn, person: uuid.UUID, node: uuid.UUID
) -> None:
    item = _item(app, person, Priority.URGENT, "test.notice", "test")
    assert summary.reconnect_summary(app, person, node) == [
        {
            "priority": "URGENT",
            "group_key": "test",
            "kind": "test.notice",
            "outbox_ids": [item],
            "text": summary.GENERIC_LINE,
        }
    ]


def test_summary_is_per_person(app: Conn, owner: Conn, person: uuid.UUID, node: uuid.UUID) -> None:
    other = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Ravi Fictional', %s, 'test')",
        (other, HOUSEHOLD),
    )
    _item(app, other, Priority.URGENT, "budget.100", "budget")
    assert summary.reconnect_summary(app, person, node) == []


def test_summary_is_per_node(app: Conn, gateway: Conn, person: uuid.UUID, node: uuid.UUID) -> None:
    """Final review I-1, R12-23: an item addressed to one node is in that node's summary only;
    an item for every node is in each. The two approval-flow kinds have their own lines."""
    other = _node(app, gateway, person)
    approved = _item(app, person, Priority.URGENT, "action.approved", "action:a1", target=node)
    refused = _item(
        app, person, Priority.URGENT, "approval.decision_refused", "approval:a2", target=other
    )
    everyone = _item(app, person, Priority.TODAY, "budget.80", "budget")
    mine = summary.reconnect_summary(app, person, node)
    theirs = summary.reconnect_summary(app, person, other)
    assert [line["outbox_ids"] for line in mine] == [[approved], [everyone]]
    assert [line["outbox_ids"] for line in theirs] == [[refused], [everyone]]
    assert mine[0]["text"] == summary.LINE["action.approved"] != summary.GENERIC_LINE
    assert theirs[0]["text"] == summary.LINE["approval.decision_refused"] != summary.GENERIC_LINE


FORBIDDEN_ROOTS = ("anthropic", "httpx", "openai", "requests")


def test_summary_needs_no_brain() -> None:
    """P2-E08: importing the summary in a fresh interpreter loads no brain or model module,
    directly or through anything it imports."""
    probe = (
        "import json, sys\n"
        "import jarvis_server.core.summary\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True
    )
    loaded = json.loads(out.stdout)
    assert "jarvis_server.core.summary" in loaded  # the probe really imported it
    brain = [
        m for m in loaded if m == "jarvis_server.brain" or m.startswith("jarvis_server.brain.")
    ]
    model = [m for m in loaded if m.split(".")[0] in FORBIDDEN_ROOTS]
    assert brain == [] and model == []
