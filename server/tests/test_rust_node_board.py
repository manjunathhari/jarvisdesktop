"""The Board end to end with the real Rust node (Board slice 1, Tasks 5 and 9).

As `test_rust_node.py`: `jarvis-node-probe` is the real `jarvis-node` runtime, talking to the
real gateway and the real core, with a fake brain. The probe stands in for the Board window:
`--render-next` reports a panel drawn, `--refuse-render` that it could not be, `--set-state`
is a click. Every person, phrase and panel is fictional.

Skipped unless `JARVIS_NODE_PROBE` names the built probe.
"""

# The fixtures imported from the other end-to-end modules are pytest parameters here.
# ruff: noqa: F811

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jarvis_server.brain import ToolCall
from jarvis_server.contracts import board_content_sha256
from jarvis_server.core import board_job, board_node, turn
from jarvis_server.memory.engine import MemoryEngine

from .conftest import Dsns
from .test_gateway_handshake import MANJ
from .test_live_node import _code, dsns, memory, served  # noqa: F401 (fixtures)
from .test_rust_node import PROBE, AllJobs, Probe, core, kinds  # noqa: F401 (fixtures)
from .test_turn import Brains

pytestmark = pytest.mark.skipif(not PROBE, reason="set JARVIS_NODE_PROBE to the built probe")

TEXT = "| Place | Nights |\n|---|---|\n| Coorg (fictional) | 3 |\n| Ooty (fictional) | 2 |"
SAID = "Coorg looks best. Three options are on the board."
MAKE = ToolCall("board_make", {"request": "A comparison of three fictional hill stations"})
SHOW = ToolCall(
    "board_show", {"kind": "text", "title": "December trip", "content": TEXT, "headline": SAID}
)


def _panel(dsns: Dsns, panel_id: str) -> tuple[Any, ...]:
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        row = conn.execute(
            "SELECT state, revision, rendered_revision, person_id FROM board.panel WHERE id = %s",
            (uuid.UUID(panel_id),),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _until(check: Any, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_request_becomes_a_rendered_panel_then_voice_and_click_and_restart(
    dsns: Dsns, memory: MemoryEngine, core_key: Any, served: dict[str, Any],
    core: AllJobs, tmp_path: Path,
) -> None:  # fmt: skip
    """Say a fictional request → the acknowledgement → the Board job → `board.show` at the
    node → the node's store → the Board reports it drawn → the server marks it rendered → the
    headline. Then "minimise it" by voice reaches the node, a click on Close reaches the
    server, and a restarted node still has the panel."""
    # The handle the new panel will get: the tests of this module share one person.
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        row = conn.execute(
            "SELECT coalesce(max(number), 0) + 1 FROM board.panel WHERE person_id = %s", (MANJ,)
        ).fetchone()
    assert row is not None
    minimise = ToolCall("board_set_state", {"panel": f"p{row[0]}", "state": "minimise"})
    turn.set_brain(Brains([MAKE], [SHOW], [minimise]))
    state = tmp_path / "probe"
    p = Probe(
        served["port"], state, "--enrol", _code(dsns, core_key, served["gw_key"]),
        "--say", "Compare three fictional hill stations for December", "--render-next",
        "--wait", "3", "--say", "Minimise the trip panel", "--wait", "1",
        "--set-state", "closed", "--wait", "2",
    )  # fmt: skip
    p.wait_for(lambda e: e["event"] == "turn_done")
    # The node hands speech on a sentence at a time.
    spoken = " ".join(e["text"] for e in p.events if e["event"] == "speech")
    assert spoken == turn.BOARD_ACK_TEXT
    shown = p.wait_for(lambda e: e["event"] == "board_show", timeout=40)
    # The desktop was told work had started, in the turn's own reply, before the panel came.
    order = [e["event"] for e in p.events]
    assert "board_working" in order
    assert order.index("board_working") < order.index("board_show")
    assert (shown["title"], shown["kind"], shown["revision"]) == ("December trip", "text", 1)
    assert shown["content_sha256"] == board_content_sha256(TEXT)
    assert shown["cost_paise"] > 0
    panel_id = shown["panel_id"]
    assert _panel(dsns, panel_id) == ("open", 1, None, MANJ), "not rendered until reported"
    p.wait_for(lambda e: e["event"] == "board_rendered")
    headline = p.wait_for(lambda e: e["event"] == "reply" and e["text"] == SAID, timeout=40)
    assert headline["in_reply_to"] is None and headline["final"] is True
    assert _panel(dsns, panel_id)[2] == 1, "the server recorded the panel as drawn"
    # By voice: the node gets the state, and its store and the server's agree.
    moved = p.wait_for(lambda e: e["event"] == "board_state", timeout=40)
    assert (moved["panel_id"], moved["state"]) == (panel_id, "minimised")
    # By click: the node tells the server.
    p.wait_for(lambda e: e["event"] == "board_state_set")
    _until(lambda: _panel(dsns, panel_id)[0] == "closed")
    events = p.finish()
    assert kinds(events)[-1] == "done", events
    assert "board.make" in core.ran and "board.headline" in core.ran
    assert board_job.HEADLINE_WAIT.total_seconds() > 100, "it was the receipt that released it"
    assert "board.state" in core.ran
    # Nothing about this panel failed. (Asked of the server: a new node of the same person is
    # also sent the replies of the module's other test.)
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        failed = conn.execute(
            "SELECT count(*) FROM comm.event WHERE details->>'panel_id' = %s"
            " AND kind IN ('board.render_failed', 'board.state_refused')",
            (panel_id,),
        ).fetchone()
    assert failed == (0,)
    # A restarted node has the panel, in its last state.
    again = Probe(served["port"], state, "--wait", "1").finish()
    restored = [e for e in again if e["event"] == "board_restored"]
    assert [(e["panel_id"], e["state"], e["title"]) for e in restored] == [
        (panel_id, "closed", "December trip")
    ]


def test_refused_render_is_not_marked_rendered(
    dsns: Dsns, memory: MemoryEngine, core_key: Any, served: dict[str, Any],
    core: AllJobs, tmp_path: Path,
) -> None:  # fmt: skip
    # Its own headline: a new node of the same person is also sent the other test's replies.
    unsaid = "Two fictional beaches are on the board."
    show = ToolCall("board_show", {**SHOW.args, "title": "Beaches", "headline": unsaid})
    turn.set_brain(Brains([MAKE], [show]))
    p = Probe(
        served["port"], tmp_path / "probe", "--enrol", _code(dsns, core_key, served["gw_key"]),
        "--say", "Compare two fictional beaches", "--refuse-render",
        "--wait", "4",
    )  # fmt: skip
    shown = p.wait_for(lambda e: e["event"] == "board_show" and e["title"] == "Beaches", timeout=40)
    failed = p.wait_for(
        lambda e: e["event"] == "reply" and e["text"] == board_node.BOARD_NOT_SHOWN_TEXT,
        timeout=40,
    )
    assert failed["in_reply_to"] is None
    events = p.finish()
    assert _panel(dsns, shown["panel_id"])[2] is None
    assert "board.headline" in core.ran, "its job ran, released by the receipt, and said nothing"
    assert unsaid not in [e.get("text") for e in events], "the headline is not claimed"
