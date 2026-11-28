"""`jarvis-core`: the core process and its admin commands.

jarvis-core init        bootstrap + migrate + record the bootstrap signing keys (first deploy)
                        (+ smoke if JARVIS_SMOKE_ON_INIT=1), one-shot
jarvis-core bootstrap   create roles and database (superuser; once per server)
jarvis-core migrate     apply migrations (owner role)
jarvis-core downgrade   undo the newest migration (owner role; take a dump first)
jarvis-core serve       run the core loop: heartbeat + job queue (app role). Needs the secret
                        anthropic_api_key and HTTPS_PROXY (the egress proxy), or refuses to start
jarvis-core smoke       check P0-1 … P0-5 (live database, app role), M1-M4 (temporary repo) and
                        the database-only P2 rows (app and gateway roles; run it in `init`)
jarvis-core node enrol --person <slug> --kind DESKTOP|PHONE --name <text>
                        create a one-time enrolment (15 min) and print its enrolment code
jarvis-core node revoke <node_id>
                        revoke a node; its open connection is fenced at once
jarvis-core node reenrol <node_id>
                        revoke a node and print a new enrolment code (same person, kind and
                        name) with the current key fingerprints (R5-1)
jarvis-core keys init   record the current core and gateway keys as ACTIVE (bootstrap; init
                        does this itself; the manual fallback)
jarvis-core keys announce --role CORE|GATEWAY --public-key-file <path>
                        announce a new key (raw 32-byte public key) to every active node
jarvis-core keys retire <key_id>
                        end a retired key's overlap (never a NEXT or ACTIVE key)
jarvis-core keys list   the trusted keys and the STALE nodes that need `node reenrol`
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from jarvis_server import config, jobs, migrate, nodes, signing, smoke
from jarvis_server import retention as _retention_jobs  # noqa: F401  registers job kinds
from jarvis_server.bootstrap import bootstrap
from jarvis_server.brain import budget as _budget_jobs  # noqa: F401  registers job kinds
from jarvis_server.brain import health, prices
from jarvis_server.brain.claude import ClaudeBrain, shared_client
from jarvis_server.core import board_job as _board_jobs  # noqa: F401  registers job kinds
from jarvis_server.core import cad_job as _cad_jobs  # noqa: F401  registers job kinds
from jarvis_server.core import dispatch, keys, turn
from jarvis_server.dbcall import Refused
from jarvis_server.memory import checks as memory_checks
from jarvis_server.memory import format as memory_format
from jarvis_server.memory import service as memory_service

log = logging.getLogger("jarvis.core")

# Wakes the poll loop early; an optimisation only. The loop polls `ops.job` every
# `poll_seconds` whether or not a NOTIFY ever arrives.
JOBS_CHANNEL = "jarvis_jobs"


def open_listener(dsn: str) -> psycopg.Connection[Any] | None:
    """A separate autocommit connection that LISTENs on `jarvis_jobs`, or None if that fails."""
    try:
        listener = psycopg.connect(dsn, autocommit=True)
        listener.execute(f"LISTEN {JOBS_CHANNEL}")
    except psycopg.Error as e:
        log.warning("LISTEN %s unavailable, polling only: %s", JOBS_CHANNEL, e)
        return None
    return listener


def _wait_for_jobs(listener: psycopg.Connection[Any] | None, seconds: float) -> bool:
    """Wait up to `seconds`, returning early on a NOTIFY. False if the listener broke."""
    if listener is None:
        time.sleep(seconds)
        return True
    try:
        for _ in listener.notifies(timeout=seconds, stop_after=1):
            pass
    except psycopg.Error as e:
        log.warning("LISTEN %s lost, polling only: %s", JOBS_CHANNEL, e)
        return False
    return True


def run_loop(
    conn: psycopg.Connection[Any],
    listener: psycopg.Connection[Any] | None,
    worker: str,
    *,
    poll_seconds: float,
    should_stop: Callable[[], bool],
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Claim and dispatch jobs until `should_stop()`; wait between empty polls."""
    while not should_stop():
        if on_tick is not None:
            on_tick()
        job = jobs.claim(conn, worker)
        if job is None:
            if not _wait_for_jobs(listener, poll_seconds):
                listener = None  # fall back to plain polling for the rest of this run
            continue
        try:
            dispatch.handle(conn, job)  # records the failure itself before re-raising
        except Exception as e:
            log.warning("job %s (%s) not completed: %s", job.id, job.kind, dispatch.failure_code(e))


API_KEY_SECRET = "anthropic_api_key"  # noqa: S105  the secret file's name, not a secret


def _per_job_brains(api_key: str, proxy: str) -> Callable[[], tuple[ClaudeBrain, str]]:
    """A brain factory: a new `ClaudeBrain` for each job (Task 8 carry-over b), on the default
    model (R12-12). The brains share one HTTP client, so a turn reuses the previous turn's
    connection to Claude instead of opening a new one through the proxy (live check, 25 Nov).
    The client lives as long as the process."""
    client = shared_client(proxy)

    def build() -> tuple[ClaudeBrain, str]:
        model = prices.default_model()
        return ClaudeBrain(api_key, model, proxy=proxy, client=client), model

    return build


def wire_brain() -> None:
    """Give the conversation turn and the health probe their Claude brains (§10, R12-12).

    Refuses (SystemExit) without the secret `anthropic_api_key` or `HTTPS_PROXY`: core reaches
    Claude only through the egress proxy (§4.2). The key is never logged."""
    try:
        api_key = config.secret(API_KEY_SECRET)
    except RuntimeError as e:  # names the file, never its content
        raise SystemExit(f"core cannot start: {e}") from None
    proxy = os.environ.get("HTTPS_PROXY", "")
    if not proxy:
        raise SystemExit(
            "core cannot start: HTTPS_PROXY is not set (core reaches Claude only through the"
            " egress proxy)"
        )
    model = prices.default_model()  # the price table must load before any job runs
    turn.set_brain(_per_job_brains(api_key, proxy))
    health.set_probe_brain(_per_job_brains(api_key, proxy))
    log.info("Claude brain wired: model %s, through the egress proxy", model)


def serve(poll_seconds: float = 2.0) -> None:
    wire_brain()  # before anything else: refuses to start without the key or the proxy
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
        memory = memory_service.open_engine(conn)  # refuses to start if memory does not load
        turn.set_memory(memory)  # conv.turn reads memory as the authenticated person

        def _tick() -> None:
            nonlocal last_beat
            if time.monotonic() - last_beat >= jobs.HEARTBEAT_INTERVAL.total_seconds():
                jobs.beat(conn, "core")
                jobs.sweep_abandoned(conn)
                try:
                    memory_service.write_daily_bundle(memory)
                except Exception as e:  # a failed backup must not stop core
                    log.warning("memory backup bundle failed: %s", e)
                last_beat = time.monotonic()

        listener = open_listener(config.app_dsn())
        try:
            run_loop(
                conn,
                listener,
                worker,
                poll_seconds=poll_seconds,
                should_stop=lambda: stopping,
                on_tick=_tick,
            )
        finally:
            if listener is not None:
                listener.close()
    log.info("core stopped")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="jarvis-core")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "bootstrap", "migrate", "downgrade", "serve", "smoke"):
        commands.add_parser(name)
    node = commands.add_parser("node").add_subparsers(dest="node_command", required=True)
    enrol = node.add_parser("enrol")
    enrol.add_argument("--person", required=True, help="memory slug, e.g. manj")
    enrol.add_argument("--kind", required=True, type=str.upper, choices=nodes.KINDS)
    enrol.add_argument("--name", required=True)
    revoke = node.add_parser("revoke")
    revoke.add_argument("node_id", type=uuid.UUID)
    reenrol = node.add_parser("reenrol")
    reenrol.add_argument("node_id", type=uuid.UUID)
    key = commands.add_parser("keys").add_subparsers(dest="keys_command", required=True)
    key.add_parser("init")
    key.add_parser("list")
    announce = key.add_parser("announce")
    announce.add_argument("--role", required=True, type=str.upper, choices=keys.ROLES)
    announce.add_argument("--public-key-file", required=True, type=Path)
    retire = key.add_parser("retire")
    retire.add_argument("key_id")
    args = parser.parse_args(argv)

    if args.command == "node":
        return _node(args)
    if args.command == "keys":
        return _keys(args)
    if args.command == "init":
        _bootstrap()
        migrate.upgrade(config.owner_dsn())
        log.info("bootstrap done; migrations at head")
        record_bootstrap_keys()
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
        config.secret("pg_gateway_password"),
    )


def _person_id(slug: str) -> uuid.UUID:
    """The ledger person of a memory subject, read from its file without opening the engine
    (core may be running it)."""
    if not memory_format.SLUG_RE.match(slug):
        raise SystemExit(f"not a person slug: {slug!r}")
    rel = f"people/{slug}.md"
    path = memory_service.memory_dir() / rel
    try:
        person = memory_format.parse_file(rel, path.read_text(encoding="utf-8")).person
    except FileNotFoundError:
        person = None
    if person is None:
        raise SystemExit(f"no person {slug!r} in memory")
    return person.person_id


def _key_fingerprints(conn: psycopg.Connection[Any]) -> tuple[str, str]:
    """(core_fp, gw_fp) for an enrolment code: the ACTIVE CORE and GATEWAY key IDs in
    gw.trusted_key (a key ID is its public key's fingerprint)."""
    try:
        return keys.active_key(conn, "CORE").key_id, keys.active_key(conn, "GATEWAY").key_id
    except RuntimeError as e:
        raise SystemExit(str(e)) from e


def _warn_next_keys(conn: psycopg.Connection[Any]) -> None:
    """A device enrolled while a key is NEXT never gets it, and goes STALE when it activates
    (Task 12 concern b). Says so on stderr, so the code stays the last thing on stdout."""
    for k in keys.list_keys(conn):
        if k.state == "NEXT":
            by = (k.created_at + keys.ACTIVATE_BY).isoformat()
            print(
                f"Warning: {k.role} key {k.key_id} is being rotated in (activates when every"
                f" node acks, or at {by}). This device won't get it and will be marked STALE"
                " then: run `jarvis-core node reenrol <node_id>` for it after activation.",
                file=sys.stderr,
                flush=True,
            )


def _print_code(code: str) -> None:
    # The code goes to the terminal only, never to the log (§7.2 step 1).
    print(f"Enrolment code (valid 15 minutes, single use): {code}", flush=True)


def _node(args: argparse.Namespace) -> int:
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        try:
            if args.node_command == "enrol":
                core_fp, gw_fp = _key_fingerprints(conn)
                code = nodes.create_enrolment(
                    conn,
                    person_id=_person_id(args.person),
                    kind=args.kind,
                    name=args.name,
                    core_fp=core_fp,
                    gw_fp=gw_fp,
                )
                _warn_next_keys(conn)
                _print_code(code)
            elif args.node_command == "reenrol":
                code = nodes.reenrol(conn, args.node_id, *_key_fingerprints(conn))
                log.info("node %s revoked for re-enrolment", args.node_id)
                _warn_next_keys(conn)
                _print_code(code)
            else:
                nodes.revoke(conn, args.node_id)
                log.info("node %s revoked", args.node_id)
        except Refused as e:
            log.error("refused: %s", e.reason)
            return 1
    return 0


def _read_public(path: Path) -> bytes:
    try:
        public = path.read_bytes()
    except FileNotFoundError as e:
        raise SystemExit(f"{path} is missing") from e
    if len(public) != 32:
        raise SystemExit(f"{path} must hold a raw 32-byte Ed25519 public key")
    return public


def record_bootstrap_keys() -> None:
    """init, on every deploy: record `core_signing_key.pub` and `gateway_signing_key.pub` (raw
    public keys written by harden.sh; init never holds a private key) as the ACTIVE trusted
    keys. The first deploy records them, so the gateway can start; later deploys change
    nothing. When a role's ACTIVE key differs, `keys.init_keys` refuses (`active_key_differs`,
    nothing written): after a rotation that is expected, but it can also mean a database
    restored onto a re-hardened server (new key files). It is logged at WARNING naming the
    role, and does not fail the deploy. A missing public key file stops init."""
    core = _read_public(config.SECRETS_DIR / "core_signing_key.pub")
    gateway = _read_public(config.SECRETS_DIR / "gateway_signing_key.pub")
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        try:
            added = keys.init_keys(conn, core_public=core, gateway_public=gateway)
        except keys.KeyRefused as e:
            if e.reason != "active_key_differs":
                raise
            given = {"CORE": core, "GATEWAY": gateway}
            for role in keys.ROLES:
                active = keys.active_key(conn, role)
                if active.public_key != given[role]:
                    log.warning(
                        "trusted keys left as they are (active_key_differs): the ACTIVE %s key"
                        " %s is not %s_signing_key.pub. Expected after a key rotation; otherwise"
                        " a database restored onto a re-hardened server: check with"
                        " `jarvis-core keys list` (runbook §8.2, §8.5)",
                        role, active.key_id, role.lower(),
                    )  # fmt: skip
            return
    if added:
        log.info("trusted keys recorded: %s", ", ".join(added))
    else:
        log.info("trusted keys already recorded")


def _keys(args: argparse.Namespace) -> int:
    """`keys init|announce|retire|list`. Prints key IDs (public fingerprints), never a key."""
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        try:
            if args.keys_command == "init":
                core = signing.load_private_key(config.SECRETS_DIR / keys.SECRET_NAMES["CORE"])
                gw_public = _read_public(config.SECRETS_DIR / "gateway_signing_key.pub")
                added = keys.init_keys(
                    conn, core_public=signing.public_bytes(core), gateway_public=gw_public
                )
                log.info("trusted keys initialised (%d added)", len(added))
            elif args.keys_command == "announce":
                key_id = keys.announce(
                    conn, role=args.role, public_key=_read_public(args.public_key_file)
                )
                print(
                    f"Announced {args.role} key {key_id}. Its private key must be the secret"
                    f" {keys.secret_name(args.role, key_id)} before it activates.",
                    flush=True,
                )
            elif args.keys_command == "retire":
                keys.retire(conn, args.key_id)
                log.info("key %s retired", args.key_id)
            else:
                for k in keys.list_keys(conn):
                    retired = "" if k.retired_at is None else f" retired {k.retired_at.isoformat()}"
                    pending = " (overlap)" if k.state == "RETIRED" and k.retired_at is None else ""
                    if k.state == "NEXT":  # R12-28: all acks, or this deadline
                        by = (k.created_at + keys.ACTIVATE_BY).isoformat()
                        pending = f" (activates when every node acks, or at {by})"
                    print(f"{k.role} {k.key_id} {k.state}{pending}{retired}", flush=True)
                for role, key_id, secret in keys.missing_secrets(conn):
                    print(f"MISSING SECRET {role} {key_id}: place {secret}", flush=True)
                for node_id, name in keys.stale_nodes(conn):
                    print(f"STALE {node_id} {name}  (run: jarvis-core node reenrol {node_id})")
        except keys.KeyRefused as e:
            log.error("refused: %s", e.reason)
            return 1
    return 0


def _smoke() -> int:
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        results = smoke.run(conn)
        row = conn.execute(
            "SELECT has_function_privilege('audit.record_memory(text, text, text, text, text,"
            " text, text, text, text)', 'EXECUTE')"
        ).fetchone()
        results.append(("memory audit function", bool(row and row[0]), "app may record"))
    # M1-M4 in a throwaway repo with fictional people: no live memory, no live audit rows.
    results += memory_checks.run()
    # The database-only Phase 2 rows, as the app and gateway roles (needs pg_gateway_password,
    # which init has). No node, gateway process or brain.
    with (
        psycopg.connect(config.app_dsn(), autocommit=True) as app,
        psycopg.connect(config.gateway_dsn(), autocommit=True) as gateway,
    ):
        results += smoke.run_phase2(app, gateway)
    for name, ok, detail in results:
        print(f"SMOKE {'PASS' if ok else 'FAIL'}  {name}  ({detail})", flush=True)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
