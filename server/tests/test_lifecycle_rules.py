"""Every allowed and every forbidden move, checked against the tables in the spec (§6)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from jarvis_server import contracts as c

# Written out from spec §6 on purpose: this is the independent check of migration 0004.
TASK = {
    "DETECTED": {"INTERPRETED", "CANCELLED", "SUPERSEDED"},
    "INTERPRETED": {"PROPOSED", "DEFERRED", "CANCELLED", "SUPERSEDED"},
    "PROPOSED": {"APPROVAL_PENDING", "READY", "DEFERRED", "CANCELLED", "SUPERSEDED"},
    "APPROVAL_PENDING": {"READY", "CANCELLED", "EXPIRED", "SUPERSEDED"},
    "READY": {"IN_PROGRESS", "BLOCKED", "DEFERRED", "CANCELLED", "SUPERSEDED"},
    "IN_PROGRESS": {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"},
    "BLOCKED": {"READY", "CANCELLED", "SUPERSEDED"},
    "DEFERRED": {"PROPOSED", "CANCELLED", "SUPERSEDED"},
}
COMMITMENT = {
    "EXPECTED": {"AT_RISK", "SECURED", "FULFILLED", "MISSED"},
    "AT_RISK": {"SECURED", "EXPECTED", "FULFILLED", "MISSED"},
    "SECURED": {"AT_RISK", "FULFILLED", "MISSED"},
}


def _allowed(app: psycopg.Connection[Any], entity: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for src, dst in app.execute(
        "SELECT from_state, to_state FROM ledger.allowed_transition WHERE entity_type = %s",
        (entity,),
    ):
        out.setdefault(src, set()).add(dst)
    return out


def test_task_rules_match_spec(app: psycopg.Connection[Any]) -> None:
    assert _allowed(app, "task") == TASK


def test_commitment_rules_match_spec(app: psycopg.Connection[Any]) -> None:
    assert _allowed(app, "commitment") == COMMITMENT


def test_terminal_states_have_no_exits() -> None:
    terminal_task = {"COMPLETED", "CANCELLED", "FAILED", "EXPIRED", "SUPERSEDED"}
    assert terminal_task.isdisjoint(TASK)
    assert set(c.TaskState) == set(TASK) | terminal_task
    assert set(c.CommitmentState) == set(COMMITMENT) | {"FULFILLED", "MISSED"}


@pytest.mark.parametrize(
    ("type_name", "enum"),
    [
        ("ledger.task_state", c.TaskState),
        ("ledger.commitment_state", c.CommitmentState),
        ("ledger.visibility", c.Visibility),
        ("audit.actor_kind", c.ActorKind),
        ("audit.action", c.AuditAction),
        ("audit.outcome", c.AuditOutcome),
    ],
)
def test_database_enums_match_contracts(
    app: psycopg.Connection[Any], type_name: str, enum: type[c.StrEnum]
) -> None:
    rows = app.execute(
        "SELECT unnest(enum_range(NULL::" + type_name + "))::text"  # static names above
    ).fetchall()
    assert [r[0] for r in rows] == [e.value for e in enum]
