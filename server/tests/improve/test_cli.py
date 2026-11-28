"""jarvis-improver: his commands, and the key check that fills the inbox without a key's value."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from jarvis_server.improve import cli, inbox
from jarvis_server.improve.tickets import State

from .fakes import FakeStore

PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
ENV = {"JARVIS_IMPROVE_APP_ID": "123", "JARVIS_IMPROVE_INSTALLATION_ID": "55"}


def _secrets(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_nothing_is_missing_when_the_app_key_ids_and_proxy_are_there(tmp_path: Path) -> None:
    d = _secrets(tmp_path, improve_github_app_key=PEM)
    assert cli.missing_keys(ENV, d, proxy_up=lambda: True) == []


def test_each_missing_piece_gets_one_item_with_steps_and_never_a_value(tmp_path: Path) -> None:
    d = _secrets(tmp_path, improve_github_app_key="not a key")
    items = cli.missing_keys({}, d, proxy_up=lambda: False)
    names = [i.key_name for i in items]
    assert names == ["improve_github_app_key", "improve_claude_key"]
    text = inbox.render(items)
    assert "JARVIS_IMPROVE_APP_ID" in text and "/etc/jarvis/secrets/improve_claude_key" in text
    assert "$30" in text
    assert "BEGIN" not in text  # the key file's content is never repeated


def test_the_inbox_is_synced_once_per_key_and_cleared_when_it_is_fixed(tmp_path: Path) -> None:
    store = FakeStore()
    missing = cli.missing_keys({}, tmp_path, proxy_up=lambda: True)
    cli.sync_keys(store, missing)
    cli.sync_keys(store, missing)
    assert [i.key_name for i in store.open_inbox()] == ["improve_github_app_key"]
    cli.sync_keys(store, [])
    assert store.open_inbox() == []


def test_manj_s_commands(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeStore()
    assert cli.command(store, ["open", "research", "is", "too", "thin"]) == 0
    assert store.get(1).words == "research is too thin"
    assert store.queued() == ["design"]
    assert "ticket 1" in capsys.readouterr().out
    assert cli.command(store, ["approve", "1", "abcd1234"]) == 1  # no design waiting
    assert "no design waiting" in capsys.readouterr().err
    assert cli.command(store, ["stop", "1"]) == 0
    assert store.get(1).state is State.STOPPED
    assert cli.command(store, ["inbox"]) == 0
    assert capsys.readouterr().out.strip().endswith("Nothing needs you.")


def test_the_inbox_says_to_seal_each_key_as_harden_sh_does(tmp_path: Path) -> None:
    # The improver runs as 10001 and claude-proxy joins group 10001: a root-only 0400 file
    # would be unreadable to both, so the steps must say root:10001, mode 0440.
    text = inbox.render(cli.missing_keys({}, _secrets(tmp_path), proxy_up=lambda: False))
    assert "0400" not in text
    assert text.count("root:10001, mode 0440") == 2
