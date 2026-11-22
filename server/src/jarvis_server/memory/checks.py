"""M1-M4 (Phase 1 spec §10) as runnable checks, with a fictional family only.

Used by the tests and by `jarvis-core smoke` at every deploy. The deploy check runs in a
temporary repo with an in-memory audit log and directory, so it never touches the real memory
volume or the live audit trail (lesson from Phase 0 finding Q3). No real personal data, ever.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jarvis_server.config import HOUSEHOLD_ID
from jarvis_server.contracts import MemoryCategory, MemoryTag, Visibility
from jarvis_server.memory.engine import SYSTEM, AuditRecord, MemoryEngine
from jarvis_server.memory.errors import MemoryRefused, NotActionable

# Fictional people. MANJ and HOUSEHOLD match the Phase 0 seed so Postgres tests can reuse them.
MANJ_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
DAUGHTER_ID = uuid.UUID("00000000-0000-4000-8000-00000000d001")
NEIGHBOUR_ID = uuid.UUID("00000000-0000-4000-8000-00000000d002")


@dataclass
class ListAudit:
    """In-memory audit sink (deploy check and unit tests)."""

    records: list[AuditRecord] = field(default_factory=list)
    fail_next: bool = False

    def record(self, rec: AuditRecord) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated audit failure")
        self.records.append(rec)

    def recorded_commits(self) -> set[str]:
        return {r.commit for r in self.records if r.commit}


@dataclass
class StaticDirectory:
    """In-memory people directory: person id -> organization id."""

    people: dict[uuid.UUID, uuid.UUID | None] = field(
        default_factory=lambda: {
            MANJ_ID: HOUSEHOLD_ID,
            DAUGHTER_ID: HOUSEHOLD_ID,
            NEIGHBOUR_ID: None,
        }
    )

    def organization_of(self, person_id: uuid.UUID) -> uuid.UUID | None:
        if person_id not in self.people:
            raise LookupError(person_id)
        return self.people[person_id]


def add_test_family(engine: MemoryEngine) -> None:
    engine.add_person("manj", MANJ_ID, "Manj (test)", by=SYSTEM)
    engine.add_person("daughter", DAUGHTER_ID, "Daughter (fictional)", by=SYSTEM)
    engine.add_person("neighbour", NEIGHBOUR_ID, "Neighbour (fictional)", by=SYSTEM)


def seed_m4(engine: MemoryEngine) -> dict[str, str]:
    """The M4 memory (spec §6 worked example). Returns name -> memory ID."""
    fs = Visibility.FAMILY_SHARED
    pref, ident = MemoryCategory.PREFERENCE, MemoryCategory.IDENTITY
    ids = {
        "identity": engine.write(
            "daughter", ident, "In Class 9", MemoryTag.STATED, "test", by="manj", visibility=fs
        ),
        "travel_sick": engine.write(
            "daughter",
            pref,
            "Gets travel-sick on long bus rides",
            MemoryTag.STATED,
            "test",
            by="manj",
            visibility=fs,
            domain="travel",
            topics=["travel"],
        ),
        "restaurant": engine.write(
            "daughter",
            pref,
            "Loves dosa at the corner cafe",
            MemoryTag.STATED,
            "test",
            by="manj",
            visibility=fs,
            domain="restaurants",
            topics=["food"],
        ),
        "private": engine.write(
            "daughter",
            pref,
            "Nervous about the school trip",
            MemoryTag.STATED,
            "test",
            by="daughter",
            domain="lifestyle",
            topics=["school"],
        ),
        "old_school": engine.write(
            "daughter",
            ident,
            "Studies at Old Oak School",
            MemoryTag.STATED,
            "test",
            by="manj",
            visibility=fs,
        ),
        "manj_flights": engine.write(
            "manj",
            pref,
            "Prefers aisle seats on flights",
            MemoryTag.STATED,
            "test",
            by="manj",
            visibility=fs,
            domain="travel",
            topics=["travel"],
        ),
        "pickups": engine.write(
            "household",
            MemoryCategory.OPERATIONAL,
            "Friday school pickups are done by Manj",
            MemoryTag.STATED,
            "test",
            by="manj",
            visibility=fs,
            domain="routines",
            topics=["school"],
        ),
    }
    engine.archive(ids["old_school"], by="manj", reason="no-longer-true")
    return ids


M4_EXPECTED = ("identity", "travel_sick", "pickups")


def check_m1(engine: MemoryEngine) -> tuple[bool, str]:
    mid = engine.write(
        "manj",
        MemoryCategory.PREFERENCE,
        "Prefers window seats",
        MemoryTag.STATED,
        "msg-m1",
        by="manj",
        domain="travel",
        topics=["travel"],
    )
    read = [
        e
        for e in engine.read("manj", MemoryCategory.PREFERENCE, viewer="manj")
        if e.memory_id == mid
    ]
    ctx = [
        e
        for e in engine.retrieve_context("manj", "flight booking", viewer="manj")
        if e.memory_id == mid
    ]
    ok = (
        len(read) == 1
        and read == ctx
        and read[0].content == "Prefers window seats"
        and read[0].source == "msg-m1"
    )
    return ok, f"{mid}, source {read[0].source if read else '-'}"


def check_m2(engine: MemoryEngine) -> tuple[bool, str]:
    mid = engine.write(
        "manj",
        MemoryCategory.PREFERENCE,
        "Likes filter coffee",
        MemoryTag.STATED,
        "test",
        by="manj",
        domain="lifestyle",
    )
    engine.update(mid, "Likes strong filter coffee", "msg-m2", by="manj")
    versions = engine.history(mid, viewer="manj")
    old = engine.store.show(versions[0].commit, "preferences/lifestyle.md") if versions else None
    ok = (
        [v.entry.content for v in versions] == ["Likes filter coffee", "Likes strong filter coffee"]
        and versions[0].commit != versions[1].commit
        and old is not None
        and "Likes filter coffee" in old
    )
    return ok, f"{mid}, {len(versions)} versions"


def check_m3(engine: MemoryEngine) -> tuple[bool, str]:
    mid = engine.write(
        "manj",
        MemoryCategory.PREFERENCE,
        "Orders from the same cafe on Fridays",
        MemoryTag.INFERRED,
        "test",
        by="agent:atlas",
        domain="restaurants",
    )
    steps: list[bool] = []
    try:
        engine.assert_actionable([mid])
        steps.append(False)
    except NotActionable:
        steps.append(True)
    for who in ("agent:atlas", SYSTEM):
        try:
            engine.confirm(mid, by=who)
            steps.append(False)
        except MemoryRefused:
            steps.append(True)
    engine.confirm(mid, by="manj")
    try:
        engine.assert_actionable([mid])
        steps.append(True)
    except NotActionable:
        steps.append(False)
    return all(steps), f"{mid}, blocked until confirmed by manj"


def check_m4(engine: MemoryEngine, ids: dict[str, str]) -> tuple[bool, str]:
    got = {e.memory_id for e in engine.retrieve_context("daughter", "school trip", viewer="manj")}
    want = {ids[k] for k in M4_EXPECTED}
    return got == want, f"{len(got)} entries, expected {len(want)}"


def run() -> list[tuple[str, bool, str]]:
    """M1-M4 in a throwaway repo (deploy check)."""
    with tempfile.TemporaryDirectory(prefix="jarvis-memory-smoke-") as tmp:
        engine = MemoryEngine(
            Path(tmp) / "memory", StaticDirectory(), ListAudit(), HOUSEHOLD_ID
        ).open()
        add_test_family(engine)
        ids = seed_m4(engine)
        steps: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
            ("M1 write then retrieve", lambda: check_m1(engine)),
            ("M2 update keeps history", lambda: check_m2(engine)),
            ("M3 inferred blocked until confirmed", lambda: check_m3(engine)),
            ("M4 retrieve_context(daughter, school trip)", lambda: check_m4(engine, ids)),
        ]
        results = []
        for name, check in steps:
            try:
                ok, detail = check()
            except Exception as e:  # report, don't crash the deploy check
                ok, detail = False, f"{type(e).__name__}: {e}"
            results.append((name, ok, detail))
        return results
