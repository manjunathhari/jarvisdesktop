"""jarvis-improver: the team's orchestrator (`run`) and Manj's commands.

On the server: `docker compose --profile improve exec improver jarvis-improver <command>`.

    run                      the orchestrator loop (the improver service's command)
    open WORDS...            open a ticket from a complaint, in his words
    approve ID FINGERPRINT   yes to the design as it stands (first 8 characters are enough)
    change ID WORDS...       ask for a different design
    resume ID                go on after a pause (allows one more stage at the cap)
    stop ID                  stop a ticket
    inbox                    what needs Manj

`run` checks its keys first. Anything missing goes to the inbox as exact steps that name the key and
never show a value, and the loop waits for it rather than failing over and over.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis_server.improve import inbox
from jarvis_server.improve.ports import Store
from jarvis_server.improve.stages import LEASE_S, Orchestrator, Refused

log = logging.getLogger("jarvis.improve")

REPO = "manjunathhari/jarvisdesktop"
OWNER = "manjunathhari"
APP_KEY = "improve_github_app_key"
CLAUDE_KEY = "improve_claude_key"
PROXY_HEALTH = "http://claude-proxy:8080/healthz"
_RESTART = "Restart the team: docker compose --profile improve up -d"


def missing_keys(
    env: Mapping[str, str], secrets_dir: Path, *, proxy_up: Callable[[], bool]
) -> list[inbox.Item]:
    items = []
    pem = secrets_dir / APP_KEY
    key_ok = pem.is_file() and "PRIVATE KEY-----" in pem.read_text(errors="replace")
    ids_ok = all(
        env.get(k, "").isdigit()
        for k in ("JARVIS_IMPROVE_APP_ID", "JARVIS_IMPROVE_INSTALLATION_ID")
    )
    if not (key_ok and ids_ok):
        items.append(
            inbox.key_needed(
                APP_KEY,
                "the GitHub App's private key: the team uses it to push improve/ branches and open"
                " PRs, never to merge",
                (
                    "Create the GitHub App and install it on the repo, as the PR's setup steps say",
                    f"Save its private key on the server as /etc/jarvis/secrets/{APP_KEY}"
                    " (owner root, mode 0400)",
                    "Put the App's numeric ids in the server's .env as JARVIS_IMPROVE_APP_ID and"
                    " JARVIS_IMPROVE_INSTALLATION_ID",
                    _RESTART,
                ),
            )
        )
    if not proxy_up():
        items.append(
            inbox.key_needed(
                CLAUDE_KEY,
                "the team's own Claude key, separate from JARVIS's, so its spend has its own limit",
                (
                    "In the Claude Console, make a new workspace with a $30 monthly spend limit and"
                    " create an API key in it",
                    f"Save the key on the server as /etc/jarvis/secrets/{CLAUDE_KEY}"
                    " (owner root, mode 0400)",
                    _RESTART,
                ),
            )
        )
    return items


def sync_keys(store: Store, missing: Sequence[inbox.Item]) -> None:
    open_keys = {i.key_name for i in store.open_inbox() if i.kind is inbox.Kind.KEY}
    wanted = {i.key_name for i in missing}
    for item in missing:
        if item.key_name not in open_keys:
            store.add_inbox(item)
    for name in open_keys - wanted:
        if name is not None:
            store.resolve_key(name)


def _proxy_up() -> bool:
    try:
        with urllib.request.urlopen(PROXY_HEALTH, timeout=5) as r:
            return bool(r.status == 200)
    except OSError:
        return False


class _NotHere:
    """For commands, which touch only the store: the sandbox, repo and GitHub aren't needed."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"{name} is used only by `jarvis-improver run`")


def _orchestrator(store: Store) -> Orchestrator:
    unused: Any = _NotHere()
    return Orchestrator(store, unused, unused, unused, owner=OWNER, now=lambda: datetime.now(UTC))


def command(store: Store, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-improver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("open").add_argument("words", nargs="+")
    a = sub.add_parser("approve")
    a.add_argument("id", type=int)
    a.add_argument("fingerprint")
    c = sub.add_parser("change")
    c.add_argument("id", type=int)
    c.add_argument("words", nargs="+")
    sub.add_parser("resume").add_argument("id", type=int)
    sub.add_parser("stop").add_argument("id", type=int)
    sub.add_parser("inbox")
    args = parser.parse_args(argv)
    orch = _orchestrator(store)
    try:
        if args.cmd == "open":
            tid = orch.open_ticket(" ".join(args.words), {})
            print(f"Opened ticket {tid}. The design comes to your inbox for a yes.")
        elif args.cmd == "approve":
            orch.approve(args.id, args.fingerprint, actor="owner")
            print(f"Ticket {args.id} approved; the Test writer starts next.")
        elif args.cmd == "change":
            orch.change(args.id, " ".join(args.words), actor="owner")
            print(f"Ticket {args.id} goes back for a new design.")
        elif args.cmd == "resume":
            orch.resume(args.id, actor="owner")
            print(f"Ticket {args.id} resumed.")
        elif args.cmd == "stop":
            orch.stop(args.id, actor="owner")
            print(f"Ticket {args.id} stopped.")
        print(inbox.render(store.open_inbox()))
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def run(env: Mapping[str, str]) -> None:  # pragma: no cover - wiring, exercised in deployment
    import psycopg

    from jarvis_server import config
    from jarvis_server.improve.github import GitHubApp
    from jarvis_server.improve.repo import GitRepo
    from jarvis_server.improve.sandbox import FileSandbox
    from jarvis_server.improve.store import PgStore

    logging.basicConfig(level=logging.INFO)
    work = Path(env.get("JARVIS_IMPROVE_WORK", "/work"))
    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        store = PgStore(conn)
        while missing := missing_keys(env, config.SECRETS_DIR, proxy_up=_proxy_up):
            sync_keys(store, missing)
            log.info("waiting for %s", ", ".join(str(i.key_name) for i in missing))
            time.sleep(300)
        sync_keys(store, [])
        app = GitHubApp(
            app_id=env["JARVIS_IMPROVE_APP_ID"],
            installation_id=env["JARVIS_IMPROVE_INSTALLATION_ID"],
            private_key_pem=(config.SECRETS_DIR / APP_KEY).read_text(),
            repo=REPO,
        )
        repo = GitRepo(
            origin=f"https://github.com/{REPO}.git",
            clone=Path("/tmp/improve-clone"),  # noqa: S108 - the improver's own tmpfs, not /work
            auth=app.git_auth_header,
        )
        sandbox = FileSandbox(work, repo, heartbeat=lambda job: store.heartbeat(job, LEASE_S))
        orch = Orchestrator(store, sandbox, repo, app, owner=OWNER, now=lambda: datetime.now(UTC))
        worker = f"improver-{os.getpid()}"
        while True:
            try:
                orch.sweep()
                while orch.tick(worker):
                    pass
            except Exception:
                log.exception("a stage failed; the job is marked and the loop goes on")
            time.sleep(30)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - entry point
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["run"]:
        run(os.environ)
        return 0
    import psycopg

    from jarvis_server import config
    from jarvis_server.improve.store import PgStore

    with psycopg.connect(config.app_dsn(), autocommit=True) as conn:
        return command(PgStore(conn), args)
