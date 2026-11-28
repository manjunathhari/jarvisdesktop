"""One sample ticket through the whole team: real git, the real hand-off and runner steps.

Only Claude (a scripted agent) and GitHub (a scripted PR) are stand-ins.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis_server.improve import dry_run
from jarvis_server.improve.tickets import State


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dry_run.Report:
    return dry_run.run(tmp_path_factory.mktemp("dry-run"))


def _git(origin: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(origin), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_ticket_goes_all_the_way_to_done(report: dry_run.Report) -> None:
    assert report.ticket.state is State.DONE
    states = [line for line in report.lines if "->" in line and "ticket 1" in line]
    order = ["DESIGNING", "DESIGN_READY", "APPROVED", "PR_OPEN", "WATCHING", "DONE"]
    assert all(any(f"-> {s}" in line for line in states) for s in order)


def test_main_is_never_touched_and_the_branch_has_the_change(report: dry_run.Report) -> None:
    assert _git(report.origin, "rev-parse", "main") == report.main_before
    files = _git(report.origin, "diff", "--name-only", "main", report.branch).splitlines()
    assert sorted(files) == sorted(dry_run.PLAN_FILES)
    code = _git(report.origin, "show", f"{report.branch}:{dry_run.SRC}")
    assert "ROUNDS = 3" in code and "Why three" in code  # the review comment was built in


def test_the_tests_were_red_before_the_build_and_green_after(report: dry_run.Report) -> None:
    text = "\n".join(report.lines)
    assert text.index("red on the base") < text.index("green after the change")


def test_the_guard_said_no_to_out_of_plan_work(report: dry_run.Report) -> None:
    denied = [line for line in report.lines if "guard: deny" in line]
    assert len(denied) >= 3


def test_only_the_owner_s_comment_went_back_to_the_builder(report: dry_run.Report) -> None:
    assert any("why three" in f for f in report.builder_feedback)
    assert not any("merge it now" in f for f in report.builder_feedback)


def test_manj_heard_once_that_it_was_ready_and_jarvis_never_merged(
    report: dry_run.Report,
) -> None:
    assert report.ready_told == 1
    assert set(report.github.calls) == {"open_or_update_pr", "snapshot"}


def test_the_runner_never_saw_the_nonce(report: dry_run.Report) -> None:
    assert report.agent_saw_inbox is False
