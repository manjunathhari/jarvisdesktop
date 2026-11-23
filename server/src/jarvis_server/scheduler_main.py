"""`jarvis-scheduler`: writes a heartbeat every 30 s and reports silent processes.

Phase 0 only proves the heartbeat; briefings, scans and filing arrive in later phases.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import psycopg

from jarvis_server import config, jobs

log = logging.getLogger("jarvis.scheduler")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = jobs.HEARTBEAT_INTERVAL.total_seconds()
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        log.info("scheduler started")
        while not stopping:
            jobs.beat(conn, "scheduler")
            # "brain" is the brain-health row (brain.health), not a process with a heartbeat.
            silent = [p for p in jobs.silent_processes(conn) if p not in ("scheduler", "brain")]
            if silent:
                log.warning("silent processes (3 missed heartbeats): %s", ", ".join(silent))
            for _ in range(int(interval)):
                if stopping:
                    break
                time.sleep(1)
    log.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
