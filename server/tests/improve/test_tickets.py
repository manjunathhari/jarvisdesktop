"""A ticket moves only along the states table in the design."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis_server.improve import tickets
from jarvis_server.improve.tickets import State

S = State

ALLOWED = [
    (S.OPEN, S.DESIGNING),
    (S.DESIGNING, S.DESIGN_READY),
    (S.DESIGN_READY, S.APPROVED),
    (S.DESIGN_READY, S.DESIGNING),
    (S.APPROVED, S.PR_OPEN),
    (S.PR_OPEN, S.WATCHING),
    (S.PR_OPEN, S.APPROVED),
    (S.PR_OPEN, S.STOPPED),
    (S.WATCHING, S.DONE),
    (S.WATCHING, S.DESIGNING),
]


@pytest.mark.parametrize(("a", "b"), ALLOWED)
def test_the_design_s_moves_are_allowed(a: State, b: State) -> None:
    assert tickets.can_move(a, b)


@pytest.mark.parametrize("a", [s for s in S if s not in (S.DONE, S.STOPPED, S.PAUSED)])
def test_any_live_ticket_can_pause_or_stop(a: State) -> None:
    assert tickets.can_move(a, S.PAUSED)
    assert tickets.can_move(a, S.STOPPED)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (S.OPEN, S.APPROVED),  # no design, no yes
        (S.DESIGNING, S.APPROVED),  # skips Manj's gate 1
        (S.APPROVED, S.WATCHING),  # skips the PR and his merge
        (S.OPEN, S.PR_OPEN),
        (S.DONE, S.DESIGNING),
        (S.STOPPED, S.OPEN),
        (S.DONE, S.PAUSED),
        (S.PAUSED, S.PAUSED),
        (S.OPEN, S.OPEN),
    ],
)
def test_other_moves_are_refused(a: State, b: State) -> None:
    assert not tickets.can_move(a, b)


def test_a_paused_ticket_goes_back_only_where_it_was() -> None:
    assert tickets.can_move(S.PAUSED, S.APPROVED, paused_from=S.APPROVED)
    assert not tickets.can_move(S.PAUSED, S.PR_OPEN, paused_from=S.APPROVED)
    assert tickets.can_move(S.PAUSED, S.STOPPED, paused_from=S.APPROVED)
    assert not tickets.can_move(S.PAUSED, S.APPROVED)


def test_the_table_lists_every_move_once() -> None:
    pairs = tickets.transitions()
    assert len(pairs) == len(set(pairs))
    assert set(ALLOWED) <= set(pairs)


def test_three_rounds_in_all() -> None:
    assert tickets.ROUNDS == 3
    assert tickets.rounds_left(0) == 3
    assert tickets.rounds_left(3) == 0


def test_a_design_waits_fourteen_days() -> None:
    ready = datetime(2026, 11, 1, tzinfo=UTC)
    assert not tickets.design_expired(ready, now=ready + timedelta(days=14))
    assert tickets.design_expired(ready, now=ready + timedelta(days=14, seconds=1))


def test_the_watch_lasts_seven_days() -> None:
    started = datetime(2026, 11, 1, tzinfo=UTC)
    assert not tickets.watch_over(started, now=started + timedelta(days=6, hours=23))
    assert tickets.watch_over(started, now=started + timedelta(days=7))


@pytest.mark.parametrize(
    ("words", "slug"),
    [
        ("You can't do research properly!", "you-can-t-do-research-properly"),
        ("   ", "ticket"),
        ("Ünïcode wörds only ✨", "n-code-w-rds-only"),
        ("x" * 100, "x" * 40),
    ],
)
def test_the_branch_slug_is_plain(words: str, slug: str) -> None:
    assert tickets.slug(words) == slug


def test_the_branch_name_sits_under_improve() -> None:
    assert tickets.branch(4, "research-too-thin") == "improve/4-research-too-thin"
