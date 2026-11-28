"""Each stage, run with a fake sandbox, moves the ticket correctly and records what happened."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis_server.improve import plan, stages
from jarvis_server.improve.inbox import Kind
from jarvis_server.improve.ports import StageRequest, StageResult
from jarvis_server.improve.review_loop import Comment, Pr
from jarvis_server.improve.tickets import State

from .fakes import FakeGitHub, FakeRepo, FakeSandbox, FakeStore, patch_for

SRC = "server/src/jarvis_server/brain/research.py"
TEST = "server/tests/test_research_depth.py"
DESIGN = f"""# Research answers are too thin

The research tool stops after one search.

```improve-plan
{{"files": ["{SRC}"], "tests": ["{TEST}"], "packages": [],
  "done_when": "A research answer cites three sources."}}
```
"""
NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
OWNER = "manjunathhari"


def ok(**kw: object) -> StageResult:
    base: dict[str, object] = {"ok": True, "cost_usd": 0.5, "turns": 5}
    base.update(kw)
    return StageResult(**base)  # type: ignore[arg-type]


SCRIPT: dict[str, object] = {
    "design": ok(design=DESIGN),
    "tests": ok(patch=patch_for(TEST)),
    "build": ok(patch=patch_for(TEST, SRC)),
    "review": ok(text="All good.\nVERDICT: PASS"),
}


class World:
    def __init__(self, **script: object) -> None:
        self.store = FakeStore()
        self.sandbox = FakeSandbox({**SCRIPT, **script})  # type: ignore[dict-item]
        self.repo = FakeRepo()
        self.github = FakeGitHub()
        self.now = NOW
        self.orch = stages.Orchestrator(
            self.store, self.sandbox, self.repo, self.github, owner=OWNER, now=lambda: self.now
        )
        self.tid = self.orch.open_ticket("you can't do research properly", {"turns": ["..."]})

    @property
    def ticket(self):  # type: ignore[no-untyped-def]
        return self.store.get(self.tid)

    def run(self, n: int = 1) -> None:
        for _ in range(n):
            assert self.orch.tick("w1"), "no job was queued"

    def approve(self) -> None:
        assert self.ticket.design_fingerprint is not None
        self.orch.approve(self.tid, self.ticket.design_fingerprint[:8], actor="owner")


def test_opening_a_ticket_queues_the_design() -> None:
    w = World()
    assert w.ticket.state is State.OPEN
    assert w.ticket.slug == "you-can-t-do-research-properly"
    assert w.store.queued() == ["design"]


def test_the_design_stage_waits_for_manj() -> None:
    w = World()
    w.run()
    t = w.ticket
    assert t.state is State.DESIGN_READY
    assert t.design == DESIGN
    assert t.design_fingerprint == plan.fingerprint(DESIGN)
    assert [i.kind for i in w.store.items] == [Kind.DECISION]
    assert w.store.queued() == []
    assert w.store.stages == [(w.tid, "design", 0.5, "PASSED")]


def test_a_design_without_a_good_plan_goes_back_with_the_reason() -> None:
    w = World(design=ok(design="# no plan here"))
    w.run()
    assert w.ticket.state is State.DESIGNING
    assert w.ticket.rounds_used == 1
    assert w.store.queued() == ["design"]
    assert "no improve-plan block" in w.ticket.feedback[0]


def test_approval_needs_the_right_fingerprint() -> None:
    w = World()
    w.run()
    with pytest.raises(stages.Refused, match="fingerprint"):
        w.orch.approve(w.tid, "deadbeef", actor="owner")
    w.approve()
    assert w.ticket.state is State.APPROVED
    assert w.store.queued() == ["tests"]
    assert not [i for i in w.store.items if i.kind is Kind.DECISION]


def test_a_changed_design_needs_a_new_yes() -> None:
    w = World()
    w.run()
    old = w.ticket.design_fingerprint
    w.orch.change(w.tid, "also handle PDFs", actor="owner")
    assert w.ticket.state is State.DESIGNING
    assert w.ticket.feedback == ("Manj asked for a change: also handle PDFs",)
    w.sandbox.script["design"] = ok(design=DESIGN.replace("one search", "a single search"))
    w.run()
    assert w.ticket.design_fingerprint != old
    with pytest.raises(stages.Refused):
        w.orch.approve(w.tid, (old or "")[:8], actor="owner")


def test_the_whole_way_to_a_pr_then_ready_then_watching() -> None:
    w = World()
    w.run()
    w.approve()
    w.run()  # tests
    assert TEST in w.ticket.test_fingerprints
    assert w.store.queued() == ["build"]
    w.run()  # build
    assert w.store.queued() == ["review"]
    w.run()  # review passes: push and PR
    t = w.ticket
    assert t.state is State.PR_OPEN
    assert w.repo.pushed == [("improve/1-you-can-t-do-research-properly", t.reviewed_sha)]
    assert t.pr_number == 9
    assert w.store.queued() == ["pr_watch"]

    w.github.prs[9] = Pr("open", False, t.reviewed_sha or "", None, "success", ())
    w.run()  # pr_watch: green, tell Manj once
    assert [i.kind for i in w.store.items] == [Kind.READY_TO_MERGE]
    assert w.ticket.told_ready
    w.run()
    assert len(w.store.items) == 1

    w.github.prs[9] = Pr("closed", True, t.reviewed_sha or "", t.reviewed_sha, "success", ())
    w.run()
    assert w.ticket.state is State.WATCHING
    assert w.ticket.watch_started_at == NOW
    assert not w.store.items


def _to_pr(w: World) -> None:
    w.run()
    w.approve()
    w.run(3)
    assert w.ticket.state is State.PR_OPEN


def test_the_build_stage_is_refused_for_a_file_the_plan_does_not_name() -> None:
    w = World(build=ok(patch=patch_for(TEST, SRC, "server/src/jarvis_server/brain/other.py")))
    w.run()
    w.approve()
    w.run(2)
    assert w.ticket.rounds_used == 1
    assert "is not named in the approved design" in w.ticket.feedback[0]
    assert w.store.queued() == ["build"]
    assert w.store.stages[-1][3] == "REFUSED"


def test_the_tests_stage_may_only_add_the_plan_s_tests() -> None:
    w = World(tests=ok(patch=patch_for(TEST, SRC)))
    w.run()
    w.approve()
    w.run()
    assert w.ticket.rounds_used == 1
    assert w.store.queued() == ["tests"]


def test_a_failed_review_goes_back_to_the_builder() -> None:
    w = World(review=ok(text="VERDICT: FAIL\nserver/x.py:3 no timeout"))
    w.run()
    w.approve()
    w.run(3)
    assert w.ticket.state is State.APPROVED
    assert w.ticket.feedback == ("The Reviewer: VERDICT: FAIL\nserver/x.py:3 no timeout",)
    assert w.store.queued() == ["build"]
    assert w.repo.pushed == []


def test_a_review_without_a_verdict_fails() -> None:
    w = World(review=ok(text="Looks fine I guess"))
    w.run()
    w.approve()
    w.run(3)
    assert w.repo.pushed == []
    assert w.store.queued() == ["build"]


def test_review_comments_from_manj_start_a_new_round() -> None:
    w = World()
    _to_pr(w)
    sha = w.ticket.reviewed_sha or ""
    w.github.prs[9] = Pr(
        "open", False, sha, None, "success", (Comment(7, OWNER, "Add a timeout", SRC, 40),)
    )
    w.run()
    t = w.ticket
    assert t.state is State.APPROVED
    assert t.handled_comments == frozenset({7})
    assert t.feedback == (f"Review comment on {SRC} line 40: Add a timeout",)
    assert w.store.queued() == ["build"]
    w.run(2)  # build, review: pushes again to the same branch
    assert w.ticket.state is State.PR_OPEN
    assert len(w.repo.pushed) == 2


def test_ci_failure_starts_a_new_round() -> None:
    w = World()
    _to_pr(w)
    w.github.prs[9] = Pr("open", False, w.ticket.reviewed_sha or "", None, "failure", ())
    w.run()
    assert w.ticket.state is State.APPROVED
    assert w.store.queued() == ["build"]


def test_pending_checks_look_again_later() -> None:
    w = World()
    _to_pr(w)
    w.github.prs[9] = Pr("open", False, w.ticket.reviewed_sha or "", None, "pending", ())
    w.run()
    assert w.store.jobs[-1]["stage"] == "pr_watch" and w.store.jobs[-1]["delay"] == 300


def test_a_closed_pr_stops_the_ticket() -> None:
    w = World()
    _to_pr(w)
    w.github.prs[9] = Pr("closed", False, w.ticket.reviewed_sha or "", None, "success", ())
    w.run()
    assert w.ticket.state is State.STOPPED


def test_a_merge_of_unreviewed_code_pauses_and_warns() -> None:
    w = World()
    _to_pr(w)
    w.github.prs[9] = Pr("closed", True, "f" * 40, "f" * 40, "success", ())
    w.run()
    assert w.ticket.state is State.PAUSED
    assert w.store.items[-1].kind is Kind.WARNING


def test_three_rounds_then_stop() -> None:
    w = World(review=ok(text="VERDICT: FAIL"))
    w.run()
    w.approve()
    w.run(1 + 2 * 3)  # tests, then three build and review rounds
    assert w.ticket.state is State.STOPPED
    assert w.store.items[-1].kind is Kind.STOPPED
    assert w.store.queued() == []


def test_a_stage_that_would_pass_the_ticket_cap_pauses_first() -> None:
    w = World(design=ok(design=DESIGN, cost_usd=6.0))
    w.run()
    w.approve()
    w.run()  # tests: spent 6.0 + tests cap 1.5 = 7.5, fine
    calls = len(w.sandbox.requests)
    w.run()  # build: 6.5 + 3.0 > 8.0
    assert w.ticket.state is State.PAUSED
    assert len(w.sandbox.requests) == calls
    assert w.store.items[-1].kind is Kind.PAUSED
    w.orch.resume(w.tid, actor="owner")
    assert w.ticket.state is State.APPROVED
    assert w.store.queued() == ["build"]


def test_a_sandbox_failure_is_recorded_and_retried_in_a_round() -> None:
    w = World(design=StageResult(ok=False, cost_usd=0.2, turns=40, error="max_turns"))
    w.run()
    assert w.store.stages[-1] == (w.tid, "design", 0.2, "FAILED")
    assert w.ticket.rounds_used == 1
    assert w.store.queued() == ["design"]


def test_a_blocked_site_lands_in_the_inbox() -> None:
    w = World(design=ok(design=DESIGN, blocked_urls=("https://example.org/x?k=1",)))
    w.run()
    assert Kind.BLOCKED_SITE in [i.kind for i in w.store.items]


def test_the_sandbox_gets_the_design_and_earlier_patches_but_no_keys() -> None:
    w = World()
    w.run()
    w.approve()
    w.run(2)
    build: StageRequest = w.sandbox.requests[-1]
    assert build.stage == "build"
    assert build.design == DESIGN
    assert build.patches == (patch_for(TEST),)
    assert "key" not in repr(build).lower()


def test_a_design_waits_fourteen_days_then_stops() -> None:
    w = World()
    w.run()
    w.now = NOW + timedelta(days=15)
    w.orch.sweep()
    assert w.ticket.state is State.STOPPED


def test_seven_quiet_days_close_the_ticket() -> None:
    w = World()
    _to_pr(w)
    sha = w.ticket.reviewed_sha or ""
    w.github.prs[9] = Pr("closed", True, sha, sha, "success", ())
    w.run()
    w.now = NOW + timedelta(days=7)
    w.orch.sweep()
    assert w.ticket.state is State.DONE


def test_the_complaint_coming_back_reopens_the_design() -> None:
    w = World()
    _to_pr(w)
    sha = w.ticket.reviewed_sha or ""
    w.github.prs[9] = Pr("closed", True, sha, sha, "success", ())
    w.run()
    w.orch.complaint_back(w.tid, "still thin today", actor="owner")
    assert w.ticket.state is State.DESIGNING
    assert w.store.queued() == ["design"]


def test_stop_by_manj() -> None:
    w = World()
    w.orch.stop(w.tid, actor="owner")
    assert w.ticket.state is State.STOPPED
    assert w.orch.tick("w1")  # the queued design job is dropped, not run
    assert w.sandbox.requests == []
