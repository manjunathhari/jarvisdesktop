"""A scripted `Brain` for tests."""

from __future__ import annotations

from collections.abc import Iterator

from jarvis_server.brain.types import BrainCallFailed, BrainEvent, BrainRequest


class FakeBrain:
    """Replays `script` on every `think` call.

    An event is yielded as it is reached; a `BrainCallFailed` in the script is raised at that
    position, so events before it are delivered first. Every request is recorded in `requests`.
    `usage` mirrors `ClaudeBrain.usage` for the caller to read after the stream ends.
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

    def think(self, req: BrainRequest) -> Iterator[BrainEvent]:
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
