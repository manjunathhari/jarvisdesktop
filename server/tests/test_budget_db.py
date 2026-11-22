"""Brain budget tables (migration 0010; spec §5.6). Tables only: reserve, settle and lapse come
in a later task.

Runs in its own database: budget rows block the 0010 downgrade, so they must never reach the
session database that P0-6 takes down and up. Amounts are integers in paise.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from jarvis_server.bootstrap import APP_ROLE

from .conftest import Dsns, fresh_database

Conn = psycopg.Connection[Any]

MONTH = "2026-11-01"
DAY = "2026-11-22"


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    """Overrides the session database for this module (and its app/gateway/owner fixtures)."""
    with fresh_database() as d:
        with psycopg.connect(d.app, autocommit=True) as app:
            app.execute(
                "INSERT INTO ops.brain_budget (month, cap_inr) VALUES (%s, 500000)", (MONTH,)
            )
        yield d


def _one(conn: Conn, sql: Any, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _job(app: Conn) -> int:
    return int(_one(app, "INSERT INTO ops.job (kind) VALUES ('conv.turn') RETURNING id"))


def _reserve(
    app: Conn,
    turn: uuid.UUID | None,
    *,
    state: str = "ACTIVE",
    reserved: int = 2500,
    actual: int | None = None,
    day: str = DAY,
) -> uuid.UUID:
    return uuid.UUID(
        str(
            _one(
                app,
                "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day, reserved_inr,"
                " state, actual_inr, lease_until) VALUES (%s, %s, %s, %s, %s, %s, %s,"
                " now() + interval '5 minutes') RETURNING id",
                (_job(app), turn, MONTH, day, reserved, state, actual),
            )
        )
    )


def test_budget_row_defaults(app: Conn) -> None:
    row = app.execute(
        "SELECT cap_inr, settled_inr, reserved_inr FROM ops.brain_budget WHERE month = %s",
        (MONTH,),
    ).fetchone()
    assert row == (500000, 0, 0)


def test_one_budget_row_per_month(app: Conn) -> None:
    with pytest.raises(psycopg.errors.UniqueViolation):
        app.execute("INSERT INTO ops.brain_budget (month, cap_inr) VALUES (%s, 1)", (MONTH,))


def test_budget_month_is_first_of_month(app: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        app.execute("INSERT INTO ops.brain_budget (month, cap_inr) VALUES ('2026-12-02', 1)")


@pytest.mark.parametrize("column", ["cap_inr", "settled_inr", "reserved_inr"])
def test_budget_amounts_non_negative(app: Conn, column: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        app.execute(
            f"UPDATE ops.brain_budget SET {column} = -1 WHERE month = %s".encode(), (MONTH,)
        )


def test_reservation_unique_per_turn_except_released(app: Conn) -> None:
    turn = uuid.uuid4()
    first = _reserve(app, turn)
    with pytest.raises(psycopg.errors.UniqueViolation):  # a second ACTIVE for the same turn
        _reserve(app, turn)
    app.execute("UPDATE ops.brain_reservation SET state = 'RELEASED' WHERE id = %s", (first,))
    second = _reserve(app, turn)  # RELEASED frees the turn
    assert second != first
    # SETTLED and LAPSED still hold the turn.
    for state, actual in (("SETTLED", 2000), ("LAPSED", None)):
        app.execute(
            "UPDATE ops.brain_reservation SET state = %s, actual_inr = %s WHERE id = %s",
            (state, actual, second),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            _reserve(app, turn)
    # Several released reservations for one turn are fine.
    other = uuid.uuid4()
    for _ in range(2):
        _reserve(app, other, state="RELEASED")


def test_probe_reservations_have_no_turn(app: Conn) -> None:
    a, b = _reserve(app, None), _reserve(app, None)  # probes: turn_id null, never unique
    assert a != b


def test_reservation_state_and_amount_checks(app: Conn) -> None:
    for kw in (
        {"state": "DONE"},
        {"reserved": -1},
        {"state": "SETTLED", "actual": -1},
        {"state": "SETTLED", "actual": None},  # a settled reservation has its actual cost
        {"state": "ACTIVE", "actual": 10},  # an active one does not yet
        {"day": "2026-12-01"},  # the day is in the reservation's month
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            _reserve(app, uuid.uuid4(), **kw)  # type: ignore[arg-type]


def test_reservation_month_must_have_a_budget(app: Conn) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app.execute(
            "INSERT INTO ops.brain_reservation (job_id, month, day, reserved_inr, lease_until)"
            " VALUES (%s, '2027-01-01', '2027-01-05', 1, now())",
            (_job(app),),
        )


def test_usage_rows(app: Conn) -> None:
    reservation = _reserve(app, uuid.uuid4())
    app.execute(
        "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens, output_tokens,"
        " cost_inr) VALUES (%s, 'claude-test', 1200, 300, 2100)",
        (reservation,),
    )
    row = app.execute(
        "SELECT model, input_tokens, output_tokens, cost_inr, at IS NOT NULL"
        " FROM ops.brain_usage WHERE reservation_id = %s",
        (reservation,),
    ).fetchone()
    assert row == ("claude-test", 1200, 300, 2100, True)
    for values in ((-1, 1, 1), (1, -1, 1), (1, 1, -1)):
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens,"
                " output_tokens, cost_inr) VALUES (%s, 'claude-test', %s, %s, %s)",
                (reservation, *values),
            )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app.execute(
            "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens, output_tokens,"
            " cost_inr) VALUES (gen_random_uuid(), 'claude-test', 1, 1, 1)"
        )


def test_no_budget_functions_yet(owner: Conn) -> None:
    """Tables only: the reservation logic is a later task."""
    rows = owner.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'ops' AND p.proname LIKE '%%brain%%'"
    ).fetchall()
    assert rows == []


def test_app_privileges_on_budget_tables(owner: Conn) -> None:
    expected = {
        "ops.brain_budget": {"SELECT", "INSERT", "UPDATE"},
        "ops.brain_reservation": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "ops.brain_usage": {"SELECT", "INSERT", "DELETE"},
    }
    for table, privileges in expected.items():
        granted = {
            p
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
            if _one(owner, "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, p))
        }
        assert granted == privileges, table


def test_gateway_has_no_access(gateway: Conn) -> None:
    for table in ("ops.brain_budget", "ops.brain_reservation", "ops.brain_usage"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(f"SELECT * FROM {table}".encode())
