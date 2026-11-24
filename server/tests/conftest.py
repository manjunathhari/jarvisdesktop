"""Test database: a fresh database per test run, built from migrations alone.

Needs JARVIS_TEST_SUPERUSER_DSN (e.g. postgresql://postgres:pw@localhost:5432/postgres).
Tests connect as jarvis_app (and, for the gateway's surface, jarvis_gateway), exactly like the
running services.
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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import sql

from jarvis_server import config, migrate
from jarvis_server.bootstrap import APP_ROLE, GATEWAY_ROLE, OWNER_ROLE, bootstrap
from jarvis_server.signing import fingerprint, public_bytes


@dataclass(frozen=True)
class Dsns:
    superuser: str
    owner: str
    app: str
    gateway: str
    database: str


def _with(dsn: str, **kw: str) -> str:
    return psycopg.conninfo.make_conninfo(dsn, **kw)


_PASSWORDS = (secrets.token_urlsafe(16), secrets.token_urlsafe(16), secrets.token_urlsafe(16))


@contextlib.contextmanager
def fresh_database() -> Iterator[Dsns]:
    """A new database built from migrations alone; dropped afterwards."""
    superuser = os.environ.get("JARVIS_TEST_SUPERUSER_DSN")
    if not superuser:
        pytest.skip("JARVIS_TEST_SUPERUSER_DSN not set; database tests skipped")
    database = f"jarvis_test_{secrets.token_hex(4)}"
    # Roles are cluster-wide and bootstrap sets their passwords, so every test database in
    # one run must use the same passwords or earlier connections stop authenticating.
    owner_pw, app_pw, gateway_pw = _PASSWORDS
    bootstrap(superuser, database, owner_pw, app_pw, gateway_pw)
    d = Dsns(
        superuser=_with(superuser, dbname=database),
        owner=_with(superuser, dbname=database, user=OWNER_ROLE, password=owner_pw),
        app=_with(superuser, dbname=database, user=APP_ROLE, password=app_pw),
        gateway=_with(superuser, dbname=database, user=GATEWAY_ROLE, password=gateway_pw),
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


@pytest.fixture
def gateway(dsns: Dsns) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsns.gateway, autocommit=True) as conn:
        yield conn


@pytest.fixture
def core_key(
    dsns: Dsns, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """A throwaway core signing key: the ACTIVE CORE key in the test database's gw.trusted_key
    (replacing any other CORE key), served as the bootstrap secret `core_signing_key` from a
    temporary secrets directory (`core.keys.active_core_key` reads both)."""
    key = Ed25519PrivateKey.generate()
    public = public_bytes(key)
    with psycopg.connect(dsns.owner, autocommit=True) as conn, conn.transaction():
        conn.execute("DELETE FROM gw.trusted_key WHERE role = 'CORE'")
        conn.execute(
            "INSERT INTO gw.trusted_key (key_id, role, public_key, state, activated_at)"
            " VALUES (%s, 'CORE', %s, 'ACTIVE', now())",
            (fingerprint(public), public),
        )
    directory = tmp_path_factory.mktemp("secrets")
    (directory / "core_signing_key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(config, "SECRETS_DIR", directory)
    return key
