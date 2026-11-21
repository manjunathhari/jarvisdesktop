"""Alembic environment: migrations run as the owner role, one transaction per revision."""

from __future__ import annotations

import os

from alembic import context
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import URL, create_engine, pool


def _url() -> URL:
    url = (
        context.config.attributes.get("dsn")
        or context.get_x_argument(as_dictionary=True).get("dsn")
        or os.environ["JARVIS_OWNER_DSN"]
    )
    # Accept any libpq connection string (URL or key=value) and point SQLAlchemy at psycopg 3.
    parts = {k: str(v) for k, v in conninfo_to_dict(url).items()}
    return URL.create(
        "postgresql+psycopg",
        username=parts.pop("user", None),
        password=parts.pop("password", None),
        host=parts.pop("host", None),
        port=int(parts.pop("port")) if "port" in parts else None,
        database=parts.pop("dbname", None),
        query=parts,
    )


def run() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


run()
