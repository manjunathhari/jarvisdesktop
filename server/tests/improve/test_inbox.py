"""One inbox for Manj: what the team needs from him, with exact steps, never a key's value."""

from __future__ import annotations

import pytest

from jarvis_server.improve import inbox
from jarvis_server.improve.inbox import Kind


def test_an_item_names_a_key_and_gives_steps() -> None:
    item = inbox.key_needed(
        "improve_claude_key",
        "the Claude key for the team, from its own Console workspace",
        ("Create the key in the jarvis-improve workspace.", "Save it to the secrets folder."),
    )
    assert item.kind is Kind.KEY
    assert item.key_name == "improve_claude_key"
    assert item.title == "Add the improve_claude_key secret"
    assert item.steps[0].startswith("Create the key")


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-" + "a" * 40,
        "ghp_" + "b" * 36,
        "github_pat_" + "c" * 60,
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "xoxb-1234-5678-abcdefghij",
        "AKIA" + "D" * 16,
        "Zm9vYmFyYmF6cXV4cXV1eGZvb2JhcmJhenF1eHF1dXhmb28",  # long unbroken token
    ],
)
def test_an_item_that_carries_a_secret_value_is_refused(value: str) -> None:
    with pytest.raises(inbox.LooksLikeASecret):
        inbox.Item(Kind.DECISION, title="Approve", steps=(f"Use {value} here",))
    with pytest.raises(inbox.LooksLikeASecret):
        inbox.Item(Kind.DECISION, title=f"Approve {value}", steps=())


def test_ordinary_words_and_paths_pass() -> None:
    inbox.Item(
        Kind.DECISION,
        title="Approve the design for ticket 4",
        steps=(
            "Read the design on the Board: fingerprint 3f9a1c2e.",
            "Run: jarvis-improver approve 4 3f9a1c2e",
            "See server/src/jarvis_server/improve/README.md for more.",
        ),
        ticket_id=4,
    )


def test_ready_to_merge_never_offers_to_merge() -> None:
    item = inbox.ready_to_merge(4, "https://github.com/o/r/pull/9", "Research answers cite sources")
    assert item.kind is Kind.READY_TO_MERGE
    assert all("merge it for you" not in s for s in item.steps)
    assert any("approve and merge" in s for s in item.steps)


def test_a_blocked_site_names_the_host_only() -> None:
    item = inbox.blocked_site(4, "https://example.org/path?token=abc")
    assert item.kind is Kind.BLOCKED_SITE
    assert "example.org" in item.title
    assert all("token" not in s for s in (item.title, *item.steps))


def test_the_list_is_rendered_newest_last_with_numbered_steps() -> None:
    items = [
        inbox.design_ready(4, "Research answers are too thin", "3f9a1c2e" + "0" * 56),
        inbox.paused(4, spent=7.2, cap=8.0),
    ]
    text = inbox.render(items)
    assert text.splitlines()[0] == "2 things need you:"
    assert "1. Approve the design for ticket 4" in text
    assert "jarvis-improver approve 4 3f9a1c2e" in text
    assert "Ticket 4 paused at $7.20 of its $8.00 cap" in text
    assert inbox.render([]) == "Nothing needs you."
