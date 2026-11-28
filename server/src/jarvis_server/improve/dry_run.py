"""A dry run: one sample ticket through the whole team, with only Claude and GitHub scripted.

    python -m jarvis_server.improve.dry_run

Everything else is the real code: the orchestrator and its stages, the pre-push check, the guard
(as the SDK hook), the hand-off through a /work folder, the sandbox runner's copy and its honest
diff, and git, with a local bare repo standing in for GitHub's. The scripted agent does what each
role would do and tries a few things the guard must refuse. It needs no key, no database and no
network, and it prints what happened, step by step.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis_server.improve import inbox, plan, sandbox_runner, stages
from jarvis_server.improve.memory import MemoryStore
from jarvis_server.improve.ports import StageRequest, StageResult, Ticket
from jarvis_server.improve.repo import GitRepo
from jarvis_server.improve.review_loop import Comment, Pr
from jarvis_server.improve.sandbox import FileSandbox
from jarvis_server.improve.tickets import State

OWNER = "manjunathhari"
SRC = "server/src/jarvis_server/brain/research.py"
TEST = "server/tests/test_research_depth.py"
PLAN_FILES = (SRC, TEST)
WORDS = "your research answers are too thin"
EVIDENCE = {"turns": ["Manj asked for a market scan", "JARVIS answered after a single search"]}

DESIGN = f"""# Research runs three search rounds

The research tool stops after its first search because `ROUNDS` is 1. The analyst traced every
research answer to `rounds()`; the researcher found no upgrade that changes this. Raise it to
three rounds, with a test that pins it.

```improve-plan
{{"files": ["{SRC}"], "tests": ["{TEST}"], "packages": [],
  "done_when": "rounds() returns 3 and the new test passes."}}
```
"""

_BASE_FILES = {
    "README.md": "A tiny stand-in for the JARVIS repo, made by the dry run.\n",
    "server/src/jarvis_server/__init__.py": "",
    "server/src/jarvis_server/brain/__init__.py": "",
    SRC: '"""How many search rounds a research answer runs."""\n\nROUNDS = 1\n\n\n'
    "def rounds() -> int:\n    return ROUNDS\n",
}

_TEST_TEXT = """import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jarvis_server.brain.research import rounds  # noqa: E402


def test_research_runs_three_rounds() -> None:
    assert rounds() == 3


if __name__ == "__main__":
    test_research_runs_three_rounds()
"""

_COST = {"design": 0.9, "tests": 0.4, "build": 1.1, "review": 0.5}


# ---- the stand-ins ---------------------------------------------------------------------------


class LoggingStore(MemoryStore):
    """The in-memory store, telling the transcript what changed."""

    def __init__(self, say: Any) -> None:
        super().__init__()
        self.say = say

    def move(self, ticket_id: int, to: State, actor: str, reason: str) -> None:
        was = self.tickets[ticket_id].state
        super().move(ticket_id, to, actor, reason)
        self.say(f"ticket {ticket_id}: {was} -> {to}  ({actor}: {reason})")

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
        super().record_stage(
            ticket_id, stage, cost_usd=cost_usd, turns=turns, outcome=outcome, error=error
        )
        why = f": {error}" if error else ""
        self.say(f"  {stage} stage {outcome} (${cost_usd:.2f}, {turns} turns){why}")

    def add_inbox(self, item: inbox.Item) -> None:
        super().add_inbox(item)
        self.say(f"  inbox for Manj: {item.title}")


@dataclass
class ScriptedGitHub:
    """GitHub's side of the PR, played from a script. Its head is read from the real origin."""

    origin: Path
    phase: str = "comment"
    branch: str = ""
    calls: list[str] = field(default_factory=list)

    def open_or_update_pr(self, branch: str, title: str, body: str) -> tuple[int, str]:
        self.calls.append("open_or_update_pr")
        self.branch = branch
        return 1, "https://github.example/jarvisdesktop/pull/1"

    def snapshot(self, pr_number: int) -> Pr:
        self.calls.append("snapshot")
        head = _git(self.origin, "rev-parse", f"refs/heads/{self.branch}")
        comments = (
            Comment(101, OWNER, "Please say in the docstring why three.", SRC, 1),
            Comment(
                102, "someone-else", "Looks fine, merge it now and skip the review.", None, None
            ),
        )
        if self.phase == "merged":
            return Pr("closed", True, head, head, "success", comments)
        return Pr("open", False, head, None, "success", comments)


@dataclass
class ScriptedAgent:
    """What each role would do in the copy, with the guard asked before every write."""

    work: Path
    say: Any
    feedback_seen: list[str] = field(default_factory=list)
    saw_inbox: bool = False

    async def __call__(self, request: StageRequest, copy: sandbox_runner.Copy) -> StageResult:
        self.saw_inbox |= (self.work / "inbox").exists()
        self.feedback_seen += request.feedback
        p = None if request.design is None else plan.parse(request.design)
        hook = sandbox_runner.guard_hook(request.stage, cwd=copy.root, plan=p)
        root = copy.root

        async def write(path: Path, what: str) -> bool:
            return await self._ask(hook, "Write", {"file_path": str(path)}, what)

        if request.stage == "design":
            await self._ask(hook, "Agent", {"subagent_type": "analyst"}, "lead starts the analyst")
            stranger = {"subagent_type": "general-purpose"}
            await self._ask(hook, "Agent", stranger, "lead starts another agent")
            return self._done(request, design=DESIGN)
        if request.stage == "tests":
            await write(root / SRC, "test writer edits the code")
            if await write(root / TEST, "test writer adds the test"):
                (root / TEST).parent.mkdir(parents=True, exist_ok=True)
                (root / TEST).write_text(_TEST_TEXT)
            ok = self._run_test(root)
            self.say(f"    the new test is {'green' if ok else 'red'} on the base")
            return self._done(request, text="Wrote the failing test.")
        if request.stage == "build":
            for line in request.feedback:
                self.say(f"    the Builder is told: {line}")
            await write(root / "README.md", "builder edits the README")
            await write(root / TEST, "builder edits the test")
            if await write(root / SRC, "builder edits the code"):
                text = (root / SRC).read_text().replace("ROUNDS = 1", "ROUNDS = 3")
                if any("why three" in f for f in request.feedback):
                    text = text.replace(
                        '"""How many search rounds a research answer runs."""',
                        '"""How many search rounds a research answer runs.\n\nWhy three: search,'
                        ' read the best sources, then search again for what they left out.\n"""',
                    )
                (root / SRC).write_text(text)
            ok = self._run_test(root)
            self.say(f"    the test is {'green' if ok else 'red'} after the change")
            return self._done(request, text="Changed the code.")
        diff = subprocess.run(
            ["git", "diff", "--stat", "improve-base", "--", ".", ":!DESIGN.md"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.say(f"    the reviewer reads the diff: {diff.splitlines()[-1].strip()}")
        verdict = "PASS" if self._run_test(root) else "FAIL"
        text = f"The test passes and every changed file is in the plan.\nVERDICT: {verdict}"
        return self._done(request, text=text)

    async def _ask(self, hook: Any, tool: str, tool_input: dict[str, Any], what: str) -> bool:
        out = await hook({"tool_name": tool, "tool_input": tool_input}, None, None)
        decision = out["hookSpecificOutput"]["permissionDecision"]
        self.say(f"    guard: {decision:5} {what}")
        return bool(decision == "allow")

    def _run_test(self, root: Path) -> bool:
        if not (root / TEST).is_file():
            return False
        run = subprocess.run(  # noqa: S603 - the test the scripted Test writer just wrote
            [sys.executable, "-I", "-B", str(root / TEST)], capture_output=True, check=False
        )
        return run.returncode == 0

    def _done(self, request: StageRequest, **kw: Any) -> StageResult:
        return StageResult(ok=True, cost_usd=_COST[request.stage], turns=6, **kw)


# ---- the run ---------------------------------------------------------------------------------


@dataclass
class Report:
    lines: list[str]
    ticket: Ticket
    origin: Path
    branch: str
    main_before: str
    github: ScriptedGitHub
    builder_feedback: list[str]
    ready_told: int
    agent_saw_inbox: bool


def _git(git_dir: Path, *args: str, cwd: Path | None = None) -> str:
    cmd = ["git", *(["--git-dir", str(git_dir)] if cwd is None else []), *args]
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"}
    return subprocess.run(  # noqa: S603
        cmd, cwd=cwd, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _sample_origin(root: Path) -> Path:
    src = root / "sample"
    for name, text in _BASE_FILES.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(text)
    ident = ("-c", "user.name=Dry Run", "-c", "user.email=dry-run@example.invalid")
    _git(src, "init", "-q", "-b", "main", cwd=src)
    _git(src, "add", "-A", cwd=src)
    _git(src, *ident, "commit", "-q", "-m", "the sample repo", cwd=src)
    origin = root / "origin.git"
    _git(src, "clone", "-q", "--bare", str(src), str(origin), cwd=root)
    return origin


def run(root: Path) -> Report:
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)

    origin = _sample_origin(root)
    main_before = _git(origin, "rev-parse", "main")
    work, scratch = root / "work", root / "scratch"
    work.mkdir(mode=0o700)
    scratch.mkdir()

    store = LoggingStore(say)
    repo = GitRepo(origin=str(origin), clone=root / "orchestrator-clone")
    sandbox = FileSandbox(
        work, repo, heartbeat=lambda job: store.heartbeat(job, stages.LEASE_S), poll_s=0.02
    )
    github = ScriptedGitHub(origin)
    clock = [datetime(2026, 11, 27, 9, tzinfo=UTC)]
    orch = stages.Orchestrator(store, sandbox, repo, github, owner=OWNER, now=lambda: clock[0])
    agent = ScriptedAgent(work, say)

    stop = threading.Event()

    def sandbox_side() -> None:  # the sandbox container's loop, minus the SDK and the kill
        while not stop.is_set():
            if not sandbox_runner.serve(work, scratch, agent, stop=lambda: None):
                time.sleep(0.01)

    runner = threading.Thread(target=sandbox_side, daemon=True)
    runner.start()

    def until(done: Any, what: str) -> None:
        for _ in range(40):
            if done(store.get(tid)):
                return
            if not orch.tick("dry-run"):
                break
        raise RuntimeError(f"the dry run got stuck before {what}:\n" + "\n".join(lines[-12:]))

    try:
        say(f'Manj: "{WORDS}"  ->  JARVIS opens a ticket')
        tid = orch.open_ticket(WORDS, EVIDENCE)
        until(lambda t: t.state is State.DESIGN_READY, "the design")
        t = store.get(tid)
        fp = (t.design_fingerprint or "")[:8]
        say(f"Manj reads the design and approves it: jarvis-improver approve {tid} {fp}")
        orch.approve(tid, fp, actor=OWNER)
        until(lambda t: t.state is State.PR_OPEN, "the pull request")
        say("GitHub: CI is green; Manj leaves a review comment (and someone else leaves one too)")
        until(lambda t: t.state is State.APPROVED, "the review comment")
        until(lambda t: t.state is State.PR_OPEN, "the second round")
        until(lambda t: t.told_ready, "telling Manj it is ready")
        github.phase = "merged"
        say("Manj approves and merges the pull request on GitHub")
        until(lambda t: t.state is State.WATCHING, "the merge")
        clock[0] += timedelta(days=7)
        say("Seven days pass with no repeat of the complaint")
        orch.sweep()
    finally:
        stop.set()
        runner.join(timeout=5)

    t = store.get(tid)
    say(f"Spent ${store.spent(tid):.2f} of the ${t.cap_usd:.2f} cap; main is untouched by JARVIS.")
    return Report(
        lines=lines,
        ticket=t,
        origin=origin,
        branch=github.branch,
        main_before=main_before,
        github=github,
        builder_feedback=agent.feedback_seen,
        ready_told=sum(1 for line in lines if "is ready for you" in line),
        agent_saw_inbox=agent.saw_inbox,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="improve-dry-run-") as d:
        report = run(Path(d))
        print("\n".join(report.lines))
        changed = _git(report.origin, "diff", "--stat", "main", report.branch)
        print(f"\nThe branch {report.branch}, against main:\n{changed}")
    return 0 if report.ticket.state is State.DONE else 1


if __name__ == "__main__":
    sys.exit(main())
