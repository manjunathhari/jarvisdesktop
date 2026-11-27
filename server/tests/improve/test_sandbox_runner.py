"""The sandbox runner's own steps: build the copy, word the task, and collect the change.

The agent itself is not run here (the dry run fakes it); the SDK test is skipped where the SDK
isn't installed.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from jarvis_server.improve import gate, handoff, plan, sandbox_runner, team
from jarvis_server.improve.ports import StageRequest

DESIGN = """# Better research

Prose.

```improve-plan
{"files": ["server/src/jarvis_server/brain/research.py"],
 "tests": ["server/tests/test_research_depth.py"],
 "packages": [], "done_when": "research answers cite three sources"}
```
"""


def _tar(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


BASE = _tar(
    {
        "README.md": "jarvis\n",
        "server/src/jarvis_server/brain/research.py": "DEPTH = 1\n",
    }
)


def _req(
    stage: str, patches: tuple[bytes, ...] = (), feedback: tuple[str, ...] = ()
) -> StageRequest:
    return StageRequest(
        job_id=1,
        ticket_id=4,
        stage=stage,
        base_sha="a" * 40,
        words="research is too thin",
        evidence={"turn": "the private words of turn 12"},
        design=None if stage == "design" else DESIGN,
        patches=patches,
        feedback=feedback,
    )


def test_the_copy_is_the_base_with_the_earlier_patches_and_the_design(tmp_path: Path) -> None:
    first = sandbox_runner.Copy.build(tmp_path / "a", BASE, _req("tests"))
    (first.root / "server/tests").mkdir(parents=True)
    (first.root / "server/tests/test_research_depth.py").write_text("def test(): ...\n")
    tests_patch = first.diff()
    assert b"server/tests/test_research_depth.py" in tests_patch

    copy = sandbox_runner.Copy.build(tmp_path / "b", BASE, _req("build", patches=(tests_patch,)))
    assert (copy.root / "server/tests/test_research_depth.py").exists()
    assert (copy.root / team.DESIGN_FILE).read_text() == DESIGN
    assert copy.diff() == tests_patch  # against the base: the design file is not a change


def test_the_change_is_collected_against_the_base_with_new_files(tmp_path: Path) -> None:
    copy = sandbox_runner.Copy.build(tmp_path / "c", BASE, _req("build"))
    (copy.root / "server/src/jarvis_server/brain/research.py").write_text("DEPTH = 3\n")
    (copy.root / "notes.txt").write_text("new\n")
    patch = copy.diff().decode()
    assert "-DEPTH = 1" in patch and "+DEPTH = 3" in patch
    assert "notes.txt" in patch


def test_a_git_config_the_agent_wrote_does_not_run_when_the_change_is_collected(
    tmp_path: Path,
) -> None:
    copy = sandbox_runner.Copy.build(tmp_path / "d", BASE, _req("build"))
    marker = tmp_path / "ran"
    (copy.root / ".git").mkdir(exist_ok=True)
    (copy.root / ".git" / "config").write_text(
        f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = /tmp\n"
    )
    (copy.root / ".gitattributes").write_text("* filter=evil diff=evil\n")
    (copy.root / "README.md").write_text("changed\n")
    patch = copy.diff().decode()
    assert not marker.exists()
    assert "+changed" in patch


def test_a_symlink_comes_back_as_a_symlink_for_the_gate_to_refuse(tmp_path: Path) -> None:
    copy = sandbox_runner.Copy.build(tmp_path / "e", BASE, _req("build"))
    (copy.root / "link").symlink_to("/etc/passwd")
    assert "new file mode 120000" in copy.diff().decode()


def test_the_design_stage_sees_the_evidence_and_later_stages_do_not() -> None:
    design = sandbox_runner.task(_req("design"))
    assert "research is too thin" in design and "the private words of turn 12" in design
    assert "improve-plan" in design
    build = sandbox_runner.task(_req("build", feedback=("CI failed: test_depth",)))
    assert "the private words of turn 12" not in build
    assert "research is too thin" in build
    assert "CI failed: test_depth" in build
    assert "server/src/jarvis_server/brain/research.py" in build


def test_the_hook_denies_what_the_guard_denies(tmp_path: Path) -> None:
    hook = sandbox_runner.guard_hook("build", cwd=tmp_path, plan=plan.parse(DESIGN))
    out = sandbox_runner.run_hook(
        hook, {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "README.md")}}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = sandbox_runner.run_hook(
        hook,
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "server/src/jarvis_server/brain/research.py")
            },
        },
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    out = sandbox_runner.run_hook(
        hook, {"tool_name": "Read", "tool_input": {}, "agent_type": "general-purpose"}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_runner_imports_without_the_server_s_dependencies() -> None:
    code = (
        "import sys\n"
        "for m in ('psycopg', 'cryptography', 'httpx', 'claude_agent_sdk'):\n"
        "    sys.modules[m] = None\n"
        "import jarvis_server.improve.sandbox_runner\n"
    )
    src = str(Path(sandbox_runner.__file__).parents[2])
    subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {src!r})\n" + code],
        check=True,
    )


def test_the_sdk_takes_the_options_the_team_builds(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    for stage in team.STAGES:
        p = None if stage == "design" else plan.parse(DESIGN)
        options = sandbox_runner.sdk_options(stage, cwd=tmp_path, plan=p)
        assert options.setting_sources == []
        assert options.hooks and "PreToolUse" in options.hooks
        assert not options.allowed_tools  # every call goes through the guard hook
        if stage == "design":
            assert set(options.agents or {}) == set(team.SUBAGENTS)


def test_the_runner_clears_a_planted_request_before_it_writes_the_result(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "request.json").write_text('{"stage": "design"}')
    sandbox_runner.finish(tmp_path, job_id=1, nonce="n", result=sandbox_runner.failed("boom"))
    assert not (tmp_path / "inbox").exists()
    assert handoff.read_result(tmp_path, job_id=1, nonce="n") is not None


def test_a_gate_still_refuses_what_the_copy_collected(tmp_path: Path) -> None:
    copy = sandbox_runner.Copy.build(tmp_path / "f", BASE, _req("build"))
    (copy.root / "server/Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (copy.root / "server/Dockerfile").write_text("FROM evil\n")
    assert gate.is_protected("server/Dockerfile")
    assert "server/Dockerfile" in copy.diff().decode()
