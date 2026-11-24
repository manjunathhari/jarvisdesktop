"""`jarvis-scheduler`: the heartbeat, silent-process reports, and the scheduled jobs.

**It only enqueues (spec §5.4; plan Task 13, ruling 1).** Core's handlers do the work. Each due
item or period gets at most one job, deduplicated by a deterministic job key in its payload
(`jobs.enqueue_once`, `<kind>:<item>`). Approvals and approved actions queue their own jobs under
the same keys when they are created (Task 11), so the scheduler is their backstop and never
their double.

| Job | When | Key item |
|---|---|---|
| `approval.expiring` | from 2 min before a PENDING approval's expiry until it | the approval |
| `approval.expire` | from a PENDING approval's expiry | the approval |
| `outbox.expire` | from an open item's `expires_at` | the item |
| `brain.probe` | every 2 min while the brain is DOWN (§10.2) | the UTC 2-minute slot |
| `brain.reservation_sweep` | every minute (§10.1) | the UTC minute |
| `keys.activate` | every minute (Task 12, R12-28) | the UTC minute |
| `outbox.resend_sweep` | every 60 s: wakes the gateway's re-send sweep (§8.2) | the UTC minute |
| `retention.sweep` | daily from 03:30 IST (§5.7) | the IST date |

The clock is injected (`Scheduler.tick(conn, now)`); due items are compared with `now`, and every
job is queued to run at once. A tick that fails is logged and the next one tries again.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from typing import Any, LiteralString

import psycopg

from jarvis_server import config, jobs
from jarvis_server.brain import health
from jarvis_server.brain.budget import IST
from jarvis_server.core.approvals import RESURFACE_BEFORE

log = logging.getLogger("jarvis.scheduler")

TICK = timedelta(seconds=15)  # well inside the shortest period, so no minute is skipped
RETENTION_AT = clock_time(3, 30)  # IST
EVERY_MINUTE = ("brain.reservation_sweep", "keys.activate", "outbox.resend_sweep")
PROBE_EVERY = timedelta(minutes=2)

# (kind, the due items' IDs at %(now)s)
_DUE_ITEMS: tuple[tuple[str, LiteralString], ...] = (
    (
        "approval.expiring",
        "SELECT id FROM ledger.approval WHERE decision = 'PENDING'"
        " AND expires_at - %(before)s <= %(now)s AND %(now)s < expires_at ORDER BY expires_at, id",
    ),
    (
        "approval.expire",
        "SELECT id FROM ledger.approval WHERE decision = 'PENDING' AND expires_at <= %(now)s"
        " ORDER BY expires_at, id",
    ),
    (
        "outbox.expire",
        "SELECT id FROM comm.outbox WHERE state IN ('PENDING', 'DELIVERING', 'DELIVERED')"
        " AND expires_at <= %(now)s ORDER BY expires_at, id",
    ),
)
_PAYLOAD_KEY = {
    "approval.expiring": "approval_id",
    "approval.expire": "approval_id",
    "outbox.expire": "outbox_id",
}


def _slot(now: datetime, every: timedelta) -> str:
    """The UTC start of the period `now` falls in, to the minute: `2026-11-24T10:20Z`."""
    seconds = int(every.total_seconds())
    start = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, UTC)
    return start.strftime("%Y-%m-%dT%H:%MZ")


class Scheduler:
    """Decides what is due at a time and enqueues it once. Keys it has already handled are
    remembered, so a quiet tick costs one query per kind of due item; the database's dedup
    (`jobs.enqueue_once`) holds across restarts and other writers."""

    def __init__(self) -> None:
        self._done: set[str] = set()

    def tick(self, conn: psycopg.Connection[Any], now: datetime) -> list[int]:
        """Enqueue everything due at `now`; return the new jobs' IDs."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must carry a time zone")
        due: list[tuple[str, str, dict[str, Any]]] = []
        params = {"now": now, "before": RESURFACE_BEFORE}
        for kind, sql in _DUE_ITEMS:
            for (item,) in conn.execute(sql, params).fetchall():
                due.append((kind, str(item), {_PAYLOAD_KEY[kind]: str(item)}))
        for kind in EVERY_MINUTE:
            due.append((kind, _slot(now, timedelta(minutes=1)), {}))
        if health.is_down(conn):
            due.append(("brain.probe", _slot(now, PROBE_EVERY), {}))
        local = now.astimezone(IST)
        if local.time() >= RETENTION_AT:
            due.append(("retention.sweep", local.date().isoformat(), {}))
        created: list[int] = []
        for kind, item, payload in due:
            key = jobs.job_key(kind, item)
            if key in self._done:
                continue
            job_id = jobs.enqueue_once(conn, kind, key, payload)
            if job_id is not None:
                created.append(job_id)
                log.info("queued %s (job %s)", key, job_id)
            self._done.add(key)
        self._forget(now)
        return created

    def _forget(self, now: datetime) -> None:
        """Drop remembered keys of periods that are over; an item's key is kept, and is small."""
        current = {
            _slot(now, timedelta(minutes=1)),
            _slot(now, PROBE_EVERY),
            now.astimezone(IST).date().isoformat(),
        }
        periodic = (*EVERY_MINUTE, "brain.probe", "retention.sweep")
        self._done = {
            k for k in self._done
            if not k.startswith(periodic) or k.split(":", 1)[1] in current
        }  # fmt: skip


def run(
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """The scheduler loop: a heartbeat every 30 s, a tick every 15 s, until `should_stop()`."""
    scheduler = Scheduler()
    last_beat: datetime | None = None
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        log.info("scheduler started")
        while not should_stop():
            now = clock()
            if last_beat is None or now - last_beat >= jobs.HEARTBEAT_INTERVAL:
                jobs.beat(conn, "scheduler")
                # "brain" is the brain-health row (brain.health), not a process with a heartbeat.
                silent = [p for p in jobs.silent_processes(conn) if p not in ("scheduler", "brain")]
                if silent:
                    log.warning("silent processes (3 missed heartbeats): %s", ", ".join(silent))
                last_beat = now
            try:
                scheduler.tick(conn, now)
            except psycopg.Error as e:
                log.warning("scheduler tick failed (%s); trying again next tick", e.sqlstate)
            sleep(TICK.total_seconds())
    log.info("scheduler stopped")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    def _sleep(seconds: float) -> None:
        for _ in range(int(seconds)):
            if stopping:
                return
            time.sleep(1)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run(should_stop=lambda: stopping, sleep=_sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
