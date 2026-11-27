"""What the stages need from the outside world, as small interfaces.

The orchestrator wires the real ones (store.PgStore, sandbox.FileSandbox, repo.GitRepo,
github.GitHubApp); the tests and the dry run wire fakes. None of these runs an agent tool in the
orchestrator: only the sandbox runs agents.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from jarvis_server.improve import inbox
from jarvis_server.improve.review_loop import Pr
from jarvis_server.improve.tickets import State


@dataclass(frozen=True)
class Ticket:
    id: int
    words: str
    slug: str
    evidence: Mapping[str, Any]
    state: State
    paused_from: State | None = None
    rounds_used: int = 0
    design: str | None = None
    design_fingerprint: str | None = None
    approved_fingerprint: str | None = None
    design_ready_at: datetime | None = None
    base_sha: str | None = None  # the main commit this round's patches are against
    cap_usd: float = 8.0
    paused_stage: str | None = None
    test_fingerprints: Mapping[str, str] = field(default_factory=dict)
    tests_patch: bytes | None = None
    build_patch: bytes | None = None
    feedback: tuple[str, ...] = ()
    pr_number: int | None = None
    pr_url: str | None = None
    reviewed_sha: str | None = None
    handled_comments: frozenset[int] = frozenset()
    told_ready: bool = False
    watch_started_at: datetime | None = None


@dataclass(frozen=True)
class Job:
    id: int
    ticket_id: int
    stage: str
    attempts: int


class Store(Protocol):
    def open_ticket(self, words: str, slug: str, evidence: Mapping[str, Any]) -> int: ...
    def get(self, ticket_id: int) -> Ticket: ...
    def in_state(self, *states: State) -> list[Ticket]: ...
    def move(self, ticket_id: int, to: State, actor: str, reason: str) -> None: ...
    def update(self, ticket_id: int, **fields: Any) -> None: ...
    def use_round(self, ticket_id: int) -> int: ...
    def record_stage(
        self,
        ticket_id: int,
        stage: str,
        *,
        cost_usd: float,
        turns: int,
        outcome: str,
        error: str | None,
    ) -> None: ...
    def spent(self, ticket_id: int) -> float: ...
    def add_inbox(self, item: inbox.Item) -> None: ...
    def open_inbox(self) -> list[inbox.Item]: ...
    def resolve_inbox(self, ticket_id: int, kind: inbox.Kind) -> None: ...
    def resolve_key(self, key_name: str) -> None: ...
    def enqueue(self, ticket_id: int, stage: str, *, delay_s: int = 0) -> int: ...
    def claim(self, worker: str, lease_s: int) -> Job | None: ...
    def heartbeat(self, job_id: int, lease_s: int) -> None: ...
    def finish(self, job_id: int) -> None: ...
    def fail(self, job_id: int, error: str) -> None: ...


@dataclass(frozen=True)
class StageRequest:
    job_id: int
    ticket_id: int
    stage: str
    base_sha: str
    words: str
    evidence: Mapping[str, Any]
    design: str | None
    patches: tuple[bytes, ...]  # applied to the base, in order, before the agent starts
    feedback: tuple[str, ...]


@dataclass(frozen=True)
class StageResult:
    ok: bool
    cost_usd: float
    turns: int
    text: str = ""  # the agent's final message (the Reviewer's verdict is in it)
    design: str | None = None  # DESIGN.md, after the design stage
    patch: bytes = b""  # everything the stage changed, against base_sha
    blocked_urls: tuple[str, ...] = ()
    error: str | None = None


class Sandbox(Protocol):
    def run(self, request: StageRequest) -> StageResult: ...


@dataclass(frozen=True)
class Inspected:
    """A patch as seen from the orchestrator's own clone, after `git apply` there."""

    changed: tuple[str, ...]
    special: tuple[str, ...]  # symlinks, submodules: always refused
    lock_texts: Mapping[str, tuple[str, str]]
    hashes: Mapping[str, str]  # fingerprint of each file in `wanted`, if it exists
    diff: str


class Repo(Protocol):
    def base_sha(self) -> str: ...
    def archive(self, sha: str, dest: Path) -> None: ...
    def inspect(self, sha: str, patch: bytes, wanted: Sequence[str]) -> Inspected: ...
    def push(self, sha: str, patch: bytes, branch: str, message: str) -> str: ...


class GitHub(Protocol):
    def open_or_update_pr(self, branch: str, title: str, body: str) -> tuple[int, str]: ...
    def snapshot(self, pr_number: int) -> Pr: ...
