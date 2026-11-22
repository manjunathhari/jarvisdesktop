"""Errors raised by the memory engine."""

from __future__ import annotations

from pathlib import PurePosixPath


class MemoryEngineError(Exception):
    """Base class for memory engine errors."""


class MemoryFormatError(MemoryEngineError):
    """A memory file does not follow the format; the engine refuses to load (spec §4.3)."""

    def __init__(self, path: str | PurePosixPath, line: int, problem: str) -> None:
        super().__init__(f"{path}:{line}: {problem}")
        self.path = str(path)
        self.line = line
        self.problem = problem


class RemoteConfigured(MemoryEngineError):
    """The memory repo has a git remote. Memory must never be pushable (ADR-010)."""


class UnsafeRepo(MemoryEngineError):
    """The repo's own git config or attributes could run code or send data elsewhere."""


class MemoryRefused(MemoryEngineError):
    """A change was refused by the rules. The refusal has been audited with `code` only."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class UnknownViewer(MemoryEngineError):
    """A read named a viewer who is neither `system` nor a known person."""


class MemoryNotFound(MemoryEngineError):
    """No entry with that ID is visible to the viewer."""


class NotActionable(MemoryEngineError):
    """One or more entries may not drive an action (INFERRED, HISTORICAL, EXPIRED or unknown)."""

    def __init__(self, memory_ids: list[str]) -> None:
        super().__init__("not actionable until confirmed: " + ", ".join(memory_ids))
        self.memory_ids = memory_ids
