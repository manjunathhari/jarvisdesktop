"""The orchestrator's side of the sandbox: hand a stage over through /work and wait for it.

While it waits it renews the job's lease, so no other worker claims a paid stage that is running.
If the sandbox doesn't answer in time the stage counts as failed at its full cap: the agent may
have spent that much, and the cap must hold either way.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from jarvis_server.improve import handoff
from jarvis_server.improve.ports import Repo, StageRequest, StageResult
from jarvis_server.improve.stages import STAGE_CAP

TIMEOUT_S = 60 * 60


class FileSandbox:
    def __init__(
        self,
        work: Path,
        repo: Repo,
        *,
        heartbeat: Callable[[int], None],
        timeout_s: float = TIMEOUT_S,
        poll_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.work, self.repo, self.heartbeat = work, repo, heartbeat
        self.timeout_s, self.poll_s, self.clock, self.sleep = timeout_s, poll_s, clock, sleep

    def run(self, request: StageRequest) -> StageResult:
        with tempfile.TemporaryDirectory() as d:
            tar = Path(d) / "base.tar"
            self.repo.archive(request.base_sha, tar)
            nonce = handoff.write_request(self.work, request, base_tar=tar.read_bytes())
        deadline = self.clock() + self.timeout_s
        while True:
            try:
                result = handoff.read_result(self.work, job_id=request.job_id, nonce=nonce)
            except handoff.TooBig as e:
                return StageResult(
                    ok=False, cost_usd=STAGE_CAP[request.stage], turns=0, error=str(e)
                )
            if result is not None:
                return result
            if self.clock() > deadline:
                handoff.clear_inbox(self.work)
                return StageResult(
                    ok=False,
                    cost_usd=STAGE_CAP[request.stage],
                    turns=0,
                    error="the sandbox did not answer in time; counted at the stage's full cap",
                )
            self.heartbeat(request.job_id)
            self.sleep(self.poll_s)
