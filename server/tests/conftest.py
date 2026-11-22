"""Test database: a fresh database per test run, built from migrations alone.

Needs JARVIS_TEST_SUPERUSER_DSN (e.g. postgresql://postgres:pw@localhost:5432/postgres).
Tests connect as jarvis_app, exactly like the running services.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg import sql

from jarvis_server import migrate
from jarvis_server.bootstrap import APP_ROLE, OWNER_ROLE, bootstrap


@dataclass(frozen=True)
class Dsns:
    superuser: str
    owner: str
    app: str
    database: str


def _with(dsn: str, **kw: str) -> str:
    return psycopg.conninfo.make_conninfo(dsn, **kw)


@contextlib.contextmanager
def fresh_database() -> Iterator[Dsns]:
    """A new database built from migrations alone; dropped afterwards."""
    superuser = os.environ.get("JARVIS_TEST_SUPERUSER_DSN")
    if not superuser:
        pytest.skip("JARVIS_TEST_SUPERUSER_DSN not set; database tests skipped")
    database = f"jarvis_test_{secrets.token_hex(4)}"
    owner_pw, app_pw = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    bootstrap(superuser, database, owner_pw, app_pw)
    d = Dsns(
        superuser=_with(superuser, dbname=database),
        owner=_with(superuser, dbname=database, user=OWNER_ROLE, password=owner_pw),
        app=_with(superuser, dbname=database, user=APP_ROLE, password=app_pw),
        database=database,
    )
    migrate.upgrade(d.owner)
    try:
        yield d
    finally:
        with psycopg.connect(superuser, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


@pytest.fixture(scope="session")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def app(dsns: Dsns) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        yield conn


@pytest.fixture
def owner(dsns: Dsns) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        yield conn
