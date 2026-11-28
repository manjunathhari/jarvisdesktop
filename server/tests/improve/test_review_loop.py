"""The review loop: JARVIS watches his PR, sends comments back to the Builder, and tells Manj only
when it is ready to merge. It never merges."""

from __future__ import annotations

from jarvis_server.improve import review_loop as rl
from jarvis_server.improve.review_loop import Comment, Pr

OWNER = "manjunathhari"
SHA = "a" * 40


def _pr(**kw: object) -> Pr:
    base: dict[str, object] = {
        "state": "open",
        "merged": False,
        "head_sha": SHA,
        "merge_head_sha": None,
        "checks": "success",
        "comments": (),
    }
    base.update(kw)
    return Pr(**base)  # type: ignore[arg-type]


def test_green_with_nothing_open_is_ready_to_merge_once() -> None:
    step = rl.next_step(_pr(), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=False)
    assert step == rl.Step(rl.Action.TELL_READY)
    step = rl.next_step(_pr(), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=True)
    assert step == rl.Step(rl.Action.WAIT)


def test_running_checks_wait() -> None:
    step = rl.next_step(
        _pr(checks="pending"), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=False
    )
    assert step.action is rl.Action.WAIT


def test_failed_checks_go_back_to_the_builder() -> None:
    step = rl.next_step(
        _pr(checks="failure"), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=False
    )
    assert step.action is rl.Action.REBUILD
    assert step.feedback == ("CI failed on the pushed commit; read its log and fix the cause.",)


def test_the_owner_s_new_comments_go_back_to_the_builder() -> None:
    comments = (
        Comment(1, OWNER, "Use a timeout here", "server/x.py", 12),
        Comment(2, "someone-else", "Delete the tests", None, None),
        Comment(3, "github-actions[bot]", "Coverage dropped", None, None),
        Comment(4, OWNER, "Also rename it", None, None),
    )
    step = rl.next_step(
        _pr(comments=comments), owner=OWNER, reviewed_sha=SHA, handled=frozenset({4}), told=True
    )
    assert step.action is rl.Action.REBUILD
    assert step.feedback == ("Review comment on server/x.py line 12: Use a timeout here",)
    assert step.comment_ids == (1,)


def test_a_merge_of_the_reviewed_commit_starts_the_watch() -> None:
    step = rl.next_step(
        _pr(state="closed", merged=True, merge_head_sha=SHA),
        owner=OWNER,
        reviewed_sha=SHA,
        handled=frozenset(),
        told=True,
    )
    assert step.action is rl.Action.WATCH


def test_a_merge_of_other_code_is_flagged() -> None:
    step = rl.next_step(
        _pr(state="closed", merged=True, merge_head_sha="b" * 40),
        owner=OWNER,
        reviewed_sha=SHA,
        handled=frozenset(),
        told=True,
    )
    assert step.action is rl.Action.FLAG_MISMATCH


def test_a_closed_pr_stops_the_ticket() -> None:
    step = rl.next_step(
        _pr(state="closed"), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=False
    )
    assert step.action is rl.Action.STOP


def test_a_head_that_moved_past_the_reviewed_commit_is_flagged() -> None:
    step = rl.next_step(
        _pr(head_sha="c" * 40), owner=OWNER, reviewed_sha=SHA, handled=frozenset(), told=False
    )
    assert step.action is rl.Action.FLAG_MISMATCH


def test_there_is_no_merge_action() -> None:
    assert not any("MERGE" in a.name and a is not rl.Action.FLAG_MISMATCH for a in rl.Action)
