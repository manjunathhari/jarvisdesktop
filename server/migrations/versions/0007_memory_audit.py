"""Memory audit (ADR-010): audit.event records memory changes, never their content.

- entity_type 'memory'; actions UPDATE, CONFIRM, ARCHIVE.
- audit.record_memory(): the only way the app records a memory change. entity_id is the
  UUID v3 of the memory reference (m-NNNN, person:<slug>, m-unassigned) in MEMORY_NAMESPACE;
  details holds {"memory_id", "commit"}. Every argument is checked against a fixed shape, so
  no free text can reach the append-only row: the reason is a short code, the actor is a
  person UUID, an agent name, 'system' or 'unknown', and the tag change must be legal for the
  action (spec §7).

Downgrade refuses once memory audit rows exist: the audit trail is append-only (ADR-008).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

NAMESPACE = "6a1c3f8e-2b4d-4e6f-9a0b-1c2d3e4f5a6b"  # = jarvis_server.memory.pg.MEMORY_NAMESPACE


def upgrade() -> None:
    op.execute("ALTER TYPE audit.action ADD VALUE IF NOT EXISTS 'UPDATE'")
    op.execute("ALTER TYPE audit.action ADD VALUE IF NOT EXISTS 'CONFIRM'")
    op.execute("ALTER TYPE audit.action ADD VALUE IF NOT EXISTS 'ARCHIVE'")
    op.execute(f"""
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment', 'memory'));

    -- UUID v3 (RFC 4122, MD5) of a memory reference, identical to Python's uuid.uuid3.
    CREATE FUNCTION audit.memory_entity_id(p_ref text) RETURNS uuid
    LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
        SELECT (substr(h, 1, 12) || '3' || substr(h, 14, 3)
                || to_hex((get_byte(decode(substr(h, 17, 2), 'hex'), 0) & 63) | 128)
                || substr(h, 19))::uuid
        FROM (SELECT md5(decode(replace('{NAMESPACE}', '-', ''), 'hex')
                         || convert_to(p_ref, 'UTF8')) AS h) s;
    $$;

    CREATE FUNCTION audit.record_memory(
        p_actor_kind text, p_actor_id text, p_ref text, p_action text,
        p_from text, p_to text, p_outcome text, p_reason text, p_commit text
    ) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
        IF p_ref !~ '^(m-[0-9]{{4,}}|m-unassigned|person:[a-z][a-z0-9-]{{1,30}})$' THEN
            RAISE EXCEPTION 'bad memory reference' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_action NOT IN ('CREATE', 'UPDATE', 'CONFIRM', 'ARCHIVE') THEN
            RAISE EXCEPTION 'bad memory action' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF coalesce(p_from, 'STATED') NOT IN ('STATED','CONFIRMED','INFERRED','HISTORICAL','EXPIRED')
           OR coalesce(p_to, 'STATED') NOT IN ('STATED','CONFIRMED','INFERRED','HISTORICAL','EXPIRED') THEN
            RAISE EXCEPTION 'bad memory tag' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_reason !~ '^[a-z][a-z0-9-]{{0,63}}$' THEN
            RAISE EXCEPTION 'memory audit reason must be a short code'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF NOT (
            (p_actor_kind = 'PERSON' AND (p_actor_id = 'unknown' OR p_actor_id ~
                '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'))
            OR (p_actor_kind = 'AGENT' AND p_actor_id ~ '^[a-z][a-z0-9-]{{0,39}}$')
            OR (p_actor_kind = 'SYSTEM' AND p_actor_id = 'system')
        ) THEN
            RAISE EXCEPTION 'bad memory actor' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_outcome = 'APPLIED' AND NOT (
            (p_action = 'CREATE' AND p_from IS NULL
                AND (p_to IN ('STATED', 'INFERRED', 'HISTORICAL')
                     OR (p_to IS NULL AND p_ref LIKE 'person:%')))
            OR (p_action = 'UPDATE' AND (p_from = p_to OR (p_from = 'CONFIRMED' AND p_to = 'STATED'))
                AND p_from <> 'EXPIRED')
            OR (p_action = 'CONFIRM' AND p_from IN ('STATED', 'INFERRED') AND p_to = 'CONFIRMED')
            OR (p_action = 'ARCHIVE' AND p_from <> 'EXPIRED' AND p_to = 'EXPIRED')
        ) THEN
            RAISE EXCEPTION 'illegal memory tag change' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_outcome = 'REJECTED' AND (p_from IS NOT NULL OR p_to IS NOT NULL) THEN
            RAISE EXCEPTION 'refusals carry no tags' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_commit IS NOT NULL AND p_commit !~ '^[0-9a-f]{{40}}$' THEN
            RAISE EXCEPTION 'bad commit' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_outcome = 'APPLIED') <> (p_commit IS NOT NULL) THEN
            RAISE EXCEPTION 'applied memory changes need a commit, refusals none'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        PERFORM audit.append(
            p_actor_kind, p_actor_id, 'memory', audit.memory_entity_id(p_ref), p_action,
            p_from, p_to, p_outcome, p_reason, NULL,
            jsonb_strip_nulls(jsonb_build_object('memory_id', p_ref, 'commit', p_commit)));
    END;
    $$;
    REVOKE ALL ON FUNCTION audit.record_memory FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION audit.record_memory TO jarvis_app;
    REVOKE ALL ON FUNCTION audit.memory_entity_id FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION audit.memory_entity_id TO jarvis_app;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM audit.event
                   WHERE entity_type = 'memory' OR action::text IN ('UPDATE','CONFIRM','ARCHIVE')) THEN
            RAISE EXCEPTION 'memory audit rows exist; 0007 cannot be undone without rewriting '
                            'the append-only audit trail (ADR-008)';
        END IF;
    END $$;
    DROP FUNCTION audit.record_memory(text, text, text, text, text, text, text, text, text);
    DROP FUNCTION audit.memory_entity_id(text);
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment'));
    -- Remove the added enum values by rebuilding the type (no rows use them).
    ALTER TYPE audit.action RENAME TO action_0007;
    CREATE TYPE audit.action AS ENUM ('CREATE', 'TRANSITION');
    ALTER TABLE audit.event ALTER COLUMN action TYPE audit.action USING action::text::audit.action;
    DROP TYPE audit.action_0007;
    """)
