"""`MemoryEngine.set_visibility` (spec §12; plan Task 13): only the subject or the recorder may
change an entry's visibility. Each change is a new git version with one audit row
(`SET_VISIBILITY`, the tag unchanged); a refusal is audited and changes nothing.

The engine rules run with the in-memory audit log; the last tests go through the Postgres audit
function (migration 0012). Fictional people only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jarvis_server.contracts import (
    ActorKind,
    AuditAction,
    AuditOutcome,
    MemoryCategory,
    MemoryTag,
    Visibility,
)
from jarvis_server.memory import checks
from jarvis_server.memory.checks import HOUSEHOLD_ID, ListAudit, StaticDirectory, add_test_family
from jarvis_server.memory.engine import SYSTEM, MemoryEngine
from jarvis_server.memory.errors import MemoryNotFound, MemoryRefused
from jarvis_server.memory.pg import PgAudit, PgDirectory, memory_entity_id

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]
TODAY = date(2026, 11, 24)
PRIVATE, SELECTED, FAMILY = (
    Visibility.PRIVATE,
    Visibility.SELECTED_PERSON,
    Visibility.FAMILY_SHARED,
)


@pytest.fixture
def audit() -> ListAudit:
    return ListAudit()


@pytest.fixture
def eng(tmp_path: Path, audit: ListAudit) -> MemoryEngine:
    e = MemoryEngine(
        tmp_path / "memory", StaticDirectory(), audit, HOUSEHOLD_ID, today=lambda: TODAY
    ).open()
    add_test_family(e)
    return e


def _entry(eng: MemoryEngine, *, subject: str = "daughter", by: str = "manj") -> str:
    """A PRIVATE preference about `subject`, recorded by `by`."""
    return eng.write(
        subject, MemoryCategory.PREFERENCE, "Likes mango lassi", MemoryTag.STATED, "test",
        by=by, domain="lifestyle",
    )  # fmt: skip


def test_set_visibility_by_subject_or_recorder_only(eng: MemoryEngine, audit: ListAudit) -> None:
    mid = _entry(eng)  # about daughter, recorded by manj
    eng.set_visibility(mid, FAMILY, by="daughter")  # the subject
    assert eng.get(mid, viewer=SYSTEM).visibility is FAMILY
    eng.set_visibility(mid, SELECTED, ["neighbour"], by="manj")  # the recorder
    entry = eng.get(mid, viewer=SYSTEM)
    assert (entry.visibility, entry.shared_with) == (SELECTED, ("neighbour",))
    # The neighbour can now see it, but seeing it is not owning it.
    assert eng.get(mid, viewer="neighbour").memory_id == mid
    head, before = eng.store.head(), len(audit.records)
    for who in ("neighbour", SYSTEM, "agent:jarvis"):
        with pytest.raises(MemoryRefused):
            eng.set_visibility(mid, PRIVATE, by=who)
    assert eng.store.head() == head  # no commit
    assert eng.get(mid, viewer=SYSTEM).visibility is SELECTED
    refusals = audit.records[before:]
    assert [(r.action, r.outcome, r.reason, r.commit) for r in refusals] == [
        (AuditAction.SET_VISIBILITY, AuditOutcome.REJECTED, "not-subject-or-recorder", None),
        (AuditAction.SET_VISIBILITY, AuditOutcome.REJECTED, "person-required", None),
        (AuditAction.SET_VISIBILITY, AuditOutcome.REJECTED, "person-required", None),
    ]


def test_household_member_is_not_the_subject_of_a_household_entry(eng: MemoryEngine) -> None:
    """Editing a household entry is open to household members (Phase 1); its visibility is
    not: only the recorder (the subject is the household, not a person)."""
    mid = eng.write(
        "household", MemoryCategory.PREFERENCE, "Dinner at eight", MemoryTag.STATED, "test",
        by="manj", visibility=FAMILY, domain="lifestyle",
    )  # fmt: skip
    with pytest.raises(MemoryRefused, match="not-subject-or-recorder"):
        eng.set_visibility(mid, PRIVATE, by="daughter")
    eng.set_visibility(mid, PRIVATE, by="manj")
    assert eng.get(mid, viewer="manj").visibility is PRIVATE


def test_set_visibility_new_git_version_and_audit(eng: MemoryEngine, audit: ListAudit) -> None:
    mid = _entry(eng)
    head, before = eng.store.head(), len(audit.records)
    eng.set_visibility(mid, FAMILY, by="daughter")
    assert eng.store.head() != head
    rec = audit.records[-1]
    assert len(audit.records) == before + 1
    assert (rec.action, rec.outcome, rec.ref, rec.from_tag, rec.to_tag) == (
        AuditAction.SET_VISIBILITY, AuditOutcome.APPLIED, mid, "STATED", "STATED",
    )  # fmt: skip
    assert rec.commit == eng.store.head() and rec.reason == "set-visibility"
    assert (rec.actor_kind, rec.actor_id) == (ActorKind.PERSON, str(checks.DAUGHTER_ID))
    versions = eng.history(mid, viewer="manj")
    assert [v.entry.visibility for v in versions] == [PRIVATE, FAMILY]
    assert versions[-1].commit == rec.commit
    # Only visibility moved: content, tag and recorder are as they were.
    first, last = versions[0].entry, versions[-1].entry
    assert last.model_copy(update={"visibility": PRIVATE}) == first
    # The file on disk carries it, so a fresh engine reads the same entry.
    again = MemoryEngine(eng.store.root, StaticDirectory(), audit, HOUSEHOLD_ID).open()
    assert again.get(mid, viewer=SYSTEM).visibility is FAMILY


def test_set_visibility_refusals_change_nothing(eng: MemoryEngine, audit: ListAudit) -> None:
    mid = _entry(eng)
    cases: list[tuple[tuple[Any, ...], str]] = [
        ((mid, PRIVATE, ()), "visibility-unchanged"),
        ((mid, SELECTED, ()), "invalid-entry"),  # SELECTED_PERSON needs someone
        ((mid, FAMILY, ["neighbour"]), "invalid-entry"),  # shared with only for SELECTED
        ((mid, SELECTED, ["nobody"]), "unknown-shared-with"),
        ((mid, Visibility.ORGANIZATION_SHARED, ()), "invalid-entry"),
        (("m-9999", FAMILY, ()), "no-such-entry"),
    ]
    head = eng.store.head()
    for (memory_id, visibility, shared), code in cases:
        with pytest.raises(MemoryRefused, match=code):
            eng.set_visibility(memory_id, visibility, shared, by="manj")
        assert audit.records[-1].outcome is AuditOutcome.REJECTED
    assert eng.store.head() == head
    eng.archive(mid, by="manj", reason="no-longer-true")
    with pytest.raises(MemoryRefused, match="entry-expired"):
        eng.set_visibility(mid, FAMILY, by="manj")


def test_set_visibility_does_not_reveal_hidden_entries(eng: MemoryEngine) -> None:
    """A person who can't see an entry gets `no-such-entry`, as for any other change."""
    mid = _entry(eng, subject="manj", by="manj")
    with pytest.raises(MemoryRefused, match="no-such-entry"):
        eng.set_visibility(mid, FAMILY, by="neighbour")
    with pytest.raises(MemoryNotFound):
        eng.get(mid, viewer="neighbour")


# --- through the Postgres audit function (migration 0012) -----------------------------------


@pytest.fixture(scope="module")
def vdb() -> Iterator[Dsns]:
    with fresh_database() as d:
        with psycopg.connect(d.app, autocommit=True) as conn:
            for pid, name, org in (
                (checks.DAUGHTER_ID, "Daughter (fictional)", checks.HOUSEHOLD_ID),
                (checks.NEIGHBOUR_ID, "Neighbour (fictional)", None),
            ):
                conn.execute(
                    "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
                    " VALUES (%s, %s, %s, 'test')",
                    (pid, name, org),
                )
        yield d


def test_set_visibility_audited_in_postgres(vdb: Dsns, tmp_path: Path) -> None:
    with psycopg.connect(vdb.app, autocommit=True) as app:
        e = MemoryEngine(tmp_path / "memory", PgDirectory(app), PgAudit(app), HOUSEHOLD_ID)
        e.open()
        add_test_family(e)
        mid = _entry(e)
        e.set_visibility(mid, FAMILY, by="daughter")
        with pytest.raises(MemoryRefused):  # FAMILY_SHARED: hidden from the neighbour
            e.set_visibility(mid, PRIVATE, by="neighbour")
        rows = app.execute(
            "SELECT action::text, from_state, to_state, outcome::text, actor_id, reason, details"
            " FROM audit.event WHERE entity_type = 'memory' AND entity_id = %s ORDER BY seq",
            (memory_entity_id(mid),),
        ).fetchall()
        assert [r[:6] for r in rows] == [
            ("CREATE", None, "STATED", "APPLIED", str(checks.MANJ_ID), "create"),
            ("SET_VISIBILITY", "STATED", "STATED", "APPLIED", str(checks.DAUGHTER_ID),
             "set-visibility"),
            ("SET_VISIBILITY", None, None, "REJECTED", str(checks.NEIGHBOUR_ID),
             "no-such-entry"),
        ]  # fmt: skip
        assert rows[1][6] == {"memory_id": mid, "commit": e.store.head()}


@pytest.mark.parametrize(
    "tags",
    [("STATED", "CONFIRMED"), ("CONFIRMED", "STATED"), ("EXPIRED", "EXPIRED"), (None, "STATED")],
)
def test_record_memory_set_visibility_keeps_the_tag(vdb: Dsns, tags: tuple[Any, Any]) -> None:
    """The database refuses a SET_VISIBILITY that changes a tag or touches an expired entry."""
    with (
        psycopg.connect(vdb.app, autocommit=True) as app,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        app.execute(
            "SELECT audit.record_memory('SYSTEM', 'system', 'm-0001', 'SET_VISIBILITY',"
            " %s, %s, 'APPLIED', 'set-visibility', %s)",
            (*tags, "a" * 40),
        )
