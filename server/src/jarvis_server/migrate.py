"""Run Alembic migrations from Python (used by `jarvis-core migrate` and the tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

SERVER_DIR = Path(__file__).resolve().parents[2]


def _config(owner_dsn: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    cfg.attributes["dsn"] = owner_dsn
    return cfg


def upgrade(owner_dsn: str, revision: str = "head") -> None:
    command.upgrade(_config(owner_dsn), revision)


def downgrade(owner_dsn: str, revision: str = "base") -> None:
    command.downgrade(_config(owner_dsn), revision)
