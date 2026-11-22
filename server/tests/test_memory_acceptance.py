"""Memory acceptance tests M1-M4 against the real database, plus migration 0007 and S5.

Fictional people only. Runs in its own database so the audit rows it writes cannot affect the
Phase 0 migration round trip (P0-6).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jarvis_server import migrate
from jarvis_server.memory import checks
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.memory.errors import MemoryRefused
from jarvis_server.memory.pg import MEMORY_NAMESPACE, PgAudit, PgDirectory, memory_entity_id

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]


@pytest.fixture(scope="module")
def mdb() -> Iterator[Dsns]:
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


@pytest.fixture
def app(mdb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(mdb.app, autocommit=True) as conn:
        yield conn


@pytest.fixture
def since(app: Conn) -> int:
    """Audit position at the start of the test: memory IDs repeat across test repos."""
    row = app.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def eng(tmp_path: Path, app: Conn, since: int) -> MemoryEngine:
    e = MemoryEngine(tmp_path / "memory", PgDirectory(app), PgAudit(app), checks.HOUSEHOLD_ID)
    e.open()
    checks.add_test_family(e)
    return e


def memory_rows(app: Conn, ref: str, since: int) -> list[tuple[Any, ...]]:
    return app.execute(
        "SELECT action::text, from_state, to_state, outcome::text, actor_kind::text, details"
        " FROM audit.event WHERE entity_type = 'memory' AND entity_id = %s AND seq > %s"
        " ORDER BY seq",
        (memory_entity_id(ref), since),
    ).fetchall()


# --- M1-M4 ---------------------------------------------------------------------------------


def test_m1_write_then_retrieve(eng: MemoryEngine) -> None:
    ok, detail = checks.check_m1(eng)
    assert ok, detail


def test_m2_update_keeps_history(eng: MemoryEngine) -> None:
    ok, detail = checks.check_m2(eng)
    assert ok, detail


def test_m3_inferred_blocked_until_confirmed(eng: MemoryEngine, app: Conn, since: int) -> None:
    ok, detail = checks.check_m3(eng)
    assert ok, detail
    mid = detail.split(",")[0]
    rows = memory_rows(app, mid, since)
    assert [(r[0], r[3], r[4]) for r in rows] == [
        ("CREATE", "APPLIED", "AGENT"),
        ("CONFIRM", "REJECTED", "AGENT"),
        ("CONFIRM", "REJECTED", "SYSTEM"),
        ("CONFIRM", "APPLIED", "PERSON"),
    ]


def test_m4_retrieve_context_daughter_school_trip(eng: MemoryEngine) -> None:
    ids = checks.seed_m4(eng)
    got = {e.memory_id for e in eng.retrieve_context("daughter", "school trip", viewer="manj")}
    assert got == {ids[k] for k in checks.M4_EXPECTED}
    excluded = {
        "restaurant": "not relevant",
        "private": "not permitted",
        "old_school": "expired",
        "manj_flights": "different subject",
    }
    for name, why in excluded.items():
        assert ids[name] not in got, f"{name} should be excluded ({why})"
    # The daughter herself sees her PRIVATE entry; the neighbour sees nothing of hers.
    own = {e.memory_id for e in eng.retrieve_context("daughter", "school trip", viewer="daughter")}
    assert ids["private"] in own
    assert eng.retrieve_context("daughter", "school trip", viewer="neighbour") == []


# --- audit rows (spec §9) ------------------------------------------------------------------


def test_every_change_has_one_audit_row_and_no_content(
    eng: MemoryEngine, app: Conn, since: int
) -> None:
    mid = eng.write(
        "manj",
        checks.MemoryCategory.PREFERENCE,
        "Unique-marker-7781 coffee",
        checks.MemoryTag.STATED,
        "test",
        by="manj",
        domain="lifestyle",
    )
    eng.update(mid, "Unique-marker-7781 tea", "test", by="manj")
    eng.archive(mid, by="manj", reason="no-longer-true")
    rows = memory_rows(app, mid, since)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("CREATE", None, "STATED"),
        ("UPDATE", "STATED", "STATED"),
        ("ARCHIVE", "STATED", "EXPIRED"),
    ]
    assert all(r[5]["memory_id"] == mid and len(r[5]["commit"]) == 40 for r in rows)
    leaked = app.execute(
        "SELECT count(*) FROM audit.event WHERE (details::text || reason) LIKE '%Unique-marker%'"
    ).fetchone()
    assert leaked == (0,)


def test_refusal_is_audited_in_postgres(eng: MemoryEngine, app: Conn) -> None:
    before = app.execute(
        "SELECT count(*) FROM audit.event WHERE entity_id = %s", (memory_entity_id("m-unassigned"),)
    ).fetchone()
    with pytest.raises(MemoryRefused):
        eng.write(
            "ghost", checks.MemoryCategory.IDENTITY, "x", checks.MemoryTag.STATED, "test", by="manj"
        )
    after = app.execute(
        "SELECT count(*), max(outcome::text) FROM audit.event WHERE entity_id = %s",
        (memory_entity_id("m-unassigned"),),
    ).fetchone()
    assert before is not None and after is not None
    assert after[0] == before[0] + 1 and after[1] == "REJECTED"


def test_startup_reconciles_against_postgres(eng: MemoryEngine, app: Conn, since: int) -> None:
    head_before = eng.store.head()
    # A commit that never reached the audit trail (process died in between).
    sha = eng.store.commit(
        {"index.md": (eng.store.root / "index.md").read_text() + "\n"},
        "memory: create m-0999 (preference)\n\nAction: CREATE\nMemory-Ref: m-0999\n"
        "From:\nTo: STATED\nActor: manj\n",
    )
    assert sha != head_before
    MemoryEngine(eng.store.root, PgDirectory(app), PgAudit(app), checks.HOUSEHOLD_ID).open()
    rows = memory_rows(app, "m-0999", since)
    assert len(rows) == 1 and rows[0][4] == "SYSTEM" and rows[0][5]["commit"] == sha


def test_entity_id_matches_between_python_and_sql(app: Conn) -> None:
    for ref in ("m-0001", "m-0042", "m-unassigned", "person:daughter"):
        row = app.execute("SELECT audit.memory_entity_id(%s)", (ref,)).fetchone()
        assert row == (uuid.uuid3(MEMORY_NAMESPACE, ref),)


@pytest.mark.parametrize(
    "args",
    [
        ("SYSTEM", "t", "Likes tea", "CREATE", None, "STATED", "APPLIED", "x", "a" * 40),
        ("SYSTEM", "t", "m-0001", "TRANSITION", None, "STATED", "APPLIED", "x", "a" * 40),
        ("SYSTEM", "t", "m-0001", "CREATE", None, "MAYBE", "APPLIED", "x", "a" * 40),
        ("SYSTEM", "t", "m-0001", "CREATE", None, "STATED", "APPLIED", "x", None),
        ("SYSTEM", "t", "m-0001", "CREATE", None, None, "REJECTED", "x", "a" * 40),
        ("SYSTEM", "t", "m-0001", "CREATE", None, "STATED", "APPLIED", "x", "not-a-sha"),
    ],
)
def test_record_memory_refuses_bad_arguments(app: Conn, args: tuple[Any, ...]) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app.execute("SELECT audit.record_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s)", args)


def test_app_cannot_write_audit_rows_directly(app: Conn) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id, action,"
            " outcome, reason) VALUES ('SYSTEM', 'x', 'memory', gen_random_uuid(), 'CREATE',"
            " 'APPLIED', 'forged')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            "SELECT audit.append('SYSTEM', 'x', 'memory', gen_random_uuid(), 'CREATE', NULL,"
            " NULL, 'APPLIED', 'forged', NULL)"
        )


# --- migration 0007 and S5 -----------------------------------------------------------------


def test_0007_downgrade_refuses_once_memory_rows_exist(mdb: Dsns, eng: MemoryEngine) -> None:
    with pytest.raises(Exception, match="memory audit rows exist"):
        migrate.downgrade(mdb.owner, "0006")


def test_0007_goes_down_and_up_on_a_database_without_memory_rows() -> None:
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0006")
        with psycopg.connect(d.owner) as conn:
            labels = conn.execute("SELECT unnest(enum_range(NULL::audit.action))::text").fetchall()
            assert [r[0] for r in labels] == ["CREATE", "TRANSITION"]
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.app) as conn:
            assert conn.execute("SELECT audit.memory_entity_id('m-0001')").fetchone() is not None


def test_s5_app_cannot_connect_to_the_default_database(mdb: Dsns) -> None:
    dsn = psycopg.conninfo.make_conninfo(mdb.app, dbname="postgres")
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(dsn).close()


# --- findings from the independent review (2026-11-22) -------------------------------------


def test_q4_malformed_ids_are_refused_and_audited(eng: MemoryEngine, app: Conn, since: int) -> None:
    with pytest.raises(MemoryRefused, match="no-such-entry"):
        eng.confirm("bogus", by="manj")
    with pytest.raises(MemoryRefused, match="bad-subject-name"):
        eng.add_person("Bad Slug", uuid.uuid4(), "x", by="system")
    rows = memory_rows(app, "m-unassigned", since)
    assert [(r[0], r[3]) for r in rows] == [("CONFIRM", "REJECTED"), ("CREATE", "REJECTED")]


@pytest.mark.parametrize(
    "args",
    [
        # free text as reason, actor or illegal tag change: all refused by the database
        (
            "SYSTEM",
            "system",
            "m-0001",
            "ARCHIVE",
            "STATED",
            "EXPIRED",
            "APPLIED",
            "Her diary was about Rahul",
            "a" * 40,
        ),
        (
            "PERSON",
            "Rahul from school",
            "m-0001",
            "CREATE",
            None,
            "STATED",
            "APPLIED",
            "create",
            "a" * 40,
        ),
        (
            "SYSTEM",
            "system",
            "m-0001",
            "CONFIRM",
            "HISTORICAL",
            "CONFIRMED",
            "APPLIED",
            "confirm",
            "a" * 40,
        ),
        ("SYSTEM", "system", "m-0001", "CREATE", None, "CONFIRMED", "APPLIED", "create", "a" * 40),
        (
            "SYSTEM",
            "system",
            "m-0001",
            "UPDATE",
            "STATED",
            "CONFIRMED",
            "APPLIED",
            "update",
            "a" * 40,
        ),
        (
            "SYSTEM",
            "system",
            "m-0001",
            "UPDATE",
            "EXPIRED",
            "EXPIRED",
            "APPLIED",
            "update",
            "a" * 40,
        ),
        (
            "SYSTEM",
            "system",
            "m-0001",
            "ARCHIVE",
            "EXPIRED",
            "EXPIRED",
            "APPLIED",
            "archive",
            "a" * 40,
        ),
        ("SYSTEM", "system", "m-0001", "CONFIRM", "STATED", None, "REJECTED", "x", None),
        ("AGENT", "Atlas Bot", "m-0001", "CREATE", None, "INFERRED", "APPLIED", "create", "a" * 40),
    ],
)
def test_s3_record_memory_refuses_free_text_and_illegal_changes(
    app: Conn, args: tuple[Any, ...]
) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app.execute("SELECT audit.record_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s)", args)


def test_s4_app_cannot_connect_to_template1(mdb: Dsns) -> None:
    dsn = psycopg.conninfo.make_conninfo(mdb.app, dbname="template1")
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(dsn).close()
