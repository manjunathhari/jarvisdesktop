"""One-time setup that needs the Postgres superuser: roles and the database.

Everything else (schemas, tables, functions, grants) is done by Alembic migrations running as
the owner role. Safe to run again: roles are created if missing and their passwords reset.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

OWNER_ROLE = "jarvis_owner"
APP_ROLE = "jarvis_app"
# Phase 2 (spec §4.1): the gateway's restricted role. Migration 0008 grants its whole surface.
GATEWAY_ROLE = "jarvis_gateway"


def bootstrap(
    superuser_dsn: str,
    database: str,
    owner_password: str,
    app_password: str,
    gateway_password: str,
) -> None:
    roles = (
        (OWNER_ROLE, owner_password),
        (APP_ROLE, app_password),
        (GATEWAY_ROLE, gateway_password),
    )
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        for role, password in roles:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.SQL(verb), sql.Identifier(role), sql.Literal(password)
                )
            )
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(OWNER_ROLE)
                )
            )
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        for role in (APP_ROLE, GATEWAY_ROLE):
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        # S5 (Phase 0 security review): nobody but the superuser connects to the default
        # database or the template, which hold nothing JARVIS needs.
        conn.execute("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")
        conn.execute("REVOKE CONNECT ON DATABASE template1 FROM PUBLIC")

    db_dsn = psycopg.conninfo.make_conninfo(superuser_dsn, dbname=database)
    with psycopg.connect(db_dsn, autocommit=True) as conn:
        # Nobody but the owner may create objects in the public schema.
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(OWNER_ROLE))
        )
