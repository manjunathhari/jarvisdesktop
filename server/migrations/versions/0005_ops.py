"""Job queue and process heartbeats (spec §8). D5: the queue lives in Postgres.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE ops.job (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind          text NOT NULL,
        payload       jsonb NOT NULL DEFAULT '{}',
        status        text NOT NULL DEFAULT 'QUEUED'
                      CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
        run_at        timestamptz NOT NULL DEFAULT now(),
        attempts      integer NOT NULL DEFAULT 0,
        max_attempts  integer NOT NULL DEFAULT 2 CHECK (max_attempts >= 1),
        locked_by     text,
        locked_until  timestamptz,
        last_error    text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        finished_at   timestamptz
    );
    CREATE INDEX job_ready ON ops.job (run_at) WHERE status IN ('QUEUED', 'RUNNING');

    CREATE TABLE ops.heartbeat (
        process    text PRIMARY KEY,
        last_seen  timestamptz NOT NULL DEFAULT now(),
        details    jsonb NOT NULL DEFAULT '{}'
    );

    GRANT SELECT, INSERT, UPDATE, DELETE ON ops.job, ops.heartbeat TO jarvis_app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE ops.heartbeat;
    DROP TABLE ops.job;
    """)
