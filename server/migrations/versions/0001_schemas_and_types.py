"""Schemas ledger/audit/ops, shared enum types and the timestamp trigger.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA ledger;
    CREATE SCHEMA audit;
    CREATE SCHEMA ops;
    REVOKE ALL ON SCHEMA ledger, audit, ops FROM PUBLIC;
    GRANT USAGE ON SCHEMA ledger, audit, ops TO jarvis_app;

    -- Spelled exactly as in /contracts (a test compares them).
    CREATE TYPE ledger.visibility AS ENUM
        ('PRIVATE', 'SELECTED_PERSON', 'SELECTED_GROUP', 'FAMILY_SHARED', 'ORGANIZATION_SHARED');
    CREATE TYPE ledger.task_state AS ENUM
        ('DETECTED', 'INTERPRETED', 'PROPOSED', 'APPROVAL_PENDING', 'READY', 'IN_PROGRESS',
         'COMPLETED', 'BLOCKED', 'DEFERRED', 'CANCELLED', 'FAILED', 'EXPIRED', 'SUPERSEDED');
    CREATE TYPE ledger.commitment_state AS ENUM
        ('EXPECTED', 'AT_RISK', 'SECURED', 'FULFILLED', 'MISSED');
    CREATE TYPE audit.actor_kind AS ENUM ('PERSON', 'AGENT', 'SYSTEM');
    CREATE TYPE audit.action AS ENUM ('CREATE', 'TRANSITION');
    CREATE TYPE audit.outcome AS ENUM ('APPLIED', 'REJECTED');

    -- created_at/updated_at are always set by the database, never by the caller.
    CREATE FUNCTION ledger.stamp_times() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            NEW.created_at := now();
        ELSE
            NEW.created_at := OLD.created_at;
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION ledger.stamp_times();
    DROP TYPE audit.outcome;
    DROP TYPE audit.action;
    DROP TYPE audit.actor_kind;
    DROP TYPE ledger.commitment_state;
    DROP TYPE ledger.task_state;
    DROP TYPE ledger.visibility;
    DROP SCHEMA ops;
    DROP SCHEMA audit;
    DROP SCHEMA ledger;
    """)
