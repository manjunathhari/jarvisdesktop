"""A scripted `Brain` for tests."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from jarvis_server.brain.types import BrainCallFailed, BrainEvent, BrainRequest, CallPhase


@dataclass(frozen=True)
class FakeCall:
    """What one `think` call of a `FakeBrain` given `calls` does: replays `script`, then reports
    `usage` and `truncated` (with `stop_reason` `max_tokens` when truncated)."""

    script: Sequence[BrainEvent | BrainCallFailed]
    usage: tuple[int, int] | None = None
    truncated: bool = False
    # After its script, the call ran past the request's `time_limit`: the SEND-phase failure
    # `ClaudeBrain` raises then. A request with no limit would never end: an error in a test.
    overran: bool = False


class FakeBrain:
    """Replays `script` on every `think` call.

    An event is yielded as it is reached; a `BrainCallFailed` in the script is raised at that
    position, so events before it are delivered first. Every request is recorded in `requests`.
    `usage` mirrors `ClaudeBrain.usage` for the caller to read after the stream ends.

    With `calls`, the k-th `think` replays `calls[k]` instead (its own script, usage and
    `truncated`, or an overrun of the request's time limit), for a job that calls more than
    once; a call past the last is an error.
    """

    def __init__(
        self,
        script: list[BrainEvent | BrainCallFailed],
        *,
        usage: tuple[int, int] | None = None,
        searches: int = 0,
        sources: list[tuple[str, str]] | None = None,
        truncated: bool = False,
        stop_reason: str = "end_turn",
        calls: Sequence[FakeCall] | None = None,
    ) -> None:
        self._script = list(script)
        self.requests: list[BrainRequest] = []
        self.usage: tuple[int, int] | None = None
        self._final_usage = usage
        self._final = (searches, list(sources or []), truncated)
        self.searches = 0
        self.sources: list[tuple[str, str]] = []
        self.truncated = False
        self._final_stop = stop_reason
        self.stop_reason: str | None = None
        self._calls = None if calls is None else list(calls)

    def think(self, req: BrainRequest) -> Iterator[BrainEvent]:
        if self._calls is not None:
            yield from self._think_call(req)
            return
        self.requests.append(req)
        self.usage = None
        self.searches, self.sources, self.truncated, self.stop_reason = 0, [], False, None
        for item in self._script:
            if isinstance(item, BrainCallFailed):
                raise item
            yield item
        self.usage = self._final_usage
        self.searches, self.sources, self.truncated = (
            self._final[0],
            list(self._final[1]),
            self._final[2],
        )
        self.stop_reason = self._final_stop

    def _think_call(self, req: BrainRequest) -> Iterator[BrainEvent]:
        assert self._calls is not None
        if len(self.requests) >= len(self._calls):
            raise AssertionError("FakeBrain: called more times than it was given calls")
        call = self._calls[len(self.requests)]
        self.requests.append(req)
        self.usage = None
        self.searches, self.sources, self.truncated, self.stop_reason = 0, [], False, None
        for item in call.script:
            if isinstance(item, BrainCallFailed):
                raise item
            yield item
        if call.overran:
            if req.time_limit is None:
                raise AssertionError("FakeBrain: an overrun call without a time limit never ends")
            raise BrainCallFailed(CallPhase.SEND, detail="time limit")
        self.usage = call.usage
        self.truncated = call.truncated
        self.stop_reason = "max_tokens" if call.truncated else self._final_stop
