"""Running the memory engine inside `core`: open it at start, write the daily bundle.

The memory volume is mounted only into core (spec §9). If memory does not load, core refuses
to start, so a damaged memory is noticed at once (runbook §7).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from jarvis_server.memory.checks import HOUSEHOLD_ID
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.memory.pg import PgAudit, PgDirectory

log = logging.getLogger("jarvis.memory")

KEEP_BUNDLES = 7


def memory_dir() -> Path:
    return Path(os.environ.get("JARVIS_MEMORY_DIR", "/var/lib/jarvis/memory"))


def bundle_dir() -> Path:
    return Path(os.environ.get("JARVIS_MEMORY_BUNDLE_DIR", "/backups/memory"))


def open_engine(conn: psycopg.Connection[Any], root: Path | None = None) -> MemoryEngine:
    return MemoryEngine(root or memory_dir(), PgDirectory(conn), PgAudit(conn), HOUSEHOLD_ID).open()


def write_daily_bundle(
    engine: MemoryEngine, target_dir: Path | None = None, today: date | None = None
) -> Path | None:
    """Write today's bundle if it is missing; keep the newest KEEP_BUNDLES. Returns the new file."""
    target_dir = target_dir or bundle_dir()
    today = today or date.today()
    target = target_dir / f"memory-{today.isoformat()}.bundle"
    if target.exists():
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    engine.store.bundle(target)
    for old in sorted(target_dir.glob("memory-*.bundle"))[:-KEEP_BUNDLES]:
        old.unlink()
    log.info("memory: wrote backup bundle %s", target.name)
    return target
