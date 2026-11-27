"""In-memory stand-ins for the stages' ports: no database, no sandbox, no git, no GitHub."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis_server.improve import inbox, tickets
from jarvis_server.improve.gate import fingerprint
from jarvis_server.improve.ports import (
    Inspected,
    Job,
    StageRequest,
    StageResult,
    Ticket,
)
from jarvis_server.improve.review_loop import Pr
from jarvis_server.improve.tickets import State


class IllegalMove(Exception):
    pass


@dataclass
class FakeStore:
    tickets: dict[int, Ticket] = field(default_factory=dict)
    moves: list[tuple[int, State, State, str]] = field(default_factory=list)
    stages: list[tuple[int, str, float, str]] = field(default_factory=list)
    items: list[inbox.Item] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)

    def in_state(self, *states: State) -> list[Ticket]:
        return [t for t in self.tickets.values() if t.state in states]

    def open_ticket(self, words: str, slug: str, evidence: Mapping[str, Any]) -> int:
        tid = len(self.tickets) + 1
        self.tickets[tid] = Ticket(tid, words, slug, dict(evidence), State.OPEN)
        return tid

    def get(self, ticket_id: int) -> Ticket:
        return self.tickets[ticket_id]

    def move(self, ticket_id: int, to: State, actor: str, reason: str) -> None:
        t = self.tickets[ticket_id]
        if not tickets.can_move(t.state, to, paused_from=t.paused_from):
            raise IllegalMove(f"{t.state} -> {to}")
        if to is State.APPROVED and t.state is State.DESIGN_READY:
            assert t.approved_fingerprint == t.design_fingerprint
        paused_from = t.state if to is State.PAUSED else None
        self.tickets[ticket_id] = dataclasses.replace(t, state=to, paused_from=paused_from)
        self.moves.append((ticket_id, t.state, to, actor))

    def update(self, ticket_id: int, **fields: Any) -> None:
        self.tickets[ticket_id] = dataclasses.replace(self.tickets[ticket_id], **fields)

    def use_round(self, ticket_id: int) -> int:
        used = self.tickets[ticket_id].rounds_used + 1
        self.update(ticket_id, rounds_used=used)
        return used

    def record_stage(
        self,
        ticket_id: int,
        stage: str,
        *,
        cost_usd: float,
        turns: int,
        outcome: str,
        error: str | None,
    ) -> None:
        self.stages.append((ticket_id, stage, cost_usd, outcome))

    def spent(self, ticket_id: int) -> float:
        return sum(c for t, _, c, _ in self.stages if t == ticket_id)

    def add_inbox(self, item: inbox.Item) -> None:
        self.items.append(item)

    def open_inbox(self) -> list[inbox.Item]:
        return list(self.items)

    def resolve_inbox(self, ticket_id: int, kind: inbox.Kind) -> None:
        self.items = [i for i in self.items if not (i.ticket_id == ticket_id and i.kind is kind)]

    def resolve_key(self, key_name: str) -> None:
        self.items = [i for i in self.items if i.key_name != key_name]

    def enqueue(self, ticket_id: int, stage: str, *, delay_s: int = 0) -> int:
        self.jobs.append(
            {
                "id": len(self.jobs) + 1,
                "ticket": ticket_id,
                "stage": stage,
                "delay": delay_s,
                "status": "QUEUED",
            }
        )
        return len(self.jobs)

    def claim(self, worker: str, lease_s: int) -> Job | None:
        for j in self.jobs:
            if j["status"] == "QUEUED":
                j["status"] = "RUNNING"
                return Job(j["id"], j["ticket"], j["stage"], 1)
        return None

    def heartbeat(self, job_id: int, lease_s: int) -> None:
        pass

    def finish(self, job_id: int) -> None:
        self.jobs[job_id - 1]["status"] = "DONE"

    def fail(self, job_id: int, error: str) -> None:
        self.jobs[job_id - 1]["status"] = "FAILED"
        self.jobs[job_id - 1]["error"] = error

    def queued(self) -> list[str]:
        return [j["stage"] for j in self.jobs if j["status"] == "QUEUED"]


@dataclass
class FakeSandbox:
    """Answers each stage from a script: stage name -> result, or a function of the request."""

    script: dict[str, StageResult | Callable[[StageRequest], StageResult]]
    requests: list[StageRequest] = field(default_factory=list)

    def run(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        answer = self.script[request.stage]
        return answer(request) if callable(answer) else answer


def patch_for(*files: str) -> bytes:
    """A stand-in patch: the fake repo reads the file names from it."""
    return "\n".join(f"FILE {f}" for f in files).encode()


@dataclass
class FakeRepo:
    sha: str = "0" * 40
    pushed: list[tuple[str, str]] = field(default_factory=list)
    contents: dict[str, bytes] = field(default_factory=dict)

    def base_sha(self) -> str:
        return self.sha

    def archive(self, sha: str, dest: Path) -> None:
        pass

    def inspect(self, sha: str, patch: bytes, wanted: Sequence[str]) -> Inspected:
        files = tuple(line[5:] for line in patch.decode().splitlines() if line.startswith("FILE "))
        hashes = {f: fingerprint(self.contents.get(f, f.encode())) for f in wanted if f in files}
        return Inspected(files, (), {}, hashes, patch.decode())

    def push(self, sha: str, patch: bytes, branch: str, message: str) -> str:
        assert branch.startswith("improve/")
        head = f"{len(self.pushed) + 1:040d}"
        self.pushed.append((branch, head))
        return head


@dataclass
class FakeGitHub:
    prs: dict[int, Pr] = field(default_factory=dict)
    opened: list[tuple[str, str]] = field(default_factory=list)

    def open_or_update_pr(self, branch: str, title: str, body: str) -> tuple[int, str]:
        self.opened.append((branch, title))
        return 9, "https://github.com/manjunathhari/jarvisdesktop/pull/9"

    def snapshot(self, pr_number: int) -> Pr:
        return self.prs[pr_number]
