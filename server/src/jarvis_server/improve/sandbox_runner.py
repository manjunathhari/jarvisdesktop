"""The sandbox side: take a stage's request, run its agent on a copy, hand back the change.

Runs as the sandbox container's entry point (`python -m jarvis_server.improve.sandbox_runner`), one
stage per container start. It imports only the standard library, the team's own stdlib-only modules
and, inside `main`, `claude_agent_sdk`: the sandbox's venv has nothing else.

What it does, in order:
1. waits for /work/inbox, reads the request into memory and deletes it, so the agent never sees the
   nonce or anything meant for the orchestrator;
2. builds the copy in /tmp (a tmpfs that is empty again at the next start): the base commit's
   archive, the earlier stages' patches, and the approved design as DESIGN.md;
3. runs the stage's agent with the Claude Agent SDK, every tool call going through `guard`;
4. stops every other process in the container, so nothing the agent started can touch what follows;
5. collects the change against the base with a fresh git directory of its own (the agent can write
   the copy's .git, so that one is never trusted), clears anything planted in /work/inbox, writes
   /work/outbox with the nonce, and exits.

The orchestrator still treats the patch as untrusted: it inspects it in its own clone and runs the
pre-push check before anything is pushed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis_server.improve import guard, handoff, plan, team
from jarvis_server.improve.plan import Plan
from jarvis_server.improve.ports import StageRequest, StageResult

WORK = Path("/work")
SCRATCH = Path("/tmp")  # noqa: S108 - the sandbox's own tmpfs, empty at every start
_BASE_TAG = "improve-base"
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "JARVIS improve",
    "GIT_AUTHOR_EMAIL": "improve@jarvis.invalid",
    "GIT_COMMITTER_NAME": "JARVIS improve",
    "GIT_COMMITTER_EMAIL": "improve@jarvis.invalid",
}
_GIT_SAFE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.autocrlf=false",
)

Hook = Callable[[Mapping[str, Any], str | None, Any], Coroutine[Any, Any, dict[str, Any]]]


def _git(
    *args: str, git_dir: Path | None = None, work_tree: Path, stdin: bytes | None = None
) -> bytes:
    cmd = ["git", *_GIT_SAFE]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir), "--work-tree", str(work_tree)]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV}
    done = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
        [*cmd, *args], cwd=work_tree, input=stdin, capture_output=True, env=env, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.decode(errors='replace')[-500:]}")
    return done.stdout


def _extract(base_tar: bytes, dest: Path) -> None:
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as f:
        f.write(base_tar)
        f.seek(0)
        with tarfile.open(fileobj=f) as tar:
            tar.extractall(dest, filter="data")


def _base_commit(git_dir: Path | None, tree: Path, patches: tuple[bytes, ...]) -> None:
    """Commit and tag the base, then apply the earlier stages' patches on top."""
    _git("init", "-q", git_dir=git_dir, work_tree=tree)
    _git("add", "-A", git_dir=git_dir, work_tree=tree)
    _git("commit", "-q", "--allow-empty", "-m", "base", git_dir=git_dir, work_tree=tree)
    _git("tag", _BASE_TAG, git_dir=git_dir, work_tree=tree)
    for p in patches:
        if p:
            _git(
                "apply",
                "--binary",
                "--whitespace=nowarn",
                "-",
                git_dir=git_dir,
                work_tree=tree,
                stdin=p,
            )
    _git("add", "-A", git_dir=git_dir, work_tree=tree)
    _git("commit", "-q", "--allow-empty", "-m", "earlier stages", git_dir=git_dir, work_tree=tree)


@dataclass(frozen=True)
class Copy:
    """The agent's copy of the repo, and what it needs to collect the change honestly later."""

    root: Path
    scratch: Path
    base_tar: bytes
    patches: tuple[bytes, ...]

    @classmethod
    def build(cls, scratch: Path, base_tar: bytes, request: StageRequest) -> Copy:
        root = scratch / "repo"
        _extract(base_tar, root)
        _base_commit(None, root, request.patches)  # the agent's own .git, for its convenience
        if request.design is not None:
            (root / team.DESIGN_FILE).write_text(request.design)
        return cls(root, scratch, base_tar, request.patches)

    def diff(self) -> bytes:
        """Everything changed against the base, from a git directory the agent never saw."""
        fresh = Path(tempfile.mkdtemp(dir=self.scratch, prefix="collect-"))
        try:
            base_tree, git_dir = fresh / "base", fresh / "git"
            _extract(self.base_tar, base_tree)
            _base_commit(git_dir, base_tree, self.patches)
            _git("add", "-A", "--", ".", git_dir=git_dir, work_tree=self.root)
            return _git(
                "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
                _BASE_TAG, "--", ".", f":(exclude){team.DESIGN_FILE}",
                git_dir=git_dir, work_tree=self.root,
            )  # fmt: skip
        finally:
            shutil.rmtree(fresh, ignore_errors=True)


# ---- the task each stage is given -----------------------------------------------------------

_PLAN_BLOCK = (
    "End DESIGN.md with one fenced block marked improve-plan holding JSON with exactly these keys:"
    ' "files" (the repo paths the Builder may change), "tests" (the new test files, under'
    ' server/tests, crates/*/tests, app/src-tauri/tests or evals), "packages" (a list of'
    ' {"name", "licence"} for anything new to install, usually empty) and "done_when" (one'
    " sentence a reviewer can check)."
)


def task(request: StageRequest) -> str:
    """The first message of the stage's agent. Only the design stage sees the private evidence."""
    lines = [f"Manj's complaint, in his words: {request.words}"]
    p = None if request.design is None else plan.parse(request.design)
    if request.stage == "design":
        lines += [
            "",
            "What JARVIS saw (private: use it to understand the problem, never quote it):",
            json.dumps(dict(request.evidence), indent=2, ensure_ascii=False),
            "",
            f"Write the design as {team.DESIGN_FILE} in the copy's root. Ask the analyst to find"
            " the cause in the code and the researcher to check releases and upgrades that bear on"
            " it. " + _PLAN_BLOCK,
        ]
    elif p is not None:
        listed = ", ".join(p.tests) if request.stage == "tests" else ", ".join(p.files)
        what = {
            "tests": f"Write the failing tests the approved design asks for, in: {listed}. Run"
            " them and show that they fail for the reason the design names.",
            "build": f"Make the failing tests pass, changing only: {listed}. Run the full suite"
            " and the linters and report their real output.",
            "review": f"Review the change: run `git diff {_BASE_TAG} -- . ':!{team.DESIGN_FILE}'`"
            " and the tests. End with VERDICT: PASS or VERDICT: FAIL on a line of its own.",
        }[request.stage]
        lines += ["", f"The approved design is {team.DESIGN_FILE} in the copy's root.", what]
        lines += ["", f"Done when: {p.done_when}"]
    if request.feedback:
        lines += ["", "From earlier rounds and the review:", *(f"- {f}" for f in request.feedback)]
    return "\n".join(lines)


# ---- the guard, as the SDK's PreToolUse hook -------------------------------------------------


def guard_hook(stage: str, *, cwd: Path, plan: Plan | None) -> Hook:
    async def hook(
        data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        role = guard.role_for(stage, data.get("agent_type"))
        if role is None:
            decision = guard.Decision(False, f"{data.get('agent_type')} is not one of the team")
        else:
            decision = guard.decide(
                role, str(data.get("tool_name")), data.get("tool_input") or {}, cwd=cwd, plan=plan
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if decision.allow else "deny",
                "permissionDecisionReason": decision.reason or "allowed by the team's guard",
            }
        }

    return hook


def run_hook(hook: Hook, data: Mapping[str, Any]) -> dict[str, Any]:
    return asyncio.run(hook(data, None, None))


def sdk_options(stage: str, *, cwd: Path, plan: Plan | None) -> Any:
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher

    opts = team.stage_options(stage, cwd=cwd, plan=plan)
    if opts["skills"] and "Skill" not in opts["tools"]:
        opts["tools"] = [*opts["tools"], "Skill"]
    if "agents" in opts:
        opts["agents"] = {name: AgentDefinition(**spec) for name, spec in opts["agents"].items()}
    opts = {k: v for k, v in opts.items() if v is not None}
    hook: Any = guard_hook(stage, cwd=cwd, plan=plan)  # the SDK's typed hook input is a dict
    return ClaudeAgentOptions(
        **opts, hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}
    )


async def _run_agent(request: StageRequest, copy: Copy) -> StageResult:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    p = None if request.design is None else plan.parse(request.design)
    options = sdk_options(request.stage, cwd=copy.root, plan=p)
    final: ResultMessage | None = None
    last_text = ""
    async for message in query(prompt=task(request), options=options):
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            texts = [b.text for b in message.content if isinstance(b, TextBlock)]
            if texts:
                last_text = "\n".join(texts)
        elif isinstance(message, ResultMessage):
            final = message
    if final is None:
        return failed("the agent ended without a result")
    cost = float(final.total_cost_usd or 0.0)
    if final.is_error:
        return StageResult(ok=False, cost_usd=cost, turns=final.num_turns, error=final.subtype)
    design = None
    if request.stage == "design":
        path = copy.root / team.DESIGN_FILE
        design = path.read_text() if path.is_file() and not path.is_symlink() else None
    return StageResult(
        ok=True, cost_usd=cost, turns=final.num_turns, text=final.result or last_text, design=design
    )


def failed(error: str, cost_usd: float = 0.0) -> StageResult:
    return StageResult(ok=False, cost_usd=cost_usd, turns=0, error=error)


def finish(work: Path, *, job_id: int, nonce: str, result: StageResult) -> None:
    handoff.clear_inbox(work)
    try:
        handoff.write_result(work, job_id=job_id, nonce=nonce, result=result)
    except handoff.TooBig as e:
        handoff.write_result(
            work, job_id=job_id, nonce=nonce, result=failed(str(e), result.cost_usd)
        )


def _not_dumpable() -> None:
    """Keep this process's memory and environment out of the agent's reach (same user id)."""
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0


def _stop_everything_else() -> None:
    """SIGKILL every other process this user may signal: whatever the agent left running."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(-1, signal.SIGKILL)


def main(work: Path = WORK, scratch: Path = SCRATCH, poll_s: float = 2.0) -> int:
    _not_dumpable()
    taken = handoff.take_request(work)
    while taken is None:
        time.sleep(poll_s)
        taken = handoff.take_request(work)
    request, base_tar, nonce = taken
    try:
        copy = Copy.build(Path(tempfile.mkdtemp(dir=scratch, prefix="stage-")), base_tar, request)
        result = asyncio.run(_run_agent(request, copy))
        _stop_everything_else()
        if result.ok and request.stage in ("tests", "build"):
            result = StageResult(**{**result.__dict__, "patch": copy.diff()})
    except Exception as e:  # report it; the orchestrator decides what a failure costs
        _stop_everything_else()
        result = failed(f"{type(e).__name__}: {e}"[:2000])
    finish(work, job_id=request.job_id, nonce=nonce, result=result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
