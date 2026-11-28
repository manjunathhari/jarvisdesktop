"""In-memory stand-ins for the stages' ports: no database, no sandbox, no git, no GitHub."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jarvis_server.improve.gate import fingerprint
from jarvis_server.improve.memory import MemoryStore
from jarvis_server.improve.ports import (
    Inspected,
    StageRequest,
    StageResult,
)
from jarvis_server.improve.review_loop import Pr

FakeStore = MemoryStore


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
