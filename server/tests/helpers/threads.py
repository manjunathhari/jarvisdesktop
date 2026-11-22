"""Threads for race tests. Every exception in a worker reaches the test (owner requirement 4).

`InThread` runs one call on its own connection. `join()` re-raises whatever the thread raised,
including a failure to connect, and fails if the thread is still running. `wait_until_blocked()`
proves a real lock wait through pg_stat_activity, and stops at once with the thread's own error
if the thread finished instead of blocking, so an error is never turned into a bare timeout.

`run_all` runs several workers at once and re-raises the first exception any of them raised.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

Conn = psycopg.Connection[Any]


class ThreadStillRunning(AssertionError):
    pass


class InThread:
    """Run `fn(conn)` on a new autocommit connection to `dsn` in a thread."""

    def __init__(self, dsn: str, fn: Callable[[Conn], Any], *, timeout: float = 15) -> None:
        self.pid: int | None = None
        self.result: Any = None
        self.error: BaseException | None = None
        self._timeout = timeout
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(dsn, fn), daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise ThreadStillRunning("worker thread never connected")
        if self.pid is None:  # the connection itself failed: re-raise it here
            self.join()

    def _run(self, dsn: str, fn: Callable[[Conn], Any]) -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                self.pid = conn.info.backend_pid
                self._ready.set()
                self.result = fn(conn)
        except BaseException as e:
            self.error = e
        finally:
            self._ready.set()

    def join(self) -> Any:
        """Wait for the thread; re-raise its exception, or return its result."""
        self._thread.join(self._timeout)
        if self._thread.is_alive():
            raise ThreadStillRunning(f"worker thread (backend {self.pid}) still running")
        if self.error is not None:
            raise self.error
        return self.result

    def wait_until_blocked(self, superuser_dsn: str) -> str:
        """Return once the thread's backend waits on a lock; its wait_event, e.g. 'tuple'."""
        with psycopg.connect(superuser_dsn, autocommit=True) as su:
            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                row = su.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    (self.pid,),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    return str(row[1])
                if not self._thread.is_alive():
                    self.join()  # re-raises the thread's error, if any
                    raise AssertionError(
                        f"backend {self.pid} finished without waiting on a lock"
                        f" (result {self.result!r})"
                    )
                time.sleep(0.01)
        raise AssertionError(f"backend {self.pid} never waited on a lock")


def run_all(workers: Sequence[Callable[[], None]], *, timeout: float = 120) -> None:
    """Run each worker in its own thread; re-raise the first exception any of them raised."""
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrap(fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=wrap, args=(w,), daemon=True) for w in workers]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if errors:
        # A worker that failed first aborts a shared barrier; prefer its error to the
        # BrokenBarrierError that abort raises in the others.
        real = [e for e in errors if not isinstance(e, threading.BrokenBarrierError)]
        raise (real or errors)[0]
    if any(t.is_alive() for t in threads):
        raise ThreadStillRunning("worker threads still running")
