"""`jarvis-core`: the core process and its admin commands.

jarvis-core init        bootstrap + migrate (+ smoke if JARVIS_SMOKE_ON_INIT=1), one-shot
jarvis-core bootstrap   create roles and database (superuser; once per server)
jarvis-core migrate     apply migrations (owner role)
jarvis-core downgrade   undo the newest migration (owner role; take a dump first)
jarvis-core serve       run the core loop: heartbeat + job queue (app role)
jarvis-core smoke       check P0-1 … P0-5 against the live database (app role)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

import psycopg

from jarvis_server import config, jobs, migrate, smoke
from jarvis_server.bootstrap import bootstrap

log = logging.getLogger("jarvis.core")

Handler = Callable[[psycopg.Connection[Any], jobs.Job], None]


def _noop(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    log.info("noop job %s ran (payload %s)", job.id, job.payload)


HANDLERS: dict[str, Handler] = {"noop": _noop}


def serve(poll_seconds: float = 2.0) -> None:
    worker = f"core@{socket.gethostname()}"
    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_beat = 0.0
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        log.info("core started as %s", worker)
        while not stopping:
            if time.monotonic() - last_beat >= jobs.HEARTBEAT_INTERVAL.total_seconds():
                jobs.beat(conn, "core")
                jobs.sweep_abandoned(conn)
                last_beat = time.monotonic()
            job = jobs.claim(conn, worker)
            if job is None:
                time.sleep(poll_seconds)
                continue
            handler = HANDLERS.get(job.kind)
            try:
                if handler is None:
                    raise LookupError(f"no handler for job kind {job.kind!r}")
                handler(conn, job)
                jobs.complete(conn, job.id)
            except Exception as e:
                status = jobs.fail(conn, job.id, f"{type(e).__name__}: {e}")
                log.warning("job %s (%s) failed: %s -> %s", job.id, job.kind, e, status)
    log.info("core stopped")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="jarvis-core")
    parser.add_argument(
        "command", choices=["init", "bootstrap", "migrate", "downgrade", "serve", "smoke"]
    )
    args = parser.parse_args(argv)

    if args.command == "init":
        _bootstrap()
        migrate.upgrade(config.owner_dsn())
        log.info("bootstrap done; migrations at head")
        if os.environ.get("JARVIS_SMOKE_ON_INIT") == "1":
            return _smoke()
    elif args.command == "bootstrap":
        _bootstrap()
        log.info("bootstrap done")
    elif args.command == "migrate":
        migrate.upgrade(config.owner_dsn())
        log.info("migrations at head")
    elif args.command == "downgrade":
        migrate.downgrade(config.owner_dsn(), "-1")
        log.info("newest migration undone")
    elif args.command == "serve":
        serve()
    else:
        return _smoke()
    return 0


def _bootstrap() -> None:
    bootstrap(
        config.superuser_dsn(),
        "jarvis",
        config.secret("pg_owner_password"),
        config.secret("pg_app_password"),
    )


def _smoke() -> int:
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        results = smoke.run(conn)
    for name, ok, detail in results:
        print(f"SMOKE {'PASS' if ok else 'FAIL'}  {name}  ({detail})", flush=True)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
