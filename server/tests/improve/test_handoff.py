"""The hand-off between the orchestrator and the sandbox, through the shared /work volume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_server.improve import handoff
from jarvis_server.improve.ports import StageRequest, StageResult

REQ = StageRequest(
    job_id=7,
    ticket_id=4,
    stage="build",
    base_sha="a" * 40,
    words="research is too thin",
    evidence={"turns": ["a"]},
    design="# Better research\n",
    patches=(b"diff --git a/x b/x\n", b"\x00binary"),
    feedback=("CI failed",),
)


def test_a_request_goes_in_whole_with_the_base_and_comes_out_once(tmp_path: Path) -> None:
    nonce = handoff.write_request(tmp_path, REQ, base_tar=b"TAR")
    got, tar, seen_nonce = handoff.take_request(tmp_path)
    assert (got, tar, seen_nonce) == (REQ, b"TAR", nonce)
    assert not (tmp_path / "inbox").exists()  # the agent never sees the nonce or the request
    assert handoff.take_request(tmp_path) is None


def test_writing_a_request_clears_anything_left_in_the_volume(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "request.json").write_text('{"stage": "design"}')
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "result.json").write_text("{}")
    handoff.write_request(tmp_path, REQ, base_tar=b"TAR")
    assert not (tmp_path / "outbox").exists()
    assert handoff.take_request(tmp_path) is not None


def test_a_result_counts_only_with_this_job_s_nonce(tmp_path: Path) -> None:
    result = StageResult(ok=True, cost_usd=1.5, turns=9, text="VERDICT: PASS", patch=b"\x00p")
    handoff.write_result(tmp_path, job_id=7, nonce="n1", result=result)
    assert handoff.read_result(tmp_path, job_id=7, nonce="n2") is None
    assert handoff.read_result(tmp_path, job_id=8, nonce="n1") is None
    assert handoff.read_result(tmp_path, job_id=7, nonce="n1") == result
    assert not (tmp_path / "outbox").exists()  # read once


def test_a_planted_result_without_the_nonce_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "result.json").write_text(
        json.dumps({"job_id": 7, "ok": True, "cost_usd": 0, "turns": 1})
    )
    assert handoff.read_result(tmp_path, job_id=7, nonce="n1") is None


def test_a_result_too_big_to_be_a_patch_is_refused(tmp_path: Path) -> None:
    big = StageResult(ok=True, cost_usd=0.1, turns=1, patch=b"x" * (handoff.MAX_PATCH + 1))
    with pytest.raises(handoff.TooBig):
        handoff.write_result(tmp_path, job_id=7, nonce="n1", result=big)
