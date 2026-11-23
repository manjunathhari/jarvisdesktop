"""Per-turn budget reservations (spec §10.1; P2-B2, P2-B3, the P2-B5 tail; R12-11, R12-12).

Runs in its own database: budget, event and outbox rows block the 0009/0010 downgrades, so they
must never reach the session database that P0-6 takes down and up. Each test uses its own
budget month, so tests never share a budget row. Amounts are integers in paise. People are
fictional.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import jobs
from jarvis_server.brain import budget, prices
from jarvis_server.brain.budget import BudgetRow, Owner
from jarvis_server.core import dispatch

from .conftest import Dsns, fresh_database
from .helpers.threads import InThread, run_all

Conn = psycopg.Connection[Any]
Turns = Callable[[], uuid.UUID]

HOUSEHOLD = uuid.UUID("00000000-0000-4000-8000-000000000001")
IST_OFFSET = timedelta(hours=5, minutes=30)


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _no_active_reservations(owner: Conn) -> None:
    """Every test starts with no ACTIVE reservation, so a sweep counts only its own."""
    owner.execute("UPDATE ops.brain_reservation SET state = 'RELEASED' WHERE state = 'ACTIVE'")


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    pid = uuid.uuid4()
    owner.execute(
        "INSERT INTO ledger.person (id, display_name, organization_id, source_ref)"
        " VALUES (%s, 'Asha Fictional', %s, 'test')",
        (pid, HOUSEHOLD),
    )
    return pid


@pytest.fixture
def node(owner: Conn, person: uuid.UUID) -> uuid.UUID:
    row = owner.execute(
        "INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)"
        " VALUES (%s, 'DESKTOP', 'pc', %s, %s) RETURNING id",
        (person, secrets.token_bytes(32), f"ts:pc-{secrets.token_hex(4)}"),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


_months = iter(range(1, 10_000))


def _month() -> date:
    """A month no other test uses (from January 2040 on)."""
    n = next(_months)
    return date(2040 + n // 12, n % 12 + 1, 1)


def _next(month: date) -> date:
    return month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)


def _utc(day: date, hour: int = 12) -> datetime:
    """A UTC instant that is `hour`:00 IST on `day`."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC) - IST_OFFSET


@pytest.fixture
def new_turn(owner: Conn, person: uuid.UUID, node: uuid.UUID) -> Callable[[], uuid.UUID]:
    """Make a turn (a conv.inbound utterance) of `person`. Written as the owner: the app role
    only reads inbound rows, which the gateway records."""

    def make() -> uuid.UUID:
        turn = uuid.uuid4()
        owner.execute(
            "INSERT INTO conv.inbound (id, node_id, person_id, kind, payload)"
            " VALUES (%s, %s, %s, 'utterance', '{}')",
            (turn, node, person),
        )
        return turn

    return make


def _job(app: Conn) -> int:
    return jobs.enqueue(app, "conv.turn")


def _budget(
    app: Conn, month: date, cap: int, monkeypatch: pytest.MonkeyPatch | None = None
) -> None:
    """The month's row with `cap`; with `monkeypatch`, `cap` is also the configured cap, so
    reserve does not raise it (R12-15)."""
    app.execute("INSERT INTO ops.brain_budget (month, cap_inr) VALUES (%s, %s)", (month, cap))
    if monkeypatch is not None:
        monkeypatch.setattr(budget, "MONTHLY_CAP_PAISE", cap)


def _totals(app: Conn, month: date) -> tuple[int, int, int]:
    row = app.execute(
        "SELECT cap_inr, settled_inr, reserved_inr FROM ops.brain_budget WHERE month = %s",
        (month,),
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def _state(app: Conn, rid: uuid.UUID) -> tuple[str, int | None]:
    row = app.execute(
        "SELECT state, actual_inr FROM ops.brain_reservation WHERE id = %s", (rid,)
    ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def _outcome(app: Conn, turn: uuid.UUID) -> str | None:
    row = app.execute("SELECT outcome FROM conv.inbound WHERE id = %s", (turn,)).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


def _items(app: Conn, person: uuid.UUID, kind: str) -> list[tuple[str, str]]:
    rows = app.execute(
        "SELECT priority, state FROM comm.outbox WHERE person_id = %s AND kind = %s",
        (person, kind),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _events(app: Conn, kind: str, subject_id: str) -> int:
    row = app.execute(
        "SELECT count(*) FROM comm.event WHERE kind = %s AND subject_id = %s", (kind, subject_id)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _reserve(
    app: Conn, turn: uuid.UUID | None, now: datetime, paise: int = 1000, lease: float = 5
) -> tuple[Owner, uuid.UUID | None]:
    return budget.reserve(
        app,
        job_id=_job(app),
        turn_id=turn,
        reserve_paise=paise,
        lease_until=now + timedelta(minutes=lease),
        now=now,
    )


# --- IST day and month, the daily ceiling ------------------------------------------------------


def test_ist_day_and_month_boundary() -> None:  # [Review Focus 1]
    now = datetime(2026, 11, 30, 18, 40, tzinfo=UTC)  # 00:10 IST on 1 December
    assert budget.ist_month_day(now) == (date(2026, 12, 1), date(2026, 12, 1))
    before = datetime(2026, 11, 30, 18, 20, tzinfo=UTC)  # 23:50 IST on 30 November
    assert budget.ist_month_day(before) == (date(2026, 11, 1), date(2026, 11, 30))
    with pytest.raises(ValueError, match="time zone"):
        budget.ist_month_day(datetime(2026, 11, 30, 18, 40))


def test_daily_ceiling_pure() -> None:
    row = BudgetRow(month=date(2026, 11, 1), cap_inr=5000, settled_inr=2000, reserved_inr=0,
                    today_inr=0)  # fmt: skip
    # Remaining 3000 at the start of 21 November; 10 days left including today.
    assert budget.daily_ceiling(row, datetime(2026, 11, 21, 9, tzinfo=UTC)) == 300
    # Spending today does not change today's ceiling: it is fixed at the start of the day.
    spent = BudgetRow(month=date(2026, 11, 1), cap_inr=5000, settled_inr=2100, reserved_inr=50,
                      today_inr=150)  # fmt: skip
    assert budget.daily_ceiling(spent, datetime(2026, 11, 21, 9, tzinfo=UTC)) == 300


def test_last_day_ceiling_is_whole_remainder() -> None:  # [Review Focus 1]
    row = BudgetRow(month=date(2026, 11, 1), cap_inr=500000, settled_inr=123457, reserved_inr=0,
                    today_inr=0)  # fmt: skip
    last = datetime(2026, 11, 30, 18, 20, tzinfo=UTC)  # 23:50 IST, 30 November
    assert budget.daily_ceiling(row, last) == 500000 - 123457
    # 00:10 IST on 1 December is a new month: a different budget row, not this one's last day.
    with pytest.raises(ValueError, match="month"):
        budget.daily_ceiling(row, datetime(2026, 11, 30, 18, 40, tzinfo=UTC))
    over = BudgetRow(month=date(2026, 11, 1), cap_inr=100, settled_inr=150, reserved_inr=0,
                     today_inr=0)  # fmt: skip
    assert budget.daily_ceiling(over, last) == 0  # never negative


def test_daily_ceiling(app: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    month = _month()
    _budget(app, month, 3000, monkeypatch)
    now = _utc(_next(month) - timedelta(days=10))  # 10 days left including today
    assert _reserve(app, None, now, paise=301) == (Owner.NO_BUDGET, None)
    got, rid = _reserve(app, None, now, paise=300)
    assert got is Owner.RESERVED and rid is not None
    assert _reserve(app, None, now, paise=1) == (Owner.NO_BUDGET, None)  # today is used up
    assert _totals(app, month) == (3000, 0, 300)
    # Tomorrow's ceiling: (3000 - 300) / 9 days = 300.
    tomorrow = now + timedelta(days=1)
    assert _reserve(app, None, tomorrow, paise=301)[0] is Owner.NO_BUDGET
    assert _reserve(app, None, tomorrow, paise=300)[0] is Owner.RESERVED


# --- prices (R12-12) ---------------------------------------------------------------------------


def test_unknown_model_price_fails_closed() -> None:  # [Review Focus 2]
    with pytest.raises(prices.UnknownModelPrice):
        prices.cost_paise("nope", 10, 10)
    with pytest.raises(prices.UnknownModelPrice):
        prices.cost_paise("claude-haiku-5-5", 10, 10)  # tiered prices: left out on purpose


def test_price_table_and_rounding() -> None:
    assert prices.default_model() == "claude-opus-5-5"
    # 40 000 / 200 000 paise per million tokens.
    assert prices.cost_paise("claude-opus-5-5", 1_000_000, 0) == 40_000
    assert prices.cost_paise("claude-opus-5-5", 0, 1_000_000) == 200_000
    assert prices.cost_paise("claude-sonnet-5-5", 1_000_000, 1_000_000) == 120_000
    assert prices.cost_paise("claude-opus-5-5", 0, 0) == 0
    # Rounds up to the next paisa: 1 input token is 0.04 paise -> 1 paisa.
    assert prices.cost_paise("claude-opus-5-5", 1, 0) == 1
    assert prices.cost_paise("claude-opus-5-5", 25, 0) == 1  # exactly 1.0
    assert prices.cost_paise("claude-opus-5-5", 26, 0) == 2  # 1.04
    assert prices.cost_paise("claude-opus-5-5", 1, 1) == 1  # 0.04 + 0.2, ceiled once
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError):
            prices.cost_paise("claude-opus-5-5", bad, 0)  # type: ignore[arg-type]


# --- reserve: concurrency, lock order, the second-job table ------------------------------------


def test_twenty_concurrent_reservations_three_fit(
    dsns: Dsns, app: Conn, new_turn: Turns, monkeypatch: pytest.MonkeyPatch
) -> None:  # P2-B3
    month = _month()
    _budget(app, month, 3000, monkeypatch)
    now = _utc(_next(month) - timedelta(days=1))  # the last day: the ceiling is the remainder
    turns = [new_turn() for _ in range(20)]
    job_ids = [_job(app) for _ in range(20)]
    barrier = threading.Barrier(20, timeout=30)
    results: list[tuple[Owner, uuid.UUID | None]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        with psycopg.connect(dsns.app, autocommit=True) as conn:
            barrier.wait()
            got = budget.reserve(
                conn, job_id=job_ids[i], turn_id=turns[i], reserve_paise=1000,
                lease_until=now + timedelta(minutes=5), now=now,
            )  # fmt: skip
        with lock:
            results.append(got)

    run_all([lambda i=i: worker(i) for i in range(20)], timeout=60)
    granted = [r for r in results if r[0] is Owner.RESERVED]
    assert len(results) == 20
    assert len(granted) == 3
    assert all(r == (Owner.NO_BUDGET, None) for r in results if r[0] is not Owner.RESERVED)
    assert _totals(app, month) == (3000, 0, 3000)
    row = app.execute(
        "SELECT count(*) FROM ops.brain_reservation WHERE month = %s AND state = 'ACTIVE'",
        (month,),
    ).fetchone()
    assert row == (3,)


def test_reserve_locks_turn_then_budget(
    dsns: Dsns, app: Conn, person: uuid.UUID, new_turn: Turns
) -> None:
    month = _month()
    now = _utc(month)
    turn = new_turn()
    first, rid = _reserve(app, turn, now)
    assert first is Owner.RESERVED
    # Lock order: while another transaction holds the turn row, reserve waits on it and has
    # not yet touched the budget row (another session can still lock that at once).
    job = _job(app)
    with psycopg.connect(dsns.app) as holder:
        holder.execute("SELECT 1 FROM conv.inbound WHERE id = %s FOR UPDATE", (turn,))
        second = InThread(
            dsns.app,
            lambda c: budget.reserve(
                c, job_id=job, turn_id=turn, reserve_paise=1000,
                lease_until=now + timedelta(minutes=5), now=now,
            ),
        )  # fmt: skip
        assert second.wait_until_blocked(dsns.superuser) in ("transactionid", "tuple")
        with psycopg.connect(dsns.app) as probe:
            probe.execute(
                "SELECT 1 FROM ops.brain_budget WHERE month = %s FOR UPDATE NOWAIT", (month,)
            )
        holder.rollback()
    # Second job, same turn, ACTIVE reservation still in its lease: it owns nothing.
    assert second.join() == (Owner.OWNED_ELSEWHERE, rid)
    assert rid is not None and _state(app, rid) == ("ACTIVE", None)
    assert _totals(app, month)[2] == 1000  # nothing else reserved


def test_unique_violation_is_owned_elsewhere(
    dsns: Dsns, app: Conn, person: uuid.UUID, new_turn: Turns
) -> None:
    """A writer that skipped the turn lock still cannot create a second owner (the index)."""
    month = _month()
    now = _utc(month)
    turn = new_turn()
    job = _job(app)
    with psycopg.connect(dsns.app) as rogue:
        rogue.execute(
            "INSERT INTO ops.brain_budget (month, cap_inr) VALUES (%s, 500000)"
            " ON CONFLICT DO NOTHING",
            (month,),
        )
        rogue.commit()
        rogue.execute(
            "INSERT INTO ops.brain_reservation (job_id, turn_id, month, day, reserved_inr,"
            " lease_until) VALUES (%s, %s, %s, %s, 1, %s)",
            (job, turn, month, month, now + timedelta(minutes=5)),
        )
        racer = InThread(
            dsns.app,
            lambda c: budget.reserve(
                c, job_id=job, turn_id=turn, reserve_paise=1000,
                lease_until=now + timedelta(minutes=5), now=now,
            ),
        )  # fmt: skip
        racer.wait_until_blocked(dsns.superuser)  # on the uncommitted unique-index entry
        rogue.commit()
    assert racer.join() == (Owner.OWNED_ELSEWHERE, None)
    assert _totals(app, month)[2] == 0  # the refused reservation added nothing


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        (None, Owner.RESERVED),
        ("RELEASED", Owner.RESERVED),
        ("ACTIVE_IN_LEASE", Owner.OWNED_ELSEWHERE),
        ("ACTIVE_EXPIRED", Owner.INDETERMINATE),
        ("LAPSED", Owner.INDETERMINATE),
        ("SETTLED", Owner.ANSWERED),
    ],
)
def test_second_job_table(
    app: Conn, person: uuid.UUID, new_turn: Turns, existing: str | None, expected: Owner
) -> None:  # §10.1 step 6, row by row
    month = _month()
    _budget(app, month, budget.MONTHLY_CAP_PAISE)
    t0 = _utc(month)
    later = t0 + timedelta(minutes=10)  # past the first job's 5-minute lease
    turn = new_turn()
    rid: uuid.UUID | None = None
    if existing is not None:
        got, rid = _reserve(app, turn, t0)
        assert got is Owner.RESERVED and rid is not None
        if existing == "RELEASED":
            budget.release(app, rid)
        elif existing == "ACTIVE_IN_LEASE":
            later = t0 + timedelta(minutes=4)
        elif existing == "LAPSED":
            assert budget.lapse_expired(app, later) == 1
        elif existing == "SETTLED":
            budget.settle(app, rid, actual_paise=400, usage=(10, 20), model="claude-opus-5-5")
    before = _totals(app, month)
    outbox_before = len(_items(app, person, "turn.indeterminate"))

    got, second = _reserve(app, turn, later)
    assert got is expected
    after = _totals(app, month)
    if expected is Owner.RESERVED:
        assert second is not None and second != rid
        assert _state(app, second) == ("ACTIVE", None)
        assert after == (before[0], before[1], before[2] + 1000)
    else:
        assert second == rid  # the existing owner's reservation; nothing new
    if expected is Owner.OWNED_ELSEWHERE:
        assert after == before and rid is not None and _state(app, rid) == ("ACTIVE", None)
        assert _outcome(app, turn) is None
    if expected is Owner.ANSWERED:
        assert after == before and rid is not None and _state(app, rid) == ("SETTLED", 400)
    if expected is Owner.INDETERMINATE:
        assert rid is not None and _state(app, rid) == ("LAPSED", None)
        assert _outcome(app, turn) == "INDETERMINATE"
        assert after == (before[0], 1000, 0)  # lapsed at its full value
        # Exactly one notification for the turn, whoever ran the indeterminate path.
        assert _items(app, person, "turn.indeterminate") == [("TODAY", "PENDING")]
        if existing == "LAPSED":  # the sweep already did it; the job changed nothing
            assert outbox_before == 1 and after == before
    else:
        assert _items(app, person, "turn.indeterminate") == []


def test_reserve_validates_arguments(app: Conn) -> None:
    now = _utc(_month())
    with pytest.raises(ValueError, match="reserve_paise"):
        _reserve(app, None, now, paise=0)
    with pytest.raises(ValueError, match="lease_until"):
        _reserve(app, None, now, lease=-1)
    with pytest.raises(ValueError, match="time zone"):
        budget.reserve(app, job_id=_job(app), turn_id=None, reserve_paise=1,
                       lease_until=datetime(2040, 1, 1), now=datetime(2040, 1, 1))  # fmt: skip
    with pytest.raises(LookupError, match="turn"):
        _reserve(app, uuid.uuid4(), now)


def test_missing_month_row_is_created_with_the_cap(app: Conn) -> None:
    month = _month()
    got, _ = _reserve(app, None, _utc(month), paise=1000)
    assert got is Owner.RESERVED
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 0, 1000)
    assert budget.MONTHLY_CAP_PAISE == 500_000  # Rs 5,000


# --- settle and release ------------------------------------------------------------------------


def test_settle_moves_reserved_to_settled_and_records_usage(app: Conn) -> None:
    month = _month()
    _, rid = _reserve(app, None, _utc(month), paise=1000)
    assert rid is not None
    budget.settle(app, rid, actual_paise=420, usage=(1234, 56), model="claude-opus-5-5")
    assert _state(app, rid) == ("SETTLED", 420)
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 420, 0)
    usage = app.execute(
        "SELECT model, input_tokens, output_tokens, cost_inr FROM ops.brain_usage"
        " WHERE reservation_id = %s",
        (rid,),
    ).fetchall()
    assert usage == [("claude-opus-5-5", 1234, 56, 420)]
    with pytest.raises(budget.ReservationStateError):
        budget.settle(app, rid, actual_paise=1, usage=(1, 1), model="claude-opus-5-5")
    with pytest.raises(budget.ReservationStateError):
        budget.release(app, rid)


def test_release_only_from_active(app: Conn) -> None:
    month = _month()
    now = _utc(month)
    _, rid = _reserve(app, None, now, paise=700)
    assert rid is not None
    budget.release(app, rid)
    assert _state(app, rid) == ("RELEASED", None)
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 0, 0)
    with pytest.raises(budget.ReservationStateError):
        budget.release(app, rid)
    with pytest.raises(budget.ReservationStateError):
        budget.release(app, uuid.uuid4())
    _, lapsing = _reserve(app, None, now, paise=700)
    assert lapsing is not None
    assert budget.lapse_expired(app, now + timedelta(minutes=6)) == 1
    with pytest.raises(budget.ReservationStateError):
        budget.release(app, lapsing)  # a LAPSED reservation is never given back


# --- lapse sweep -------------------------------------------------------------------------------


def test_lapse_counts_full_value_and_notifies_once(
    app: Conn, person: uuid.UUID, new_turn: Turns
) -> None:
    month = _month()
    now = _utc(month)
    turn = new_turn()
    _, rid = _reserve(app, turn, now, paise=1500)
    _, probe = _reserve(app, None, now, paise=200)  # a probe: lapses, nobody to notify
    assert rid is not None and probe is not None
    assert budget.lapse_expired(app, now + timedelta(minutes=4)) == 0  # still in lease
    swept = budget.lapse_expired(app, now + timedelta(minutes=6))
    assert swept == 2
    assert budget.lapse_expired(app, now + timedelta(minutes=7)) == 0  # sweep twice
    assert _state(app, rid) == ("LAPSED", None)
    assert _state(app, probe) == ("LAPSED", None)
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 1700, 0)
    assert _outcome(app, turn) == "INDETERMINATE"
    assert _items(app, person, "turn.indeterminate") == [("TODAY", "PENDING")]
    assert _events(app, "turn.indeterminate", str(turn)) == 1


def test_lapse_keeps_an_existing_outcome(app: Conn, person: uuid.UUID, new_turn: Turns) -> None:
    month = _month()
    now = _utc(month)
    turn = new_turn()
    _, rid = _reserve(app, turn, now)
    app.execute("UPDATE conv.inbound SET outcome = 'ANSWERED' WHERE id = %s", (turn,))
    assert budget.lapse_expired(app, now + timedelta(minutes=6)) == 1
    assert rid is not None and _state(app, rid) == ("LAPSED", None)
    assert _outcome(app, turn) == "ANSWERED"  # the guard: the path runs only when empty
    assert _items(app, person, "turn.indeterminate") == []


def test_sweep_and_late_job_race_notify_once(
    dsns: Dsns, app: Conn, person: uuid.UUID, new_turn: Turns
) -> None:  # P2-B5 tail
    month = _month()
    now = _utc(month)
    later = now + timedelta(minutes=10)
    turn = new_turn()
    _, rid = _reserve(app, turn, now)
    assert rid is not None
    job = _job(app)
    # Hold the turn row so both the sweep and the late job queue on it, then let them race.
    with psycopg.connect(dsns.app) as holder:
        holder.execute("SELECT 1 FROM conv.inbound WHERE id = %s FOR UPDATE", (turn,))
        sweep = InThread(dsns.app, lambda c: budget.lapse_expired(c, later))
        sweep.wait_until_blocked(dsns.superuser)
        late = InThread(
            dsns.app,
            lambda c: budget.reserve(
                c, job_id=job, turn_id=turn, reserve_paise=1000,
                lease_until=later + timedelta(minutes=5), now=later,
            ),
        )  # fmt: skip
        late.wait_until_blocked(dsns.superuser)
        holder.rollback()
    swept = sweep.join()
    assert late.join() == (Owner.INDETERMINATE, rid)
    assert swept in (0, 1)
    assert _state(app, rid) == ("LAPSED", None)
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 1000, 0)  # counted once
    assert _items(app, person, "turn.indeterminate") == [("TODAY", "PENDING")]
    assert _events(app, "turn.indeterminate", str(turn)) == 1


def test_sweep_job_kind(app: Conn) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=1)
    _, rid = budget.reserve(
        app, job_id=_job(app), turn_id=None, reserve_paise=300,
        lease_until=long_ago + timedelta(minutes=5), now=long_ago,
    )  # fmt: skip
    assert rid is not None
    job_id = jobs.enqueue(app, "brain.reservation_sweep")
    dispatch.handle(app, jobs.Job(id=job_id, kind="brain.reservation_sweep", payload={},
                                  attempts=1, max_attempts=2))  # fmt: skip
    assert _state(app, rid) == ("LAPSED", None)  # swept with the database clock
    status = app.execute("SELECT status FROM ops.job WHERE id = %s", (job_id,)).fetchone()
    assert status == ("DONE",)


# --- 80% and 100% (P2-B2) ----------------------------------------------------------------------


def test_80_and_100_percent(
    app: Conn, person: uuid.UUID, new_turn: Turns, monkeypatch: pytest.MonkeyPatch
) -> None:
    month = _month()
    _budget(app, month, 10_000, monkeypatch)
    now = _utc(_next(month) - timedelta(days=1))  # the last day: no ceiling below the cap
    subject = month.strftime("%Y-%m")

    def ask(paise: int) -> Owner:
        return _reserve(app, new_turn(), now, paise=paise)[0]

    assert ask(7_999) is Owner.RESERVED  # 79.99%: nothing
    assert _items(app, person, "budget.80") == []
    assert ask(1) is Owner.RESERVED  # exactly 80%
    assert _items(app, person, "budget.80") == [("TODAY", "PENDING")]
    assert ask(1_000) is Owner.RESERVED  # 90%: no second warning this month
    assert _events(app, "budget.80", subject) == 1
    assert _items(app, person, "budget.100") == []
    assert ask(1_001) is Owner.NO_BUDGET  # would pass the cap: refused, calls stopped
    assert _items(app, person, "budget.100") == [("URGENT", "PENDING")]
    assert ask(1_000) is Owner.RESERVED  # exactly 100% still fits
    assert ask(1) is Owner.NO_BUDGET
    assert _events(app, "budget.100", subject) == 1  # once a month
    assert _events(app, "budget.80", subject) == 1
    assert _totals(app, month) == (10_000, 0, 10_000)
    assert _items(app, person, "budget.daily") == []  # the cap refused, not today's ceiling


# --- budget.daily (R12-14) and a raised cap (R12-15) -------------------------------------------


def test_budget_daily_once_per_day(
    app: Conn, person: uuid.UUID, new_turn: Turns, monkeypatch: pytest.MonkeyPatch
) -> None:
    month = _month()
    _budget(app, month, 3000, monkeypatch)
    now = _utc(_next(month) - timedelta(days=10))  # ceiling 300 today, 333 tomorrow
    assert _reserve(app, new_turn(), now, paise=400)[0] is Owner.NO_BUDGET
    assert _items(app, person, "budget.daily") == [("URGENT", "PENDING")]
    assert _reserve(app, new_turn(), now + timedelta(hours=1), paise=400)[0] is Owner.NO_BUDGET
    assert _events(app, "budget.daily", (now + IST_OFFSET).date().isoformat()) == 1  # once today
    assert _reserve(app, new_turn(), now + timedelta(days=1), paise=400)[0] is Owner.NO_BUDGET
    tomorrow = (now + timedelta(days=1) + IST_OFFSET).date().isoformat()
    assert _events(app, "budget.daily", tomorrow) == 1  # a new IST day: one more
    assert len(_items(app, person, "budget.daily")) == 2
    assert _items(app, person, "budget.100") == []
    assert _reserve(app, None, now, paise=400)[0] is Owner.NO_BUDGET  # a probe tells no one
    assert len(_items(app, person, "budget.daily")) == 2


def test_monthly_cap_refusal_is_budget_100_not_daily(
    app: Conn, person: uuid.UUID, new_turn: Turns, monkeypatch: pytest.MonkeyPatch
) -> None:
    month = _month()
    _budget(app, month, 3000, monkeypatch)
    now = _utc(_next(month) - timedelta(days=1))  # the last day: both limits refuse 3001
    assert _reserve(app, new_turn(), now, paise=3001)[0] is Owner.NO_BUDGET
    assert _items(app, person, "budget.100") == [("URGENT", "PENDING")]
    assert _items(app, person, "budget.daily") == []


def test_raised_cap_applies_to_the_current_month(app: Conn) -> None:
    month = _month()
    _budget(app, month, 1000)  # created under an older, lower cap
    now = _utc(_next(month) - timedelta(days=1))
    assert _reserve(app, None, now, paise=2000)[0] is Owner.RESERVED  # fits the raised cap
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 0, 2000)


def test_lower_configured_cap_never_lowers_the_month(app: Conn) -> None:
    month = _month()
    _budget(app, month, budget.MONTHLY_CAP_PAISE + 100_000)
    now = _utc(_next(month) - timedelta(days=1))
    assert _reserve(app, None, now, paise=550_000)[0] is Owner.RESERVED
    assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE + 100_000, 0, 550_000)


# --- review fixes: the late owner and the sweep job (I1, I2) -----------------------------------


@pytest.mark.parametrize("owner_step", ["settle", "release"])
def test_owner_finishing_while_second_job_waits(
    dsns: Dsns, app: Conn, person: uuid.UUID, new_turn: Turns, owner_step: str
) -> None:
    """The owner's settle (or release) is queued on the budget row ahead of a second job whose
    unlocked read saw ACTIVE past its lease. The second job acts on the state it reads under
    the lock: an answered turn stays answered, a released one is reserved afresh."""
    month = _month()
    _budget(app, month, budget.MONTHLY_CAP_PAISE)
    t0 = _utc(month)
    later = t0 + timedelta(minutes=10)
    turn = new_turn()
    _, rid = _reserve(app, turn, t0)
    assert rid is not None
    job = _job(app)

    def finish(c: Conn) -> None:
        if owner_step == "settle":
            budget.settle(c, rid, actual_paise=50, usage=(10, 2), model="claude-opus-5-5")
        else:
            budget.release(c, rid)

    with psycopg.connect(dsns.app) as holder:
        holder.execute("SELECT 1 FROM ops.brain_budget WHERE month = %s FOR UPDATE", (month,))
        owner_job = InThread(dsns.app, finish)
        owner_job.wait_until_blocked(dsns.superuser)
        second = InThread(
            dsns.app,
            lambda c: budget.reserve(
                c, job_id=job, turn_id=turn, reserve_paise=1000,
                lease_until=later + timedelta(minutes=5), now=later,
            ),
        )  # fmt: skip
        second.wait_until_blocked(dsns.superuser)
        holder.rollback()
    owner_job.join()
    got, got_id = second.join()
    assert _outcome(app, turn) is None
    assert _items(app, person, "turn.indeterminate") == []
    if owner_step == "settle":
        assert (got, got_id) == (Owner.ANSWERED, rid)
        assert _state(app, rid) == ("SETTLED", 50)
        assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 50, 0)
    else:
        assert got is Owner.RESERVED and got_id is not None and got_id != rid
        assert _state(app, rid) == ("RELEASED", None)
        assert _state(app, got_id) == ("ACTIVE", None)
        assert _totals(app, month) == (budget.MONTHLY_CAP_PAISE, 0, 1000)


def _lock_waiters(dsns: Dsns, at_least: int) -> None:
    """Wait (bounded) until `at_least` backends of the test database wait on a lock."""
    with psycopg.connect(dsns.superuser, autocommit=True) as su:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = su.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s"
                " AND wait_event_type = 'Lock'",
                (dsns.database,),
            ).fetchone()
            if row is not None and row[0] >= at_least:
                return
            time.sleep(0.01)
    raise AssertionError("no backend waited on a lock")


def test_sweep_job_and_late_reserve_do_not_deadlock(
    dsns: Dsns, app: Conn, person: uuid.UUID, new_turn: Turns
) -> None:
    """Two expired reservations in one month, swept by the job (inside dispatch's transaction)
    while a late job holds the second turn and then reserves it. Each lapse commits on its
    own, so the sweep never holds the month while it waits for a turn (turn, then budget)."""
    start = datetime.now(UTC) - timedelta(hours=1)
    first_turn, second_turn = new_turn(), new_turn()
    _, first = _reserve(app, first_turn, start)
    _, second = _reserve(app, second_turn, start + timedelta(seconds=1))
    assert first is not None and second is not None
    month, _ = budget.ist_month_day(start)
    before = _totals(app, month)
    sweep_id = jobs.enqueue(app, "brain.reservation_sweep")
    late_job = _job(app)
    with psycopg.connect(dsns.app) as late:
        late.execute("SELECT 1 FROM conv.inbound WHERE id = %s FOR UPDATE", (second_turn,))
        sweep = InThread(
            dsns.app,
            lambda c: dispatch.handle(c, jobs.Job(id=sweep_id, kind="brain.reservation_sweep",
                                                  payload={}, attempts=1, max_attempts=2)),
        )  # fmt: skip
        _lock_waiters(dsns, 1)  # the sweep lapsed the first and now waits for the second turn
        late.execute("SET LOCAL lock_timeout = '10s'")
        now = datetime.now(UTC)
        got = budget.reserve(
            late, job_id=late_job, turn_id=second_turn, reserve_paise=1000,
            lease_until=now + timedelta(minutes=5), now=now,
        )  # fmt: skip
        late.commit()
    sweep.join()
    assert got == (Owner.INDETERMINATE, second)
    assert _state(app, first) == ("LAPSED", None)
    assert _state(app, second) == ("LAPSED", None)
    assert _totals(app, month) == (before[0], before[1] + 2000, before[2] - 2000)
    assert _outcome(app, first_turn) == _outcome(app, second_turn) == "INDETERMINATE"
    assert len(_items(app, person, "turn.indeterminate")) == 2  # one per turn
    status = app.execute("SELECT status FROM ops.job WHERE id = %s", (sweep_id,)).fetchone()
    assert status == ("DONE",)
