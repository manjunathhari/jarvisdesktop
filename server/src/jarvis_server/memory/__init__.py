"""Memory engine (Architecture v2 §11; Phase 1 spec; ADR-010).

engine = MemoryEngine(root, PgDirectory(conn), PgAudit(conn), HOUSEHOLD_ID).open()
engine.retrieve_context("daughter", "school trip", viewer="manj")
"""

from __future__ import annotations

from jarvis_server.contracts import MemoryCategory, MemoryEntry, MemoryTag, Visibility
from jarvis_server.memory.engine import (
    SYSTEM,
    AuditRecord,
    AuditSink,
    Directory,
    MemoryEngine,
    MemoryVersion,
)
from jarvis_server.memory.errors import (
    MemoryEngineError,
    MemoryFormatError,
    MemoryNotFound,
    MemoryRefused,
    NotActionable,
    RemoteConfigured,
    UnknownViewer,
    UnsafeRepo,
)
from jarvis_server.memory.format import HOUSEHOLD, Person

__all__ = [
    "HOUSEHOLD",
    "SYSTEM",
    "AuditRecord",
    "AuditSink",
    "Directory",
    "MemoryCategory",
    "MemoryEngine",
    "MemoryEngineError",
    "MemoryEntry",
    "MemoryFormatError",
    "MemoryNotFound",
    "MemoryRefused",
    "MemoryTag",
    "MemoryVersion",
    "NotActionable",
    "Person",
    "RemoteConfigured",
    "UnknownViewer",
    "UnsafeRepo",
    "Visibility",
]
