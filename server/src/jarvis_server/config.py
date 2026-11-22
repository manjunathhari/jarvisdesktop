"""Runtime configuration from environment variables and Docker secret files.

Passwords are never read from environment variables: only from files (Docker Compose secrets
appear under /run/secrets). See ADR-009.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import psycopg

# The household seeded by migration 0006 (D6 "me only": one household).
HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-000000000001")

SECRETS_DIR = Path(os.environ.get("JARVIS_SECRETS_DIR", "/run/secrets"))


def secret(name: str) -> str:
    path = SECRETS_DIR / name
    try:
        value = path.read_text().strip()
    except FileNotFoundError as e:
        raise RuntimeError(f"secret file {path} is missing") from e
    if not value:
        raise RuntimeError(f"secret file {path} is empty")
    return value


def dsn(user: str, password_secret: str, dbname: str | None = None) -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("JARVIS_DB_HOST", "postgres"),
        port=os.environ.get("JARVIS_DB_PORT", "5432"),
        dbname=dbname or os.environ.get("JARVIS_DB_NAME", "jarvis"),
        user=user,
        password=secret(password_secret),
        application_name=os.environ.get("JARVIS_PROCESS", "jarvis"),
    )


def app_dsn() -> str:
    return dsn("jarvis_app", "pg_app_password")


def gateway_dsn() -> str:
    return dsn("jarvis_gateway", "pg_gateway_password")


def owner_dsn() -> str:
    return dsn("jarvis_owner", "pg_owner_password")


def superuser_dsn() -> str:
    return dsn("postgres", "pg_superuser_password", dbname="postgres")
