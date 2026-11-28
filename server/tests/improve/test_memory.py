"""The in-memory store keeps the same rules as the Postgres one, so the dry run can't cheat."""

from __future__ import annotations

import pytest

from jarvis_server.improve import inbox
from jarvis_server.improve.memory import IllegalMove, MemoryStore
from jarvis_server.improve.tickets import State


def test_a_move_the_ticket_rules_forbid_is_refused() -> None:
    store = MemoryStore()
    tid = store.open_ticket("research is thin", "research-is-thin", {})
    with pytest.raises(IllegalMove):
        store.move(tid, State.PR_OPEN, "jarvis", "skipping the design")


def test_approval_needs_the_fingerprint_of_the_design_as_it_stands() -> None:
    store = MemoryStore()
    tid = store.open_ticket("research is thin", "research-is-thin", {})
    store.move(tid, State.DESIGNING, "jarvis", "started")
    store.update(tid, design_fingerprint="abc", approved_fingerprint="old")
    store.move(tid, State.DESIGN_READY, "jarvis", "written")
    with pytest.raises(IllegalMove):
        store.move(tid, State.APPROVED, "owner", "yes")


def test_jobs_are_claimed_in_order_and_once() -> None:
    store = MemoryStore()
    store.enqueue(1, "design")
    store.enqueue(1, "tests", delay_s=60)
    first, second = store.claim("w", 120), store.claim("w", 120)
    assert first is not None and second is not None
    assert (first.stage, second.stage) == ("design", "tests")
    assert store.claim("w", 120) is None
    store.finish(first.id)
    store.fail(second.id, "boom")
    assert store.queued() == []


def test_a_key_item_is_resolved_by_its_key_name() -> None:
    store = MemoryStore()
    store.add_inbox(inbox.key_needed("improve_claude_key", "the Claude key", ("step",)))
    store.resolve_key("improve_claude_key")
    assert store.open_inbox() == []
