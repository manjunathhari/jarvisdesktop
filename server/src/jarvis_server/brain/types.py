"""Provider-neutral brain types (spec §10).

Prompts and outputs are plain frozen dataclasses so a second provider can sit behind the same
`Brain` interface later. Nothing here knows about Claude or HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Turn:
    role: Literal["person", "jarvis"]
    text: str


@dataclass(frozen=True)
class MemoryLine:
    memory_id: str
    text: str
    drives_action: bool


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class BrainRequest:
    system: str
    history: list[Turn]
    utterance: str
    screen_summary: str
    memory: list[MemoryLine]
    tools: list[ToolSpec]
    max_tokens: int
    # The person's Board, one line per panel (`prompt.board_lines`); untrusted data.
    board: list[str] = field(default_factory=list)
    # Tools the provider runs itself (web search), sent as given after `tools`. Their use is
    # billed per use (`prices.tool_paise`); the brain reports the count in `searches`.
    server_tools: list[dict[str, Any]] = field(default_factory=list)
    # Earlier rounds of the same job, sent after the utterance in order (`Exchange`).
    exchanges: list[Exchange] = field(default_factory=list)


@dataclass(frozen=True)
class Speech:
    text: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    # The provider's id for this call (Claude's `tool_use` id), needed to send a result back.
    # Not part of equality: a call is the same call whatever id the provider gave it.
    call_id: str = field(default="", compare=False)


@dataclass(frozen=True)
class ToolResult:
    """What a tool call gave back, for the next call of the same job. Untrusted data."""

    call_id: str
    text: str
    image_png: bytes | None = None
    is_error: bool = False


@dataclass(frozen=True)
class Exchange:
    """One earlier round of the same job: what the model sent (`text`, `calls`) and what it is
    given back (`results`, one per call, matched by `call_id`). `note` is plain text for the
    person's side, after any results; with no calls it is the whole answer."""

    text: str
    calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]
    note: str = ""


BrainEvent = Speech | ToolCall


class CallPhase(StrEnum):
    """Where a failed call stopped (§10.1 step 4).

    CONNECT: no request byte was written, so the budget reservation may be released.
    SEND: the request may have reached the provider, so the reservation is indeterminate.
    """

    CONNECT = "CONNECT"
    SEND = "SEND"


class BrainCallFailed(Exception):
    """A brain call did not complete. `phase` says whether a request could have been sent."""

    def __init__(self, phase: CallPhase, status: int | None = None, detail: str = "") -> None:
        self.phase = phase
        self.status = status
        self.detail = detail
        super().__init__(f"brain call failed in {phase} phase (status={status}) {detail}".strip())


class Brain(Protocol):
    """A brain call streams events; after a completed stream `usage` is
    `(input_tokens, output_tokens)` as the provider reported them, or None if it did not
    complete (or reported none, which the caller treats as fail closed).

    After a completed stream, also: `searches`, the web searches the provider ran and billed;
    `sources`, the (title, address) of the pages those searches returned; `truncated`, whether
    the output limit ended the reply."""

    usage: tuple[int, int] | None
    searches: int
    sources: list[tuple[str, str]]
    truncated: bool

    def think(self, req: BrainRequest) -> Iterator[BrainEvent]: ...
