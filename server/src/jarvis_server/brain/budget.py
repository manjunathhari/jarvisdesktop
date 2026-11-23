"""Per-turn budget reservations, safe under concurrency (spec §10.1; P2-B2 to P2-B5).

Amounts are integers in paise. The columns are named `*_inr` (spec §5.6) but hold paise, as the
Task 4 migration (0010) says; nothing here renames them.

"This month" and "today" are Asia/Kolkata dates. India has kept UTC+05:30 without daylight
saving since 1945, so a fixed offset is exact and needs no time-zone database in the image.

Lock order, everywhere: the turn's `conv.inbound` row, then `ops.brain_budget` rows (ascending
month), then `ops.brain_reservation` rows. Every change to a month's reservations or totals
holds that month's budget row lock, so the totals, today's spend and the second-job checks
are always read under the lock that guards them.

- `reserve`: locks the turn (if any), then the budget row of this month, and applies the
  second-job table of §10.1 step 6 before the cap and daily-ceiling checks of step 2.
- `settle` / `release`: only from ACTIVE (`ReservationStateError` otherwise).
- `lapse`: ACTIVE to LAPSED at its full reserved value, then the indeterminate path for its
  turn. `lapse_expired` (the sweep) does this for every ACTIVE reservation past its lease.
- The indeterminate path runs only while `conv.inbound.outcome` is empty, under the turn lock,
  so the sweep and a late job together notify exactly once.
- 80% and 100% of the monthly cap: one `budget.80` / `budget.100` event and notification per
  month; a refusal by today's ceiling alone: one `budget.daily` per IST day (R12-14). Decided
  under the budget row lock and always sent to the household owner
  (`config.OWNER_PERSON_ID`), whoever's turn or probe crossed the line (R12-16).
- Budget steps of a job that calls the brain commit on their own connection
  (`committing_connection`, R12-13), never in the job's transaction.
"""

from __future__ import annotations

import calendar
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus

from jarvis_server import comm, config, jobs
from jarvis_server.core import dispatch, rules

log = logging.getLogger("jarvis.brain.budget")

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # Asia/Kolkata

# Rs 5,000 (§10.1). Changed only here, then redeployed. A raise applies to the current month
# at the next reservation; a lower value never lowers the current month (R12-15).
MONTHLY_CAP_PAISE = 500_000
STEP_LOCK_TIMEOUT = "30s"  # a budget step on its own connection never waits for ever
WARN_PERCENT = 80

_LIVE = ("ACTIVE", "SETTLED", "LAPSED")


class Owner(StrEnum):
    RESERVED = "RESERVED"  # this job owns the turn and may call Claude
    OWNED_ELSEWHERE = "OWNED_ELSEWHERE"  # another job owns it, in its lease: change nothing
    ANSWERED = "ANSWERED"  # the turn's call was settled: change nothing
    INDETERMINATE = "INDETERMINATE"  # the turn's call lapsed: never call again
    NO_BUDGET = "NO_BUDGET"  # the cap or today's ceiling refuses it: nothing reserved


class ReservationStateError(RuntimeError):
    """settle or release of a reservation that is unknown or not ACTIVE."""


@dataclass(frozen=True)
class BudgetRow:
    """A month's `ops.brain_budget` row, plus what today's reservations count for."""

    month: date
    cap_inr: int
    settled_inr: int
    reserved_inr: int
    today_inr: int  # today's SETTLED actual + ACTIVE and LAPSED reserved, in this month


def _aware(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must carry a time zone")
    return value


def _paise(value: object, what: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise ValueError(f"{what} must be a {'positive' if positive else 'non-negative'} integer")
    return value


def ist_month_day(now: datetime) -> tuple[date, date]:
    """(the first of this month, today), both as Asia/Kolkata dates."""
    today = _aware(now, "now").astimezone(IST).date()
    return today.replace(day=1), today


def daily_ceiling(budget_row: BudgetRow, now_ist: datetime) -> int:
    """(cap - spent at the start of today) / days left in the month including today, rounded
    down and never negative. On the last day it is the whole remainder."""
    month, today = ist_month_day(now_ist)
    if month != budget_row.month:
        raise ValueError(f"{today} is not in the budget row's month {budget_row.month}")
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    spent_before_today = budget_row.settled_inr + budget_row.reserved_inr - budget_row.today_inr
    return max(0, (budget_row.cap_inr - spent_before_today) // days_left)


@contextlib.contextmanager
def committing_connection(conn: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """A connection whose budget steps commit on their own (R12-13).

    `conn` itself when it is an idle autocommit connection (each step is then a real
    transaction); otherwise, inside a job transaction, a short sibling connection to the same
    database as the same role, closed afterwards. A job that calls the brain runs `reserve`
    through this before the call and `settle` / `release` / `lapse` through it after, so the
    reservation is committed before any request byte is sent, and the turn and budget locks
    are held only for those short transactions, never during the call:

        with budget.committing_connection(conn) as c:
            owner, rid = budget.reserve(c, ...)
        ... call the brain ...
        with budget.committing_connection(conn) as c:
            budget.settle(c, rid, ...)

    The caller's own transaction must not hold the turn row or a budget row when it does this:
    the sibling would wait on its own job. `STEP_LOCK_TIMEOUT` turns such a mistake into an
    error instead of a hang.
    """
    if conn.autocommit and conn.info.transaction_status == TransactionStatus.IDLE:
        yield conn
        return
    password = conn.info.password
    conninfo = conn.info.dsn
    if password:
        conninfo = psycopg.conninfo.make_conninfo(conninfo, password=password)
    with psycopg.connect(conninfo, autocommit=True) as own:
        own.execute("SELECT set_config('lock_timeout', %s, false)", (STEP_LOCK_TIMEOUT,))
        yield own


# --- locked reads and writes (callers hold the locks named) ------------------------------------


def _lock_turn(conn: psycopg.Connection[Any], turn_id: UUID) -> tuple[UUID, str | None] | None:
    row = conn.execute(
        "SELECT person_id, outcome FROM conv.inbound WHERE id = %s FOR UPDATE", (turn_id,)
    ).fetchone()
    return None if row is None else (UUID(str(row[0])), row[1])


def _lock_budget(conn: psycopg.Connection[Any], month: date, today: date) -> BudgetRow:
    """Lock the month's row (created with the configured cap if missing), raise its cap to the
    configured one if that is higher (R12-15), and read it."""
    conn.execute(
        "INSERT INTO ops.brain_budget (month, cap_inr) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (month, MONTHLY_CAP_PAISE),
    )
    row = conn.execute(
        "SELECT cap_inr, settled_inr, reserved_inr FROM ops.brain_budget"
        " WHERE month = %s FOR UPDATE",
        (month,),
    ).fetchone()
    assert row is not None
    cap = int(row[0])
    if cap < MONTHLY_CAP_PAISE:
        cap = MONTHLY_CAP_PAISE
        conn.execute("UPDATE ops.brain_budget SET cap_inr = %s WHERE month = %s", (cap, month))
    spent = conn.execute(
        "SELECT coalesce(sum(CASE state WHEN 'SETTLED' THEN actual_inr ELSE reserved_inr END), 0)"
        " FROM ops.brain_reservation WHERE month = %s AND day = %s AND state = ANY(%s)",
        (month, today, list(_LIVE)),
    ).fetchone()
    assert spent is not None
    return BudgetRow(month, cap, int(row[1]), int(row[2]), int(spent[0]))


def _lock_month(conn: psycopg.Connection[Any], month: date) -> None:
    """Lock an existing month's row (a reservation's month always exists: foreign key)."""
    conn.execute("SELECT 1 FROM ops.brain_budget WHERE month = %s FOR UPDATE", (month,))


def _lapse_locked(conn: psycopg.Connection[Any], reservation_id: UUID) -> bool:
    """ACTIVE -> LAPSED, its full reserved amount counted as settled. Needs the month lock."""
    row = conn.execute(
        "SELECT month, reserved_inr, state FROM ops.brain_reservation WHERE id = %s FOR UPDATE",
        (reservation_id,),
    ).fetchone()
    if row is None or row[2] != "ACTIVE":
        return False
    conn.execute(
        "UPDATE ops.brain_reservation SET state = 'LAPSED' WHERE id = %s", (reservation_id,)
    )
    conn.execute(
        "UPDATE ops.brain_budget SET reserved_inr = reserved_inr - %s,"
        " settled_inr = settled_inr + %s WHERE month = %s",
        (row[1], row[1], row[0]),
    )
    return True


def _indeterminate(
    conn: psycopg.Connection[Any], turn_id: UUID, person_id: UUID, reservation_id: UUID
) -> bool:
    """The indeterminate path (§10.1 step 6), once per turn. Needs the turn lock."""
    marked = conn.execute(
        "UPDATE conv.inbound SET outcome = 'INDETERMINATE' WHERE id = %s AND outcome IS NULL",
        (turn_id,),
    )
    if marked.rowcount != 1:
        return False
    event_id = comm.record_event(
        conn,
        "turn.indeterminate",
        subject_type="inbound",
        subject_id=str(turn_id),
        person_id=person_id,
        details={"reservation_id": str(reservation_id)},
    )
    rules.notify(conn, "turn.indeterminate", person_id=person_id, event_id=event_id, context={})
    return True


def _threshold(conn: psycopg.Connection[Any], kind: str, subject: str) -> None:
    """One `budget.80` / `budget.100` event and notification per month (subject `YYYY-MM`),
    one `budget.daily` per IST day (subject `YYYY-MM-DD`), always to the household owner and
    to no one else (R12-16). Needs the month lock."""
    owner = config.OWNER_PERSON_ID
    seen = conn.execute(
        "SELECT 1 FROM comm.event WHERE kind = %s AND subject_type = 'budget'"
        " AND subject_id = %s LIMIT 1",
        (kind, subject),
    ).fetchone()
    if seen is not None:
        return
    event_id = comm.record_event(
        conn, kind, subject_type="budget", subject_id=subject, person_id=owner, details={}
    )
    rules.notify(conn, kind, person_id=owner, event_id=event_id, context={})


# --- the interface -----------------------------------------------------------------------------


def reserve(
    conn: psycopg.Connection[Any],
    *,
    job_id: int,
    turn_id: UUID | None,
    reserve_paise: int,
    lease_until: datetime,
    now: datetime,
) -> tuple[Owner, UUID | None]:
    """Reserve `reserve_paise` for one call, or say why not (§10.1 steps 2 and 6).

    Returns (RESERVED, the new reservation), (NO_BUDGET, None), or for a turn that already has
    a live reservation (OWNED_ELSEWHERE | ANSWERED | INDETERMINATE, that reservation). A unique
    violation on insert (a racing owner) is (OWNED_ELSEWHERE, None).
    """
    _paise(reserve_paise, "reserve_paise", positive=True)
    if _aware(lease_until, "lease_until") <= _aware(now, "now"):
        raise ValueError("lease_until must be after now")
    month, today = ist_month_day(now)
    with conn.transaction():
        person_id: UUID | None = None
        if turn_id is not None:
            turn = _lock_turn(conn, turn_id)
            if turn is None:
                raise LookupError(f"no turn {turn_id} in conv.inbound")
            person_id = turn[0]
            existing = conn.execute(
                "SELECT id, month, state, lease_until FROM ops.brain_reservation"
                " WHERE turn_id = %s AND state = ANY(%s)",
                (turn_id, list(_LIVE)),
            ).fetchone()
            if existing is not None:
                rid, r_month, first_state, r_lease = existing
                rid = UUID(str(rid))
                expired = r_lease < now  # the lease never changes
                # This month's row, and the reservation's own month if it may be lapsed, in
                # ascending month order.
                lapsable = first_state == "ACTIVE" and expired
                for m in sorted({month, r_month} if lapsable else {month}):
                    if m == month:
                        _lock_budget(conn, m, today)
                    else:
                        _lock_month(conn, m)
                # Re-read under the reservation's row lock: settle and release take no turn
                # lock, so the owner may have finished while this call waited for the budget.
                locked = conn.execute(
                    "SELECT state FROM ops.brain_reservation WHERE id = %s FOR UPDATE", (rid,)
                ).fetchone()
                assert locked is not None
                state = locked[0]
                if state == "SETTLED":
                    return Owner.ANSWERED, rid
                if state == "LAPSED":  # lapsed earlier, with its indeterminate path
                    return Owner.INDETERMINATE, rid
                if state == "ACTIVE":
                    if not expired:
                        return Owner.OWNED_ELSEWHERE, rid
                    if _lapse_locked(conn, rid):
                        _indeterminate(conn, turn_id, person_id, rid)
                    return Owner.INDETERMINATE, rid
                # RELEASED while this call waited: the turn is free, reserve afresh below.

        row = _lock_budget(conn, month, today)
        used = row.settled_inr + row.reserved_inr
        over_cap = used + reserve_paise > row.cap_inr
        over_day = row.today_inr + reserve_paise > daily_ceiling(row, now)
        new_id: UUID | None = None
        if not (over_cap or over_day):
            try:
                with conn.transaction():  # a savepoint: a unique violation undoes only this
                    inserted = conn.execute(
                        "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day,"
                        " reserved_inr, lease_until) VALUES (%s, %s, %s, %s, %s, %s)"
                        " RETURNING id",
                        (job_id, turn_id, month, today, reserve_paise, lease_until),
                    ).fetchone()
                    assert inserted is not None
                    conn.execute(
                        "UPDATE ops.brain_budget SET reserved_inr = reserved_inr + %s"
                        " WHERE month = %s",
                        (reserve_paise, month),
                    )
            except errors.UniqueViolation:
                return Owner.OWNED_ELSEWHERE, None
            new_id = UUID(str(inserted[0]))
            used += reserve_paise
        this_month = month.strftime("%Y-%m")
        if used * 100 >= row.cap_inr * WARN_PERCENT:
            _threshold(conn, "budget.80", this_month)
        if over_cap or used >= row.cap_inr:
            _threshold(conn, "budget.100", this_month)
        elif over_day:  # refused by today's ceiling alone (R12-14)
            _threshold(conn, "budget.daily", today.isoformat())
        if new_id is None:
            return Owner.NO_BUDGET, None
        return Owner.RESERVED, new_id


def _locked_active(conn: psycopg.Connection[Any], reservation_id: UUID) -> tuple[date, int]:
    """Lock the reservation's month, then the reservation; (month, reserved) if ACTIVE."""
    row = conn.execute(
        "SELECT month FROM ops.brain_reservation WHERE id = %s", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ReservationStateError(f"no reservation {reservation_id}")
    _lock_month(conn, row[0])
    locked = conn.execute(
        "SELECT state, reserved_inr FROM ops.brain_reservation WHERE id = %s FOR UPDATE",
        (reservation_id,),
    ).fetchone()
    assert locked is not None
    if locked[0] != "ACTIVE":
        raise ReservationStateError(f"reservation {reservation_id} is {locked[0]}, not ACTIVE")
    return row[0], int(locked[1])


def settle(
    conn: psycopg.Connection[Any],
    reservation_id: UUID,
    *,
    actual_paise: int,
    usage: tuple[int, int],
    model: str,
) -> None:
    """ACTIVE -> SETTLED with the actual cost, moved from reserved to settled, and the usage
    row written (§10.1 step 3). The actual cost is what is counted, even above the reservation."""
    _paise(actual_paise, "actual_paise")
    input_tokens, output_tokens = usage
    _paise(input_tokens, "input tokens")
    _paise(output_tokens, "output tokens")
    with conn.transaction():
        month, reserved = _locked_active(conn, reservation_id)
        conn.execute(
            "UPDATE ops.brain_reservation SET state = 'SETTLED', actual_inr = %s WHERE id = %s",
            (actual_paise, reservation_id),
        )
        conn.execute(
            "UPDATE ops.brain_budget SET reserved_inr = reserved_inr - %s,"
            " settled_inr = settled_inr + %s WHERE month = %s",
            (reserved, actual_paise, month),
        )
        conn.execute(
            "INSERT INTO ops.brain_usage (reservation_id, model, input_tokens, output_tokens,"
            " cost_inr) VALUES (%s, %s, %s, %s, %s)",
            (reservation_id, model, input_tokens, output_tokens, actual_paise),
        )


def release(conn: psycopg.Connection[Any], reservation_id: UUID) -> None:
    """ACTIVE -> RELEASED, the amount given back. Only for a call that never reached Claude
    (the connect phase, §10.1 step 4)."""
    with conn.transaction():
        month, reserved = _locked_active(conn, reservation_id)
        conn.execute(
            "UPDATE ops.brain_reservation SET state = 'RELEASED' WHERE id = %s",
            (reservation_id,),
        )
        conn.execute(
            "UPDATE ops.brain_budget SET reserved_inr = reserved_inr - %s WHERE month = %s",
            (reserved, month),
        )


def lapse(conn: psycopg.Connection[Any], reservation_id: UUID) -> bool:
    """Lapse an ACTIVE reservation at its full value and run the indeterminate path for its
    turn, in one transaction (§10.1 steps 4 to 6). True if this call lapsed it; False if it was
    no longer ACTIVE (already lapsed, settled or released), and then nothing changes."""
    with conn.transaction():
        row = conn.execute(
            "SELECT turn_id, month FROM ops.brain_reservation WHERE id = %s", (reservation_id,)
        ).fetchone()
        if row is None:
            raise ReservationStateError(f"no reservation {reservation_id}")
        turn_id = None if row[0] is None else UUID(str(row[0]))
        turn = None if turn_id is None else _lock_turn(conn, turn_id)
        _lock_month(conn, row[1])
        if not _lapse_locked(conn, reservation_id):
            return False
        if turn_id is not None and turn is not None:  # a pruned turn has no one to tell
            _indeterminate(conn, turn_id, turn[0], reservation_id)
        return True


def lapse_expired(conn: psycopg.Connection[Any], now: datetime) -> int:
    """The sweep (§10.1 step 5): lapse every ACTIVE reservation past its lease, each in its own
    committed transaction (through `committing_connection`), so no lock outlives one lapse and
    the order turn, then budget holds even when the sweep runs inside a job transaction.
    Returns how many this call lapsed."""
    expired = conn.execute(
        "SELECT id FROM ops.brain_reservation WHERE state = 'ACTIVE' AND lease_until < %s"
        " ORDER BY lease_until, id",
        (_aware(now, "now"),),
    ).fetchall()
    with committing_connection(conn) as own:
        return sum(1 for (rid,) in expired if lapse(own, UUID(str(rid))))


@dispatch.register("brain.reservation_sweep")
def _sweep(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    lapsed = lapse_expired(conn, row[0])
    if lapsed:
        log.info("reservation sweep (job %s) lapsed %d reservation(s)", job.id, lapsed)
