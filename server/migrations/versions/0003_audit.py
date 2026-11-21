"""Append-only audit trail (ADR-008).

The app role may only read it. Rows are written by SECURITY DEFINER functions owned by the
owner role (revision 0004). Triggers block UPDATE, DELETE and TRUNCATE for every role.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE audit.event (
        seq             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        id              uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
        occurred_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
        actor_kind      audit.actor_kind NOT NULL,
        actor_id        text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 200),
        entity_type     text NOT NULL CHECK (entity_type IN ('task', 'commitment')),
        entity_id       uuid NOT NULL,
        action          audit.action NOT NULL,
        from_state      text,
        to_state        text,
        outcome         audit.outcome NOT NULL,
        reason          text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
        correlation_id  uuid,
        details         jsonb NOT NULL DEFAULT '{}'
    );
    CREATE INDEX ON audit.event (entity_type, entity_id, seq);
    CREATE INDEX ON audit.event (occurred_at);
    CREATE INDEX ON audit.event (correlation_id) WHERE correlation_id IS NOT NULL;

    REVOKE ALL ON audit.event FROM PUBLIC;
    GRANT SELECT ON audit.event TO jarvis_app;

    CREATE FUNCTION audit.refuse_change() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit.event is append-only (% refused)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END;
    $$;
    CREATE TRIGGER append_only_rows BEFORE UPDATE OR DELETE ON audit.event
        FOR EACH ROW EXECUTE FUNCTION audit.refuse_change();
    CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON audit.event
        FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_change();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE audit.event;
    DROP FUNCTION audit.refuse_change();
    """)
