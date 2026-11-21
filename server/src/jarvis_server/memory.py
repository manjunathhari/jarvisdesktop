"""Memory engine interface (Architecture v2 §11.5). Signatures only; built in Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    EPISODIC = "episodic"
    OPERATIONAL = "operational"
    LEARNED = "learned"


class MemoryTag(StrEnum):
    STATED = "STATED"
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    HISTORICAL = "HISTORICAL"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    subject: str
    category: MemoryCategory
    tag: MemoryTag
    content: str
    source: str


class Memory(Protocol):
    def retrieve_context(self, person: str, task: str) -> list[MemoryEntry]: ...

    def read(self, subject: str, category: MemoryCategory) -> list[MemoryEntry]: ...

    def search(self, query: str, scope: str) -> list[MemoryEntry]: ...

    def write(
        self, subject: str, category: MemoryCategory, content: str, tag: MemoryTag, source: str
    ) -> str: ...

    def update(self, memory_id: str, content: str) -> None: ...

    def confirm(self, memory_id: str, by: str) -> None: ...

    def archive(self, memory_id: str) -> None: ...
