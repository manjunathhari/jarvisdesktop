"""Job queue and heartbeats (spec §8)."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from jarvis_server import jobs, smoke

from .conftest import Dsns

Conn = psycopg.Connection[Any]


@pytest.fixture(autouse=True)
def _empty_queue(owner: Conn) -> None:
    owner.execute("DELETE FROM ops.job")
    owner.execute("DELETE FROM ops.heartbeat")


def test_job_runs_once_and_completes(app: Conn) -> None:
    job_id = jobs.enqueue(app, "noop", {"n": 1})
    job = jobs.claim(app, "w1")
    assert job is not None and job.id == job_id and job.payload == {"n": 1} and job.attempts == 1
    assert jobs.claim(app, "w2") is None
    jobs.complete(app, job_id)
    assert app.execute("SELECT status FROM ops.job WHERE id=%s", (job_id,)).fetchone() == ("DONE",)


def test_two_workers_never_get_the_same_job(dsns: Dsns) -> None:
    with psycopg.connect(dsns.app, autocommit=True) as setup:
        for i in range(40):
            jobs.enqueue(setup, "noop", {"i": i})
    claimed: list[int] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        with psycopg.connect(dsns.app, autocommit=True) as conn:
            while (job := jobs.claim(conn, name)) is not None:
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 and len(set(claimed)) == 40


def test_failed_job_is_retried_once_then_marked_failed(app: Conn) -> None:
    job_id = jobs.enqueue(app, "noop")
    first = jobs.claim(app, "w")
    assert first is not None
    assert jobs.fail(app, job_id, "boom") == "QUEUED"
    second = jobs.claim(app, "w")
    assert second is not None and second.attempts == 2
    assert jobs.fail(app, job_id, "boom again") == "FAILED"
    assert jobs.claim(app, "w") is None


def test_delayed_job_waits(app: Conn) -> None:
    jobs.enqueue(app, "noop", delay=timedelta(hours=1))
    assert jobs.claim(app, "w") is None


def test_abandoned_job_is_reclaimed_then_swept(app: Conn) -> None:
    job_id = jobs.enqueue(app, "noop")
    assert jobs.claim(app, "dead-worker", lease=timedelta(seconds=-1)) is not None
    again = jobs.claim(app, "w2", lease=timedelta(seconds=-1))
    assert again is not None and again.id == job_id and again.attempts == 2
    assert jobs.claim(app, "w3") is None
    assert jobs.sweep_abandoned(app) == 1
    row = app.execute("SELECT status FROM ops.job WHERE id=%s", (job_id,)).fetchone()
    assert row == ("FAILED",)


def test_silent_process_detected_after_three_missed_beats(app: Conn, owner: Conn) -> None:
    jobs.beat(app, "core")
    jobs.beat(app, "scheduler")
    assert jobs.silent_processes(app) == []
    owner.execute(
        "UPDATE ops.heartbeat SET last_seen = now() - interval '91 seconds' WHERE process='core'"
    )
    assert jobs.silent_processes(app) == ["core"]


def test_on_server_smoke_check_passes(app: Conn) -> None:
    results = smoke.run(app)
    assert [name for name, ok, _ in results if not ok] == []
    assert len(results) == 5
