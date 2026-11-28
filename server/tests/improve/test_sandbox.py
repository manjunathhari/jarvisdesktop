"""The orchestrator's hand-over to the sandbox, with a thread playing the sandbox."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from jarvis_server.improve import handoff
from jarvis_server.improve.ports import StageRequest, StageResult
from jarvis_server.improve.sandbox import FileSandbox
from jarvis_server.improve.stages import STAGE_CAP

REQ = StageRequest(7, 4, "build", "a" * 40, "thin", {}, "# d", (), ())


class TarRepo:
    def archive(self, sha: str, dest: Path) -> None:
        dest.write_bytes(b"TAR:" + sha.encode())


def test_the_stage_comes_back_with_its_result_and_the_lease_is_renewed(tmp_path: Path) -> None:
    beats: list[int] = []

    def sandbox() -> None:
        while (taken := handoff.take_request(tmp_path)) is None:
            time.sleep(0.01)
        request, tar, nonce = taken
        assert tar == b"TAR:" + b"a" * 40 and request == REQ
        time.sleep(0.05)
        handoff.write_result(
            tmp_path, job_id=request.job_id, nonce=nonce, result=StageResult(True, 1.25, 12)
        )

    t = threading.Thread(target=sandbox)
    t.start()
    fs = FileSandbox(tmp_path, TarRepo(), heartbeat=beats.append, poll_s=0.01)  # type: ignore[arg-type]
    assert fs.run(REQ) == StageResult(True, 1.25, 12)
    t.join()
    assert beats and set(beats) == {7}


def test_no_answer_in_time_counts_the_full_stage_cap(tmp_path: Path) -> None:
    now = [0.0]
    fs = FileSandbox(
        tmp_path,
        TarRepo(),  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        timeout_s=10,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + 6),
    )
    result = fs.run(REQ)
    assert not result.ok and result.cost_usd == STAGE_CAP["build"]
    assert not (tmp_path / "inbox").exists()
