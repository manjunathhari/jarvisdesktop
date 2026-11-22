"""One-time setup that needs the Postgres superuser: roles and the database.

Everything else (schemas, tables, functions, grants) is done by Alembic migrations running as
the owner role. Safe to run again: roles are created if missing and their passwords reset.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

OWNER_ROLE = "jarvis_owner"
APP_ROLE = "jarvis_app"


def bootstrap(superuser_dsn: str, database: str, owner_password: str, app_password: str) -> None:
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        for role, password in ((OWNER_ROLE, owner_password), (APP_ROLE, app_password)):
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
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(APP_ROLE)
            )
        )
        # S5 (Phase 0 security review): nobody but the superuser connects to the default
        # database, which holds nothing JARVIS needs.
        conn.execute("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")

    db_dsn = psycopg.conninfo.make_conninfo(superuser_dsn, dbname=database)
    with psycopg.connect(db_dsn, autocommit=True) as conn:
        # Nobody but the owner may create objects in the public schema.
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(OWNER_ROLE))
        )
