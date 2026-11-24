"""Production brain wiring in `jarvis-core serve` (plan Task 14, addition 2; R12-12; §4.2).

`core_main.wire_brain()` reads the secret `anthropic_api_key` and `HTTPS_PROXY`, and gives the
conversation turn (`turn.set_brain`) and the health probe (`health.set_probe_brain`) each a
factory that builds a new `ClaudeBrain(api_key, model=prices.default_model(), proxy)` per job.
Core refuses to start without either value, before it touches the database. The key is never
logged. Nothing here touches the network: socket connects fail the test.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis_server import config, core_main
from jarvis_server.brain import health, prices
from jarvis_server.brain.claude import ClaudeBrain
from jarvis_server.core import turn

KEY = "sk-ant-fictional-0123456789abcdef"  # not a real key
PROXY = "http://egress-proxy:3128"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary secrets directory, no network, and no brain left wired afterwards."""

    def no_network(*_: Any, **__: Any) -> None:
        raise AssertionError("the brain wiring touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    yield tmp_path
    turn.set_brain(None)
    health.set_probe_brain(None)


def _configure(secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (secrets / "anthropic_api_key").write_text(KEY + "\n")
    monkeypatch.setenv("HTTPS_PROXY", PROXY)


def test_turn_and_probe_get_a_claude_brain_per_job(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class Recording(ClaudeBrain):
        def __init__(self, api_key: str, model: str, *, proxy: str) -> None:
            built.append({"api_key": api_key, "model": model, "proxy": proxy})
            super().__init__(api_key, model, proxy=proxy)

    monkeypatch.setattr(core_main, "ClaudeBrain", Recording)
    _configure(_isolated, monkeypatch)
    core_main.wire_brain()
    assert turn._brain is not None and health._probe_brain is not None
    model = prices.default_model()
    for factory in (turn._brain, health._probe_brain):
        first, first_model = factory()
        second, second_model = factory()
        assert isinstance(first, Recording) and isinstance(second, Recording)
        assert first is not second  # one brain per job (Task 8 carry-over b)
        assert first_model == second_model == model
    assert built == [{"api_key": KEY, "model": model, "proxy": PROXY}] * 4


def test_the_previous_brain_is_closed_when_the_next_job_gets_one(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each brain owns an httpx client; jobs run one at a time, so the factory closes the last
    job's brain when it builds the next one, and nothing leaks."""
    closed: list[ClaudeBrain] = []
    monkeypatch.setattr(ClaudeBrain, "close", lambda self: closed.append(self))
    _configure(_isolated, monkeypatch)
    core_main.wire_brain()
    assert turn._brain is not None
    first, _ = turn._brain()
    assert closed == []
    second, _ = turn._brain()
    assert closed == [first] and second is not first


def test_core_refuses_to_start_without_the_api_key(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    with pytest.raises(SystemExit, match="anthropic_api_key"):
        core_main.wire_brain()
    (_isolated / "anthropic_api_key").write_text("\n")  # empty counts as missing
    with pytest.raises(SystemExit, match="anthropic_api_key"):
        core_main.wire_brain()
    assert turn._brain is None and health._probe_brain is None


@pytest.mark.parametrize("proxy", [None, ""])
def test_core_refuses_to_start_without_the_proxy(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch, proxy: str | None
) -> None:
    (_isolated / "anthropic_api_key").write_text(KEY)
    if proxy is not None:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    with pytest.raises(SystemExit, match="HTTPS_PROXY") as e:
        core_main.wire_brain()
    assert KEY not in str(e.value)
    assert turn._brain is None and health._probe_brain is None


def test_serve_refuses_before_touching_the_database(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_database(*_: Any, **__: Any) -> None:
        raise AssertionError("serve connected to the database before checking the brain")

    monkeypatch.setattr(core_main.psycopg, "connect", no_database)
    with pytest.raises(SystemExit, match="anthropic_api_key"):
        core_main.serve()


def test_the_key_is_never_logged(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(_isolated, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        core_main.wire_brain()
        assert turn._brain is not None and health._probe_brain is not None
        for factory in (turn._brain, health._probe_brain):
            brain, _ = factory()
            assert KEY not in repr(brain)
    assert caplog.records  # it does say what it wired
    assert KEY not in caplog.text
