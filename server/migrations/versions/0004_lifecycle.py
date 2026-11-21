"""State machine (ADR-007): allowed transitions as data, one door for state changes, audited creation.

- ledger.allowed_transition holds every legal move (spec §6.1 and §6.2).
- ledger.transition() is the only way to change a state. It writes an APPLIED or REJECTED audit
  row and returns 'APPLIED' or 'REJECTED' (it does not raise on an illegal move, so the refusal
  commits and is kept).
- New tasks/commitments must start in their initial state; creation is audited. The caller
  declares who is acting with set_config('jarvis.actor_kind' / 'jarvis.actor_id' / 'jarvis.reason'
  [/ 'jarvis.correlation_id'], ..., true) in the same transaction.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TASK = {
    "DETECTED": ["INTERPRETED", "CANCELLED", "SUPERSEDED"],
    "INTERPRETED": ["PROPOSED", "DEFERRED", "CANCELLED", "SUPERSEDED"],
    "PROPOSED": ["APPROVAL_PENDING", "READY", "DEFERRED", "CANCELLED", "SUPERSEDED"],
    "APPROVAL_PENDING": ["READY", "CANCELLED", "EXPIRED", "SUPERSEDED"],
    "READY": ["IN_PROGRESS", "BLOCKED", "DEFERRED", "CANCELLED", "SUPERSEDED"],
    "IN_PROGRESS": ["COMPLETED", "BLOCKED", "FAILED", "CANCELLED"],
    "BLOCKED": ["READY", "CANCELLED", "SUPERSEDED"],
    "DEFERRED": ["PROPOSED", "CANCELLED", "SUPERSEDED"],
}
COMMITMENT = {
    "EXPECTED": ["AT_RISK", "SECURED", "FULFILLED", "MISSED"],
    "AT_RISK": ["SECURED", "EXPECTED", "FULFILLED", "MISSED"],
    "SECURED": ["AT_RISK", "FULFILLED", "MISSED"],
}


def _rows() -> str:
    rows = [
        f"('{entity}', '{src}', '{dst}')"
        for entity, table in (("task", TASK), ("commitment", COMMITMENT))
        for src, dsts in table.items()
        for dst in dsts
    ]
    return ",\n        ".join(rows)


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE ledger.allowed_transition (
        entity_type  text NOT NULL CHECK (entity_type IN ('task', 'commitment')),
        from_state   text NOT NULL,
        to_state     text NOT NULL,
        PRIMARY KEY (entity_type, from_state, to_state)
    );
    INSERT INTO ledger.allowed_transition (entity_type, from_state, to_state) VALUES
        {_rows()};
    GRANT SELECT ON ledger.allowed_transition TO jarvis_app;

    -- Internal: append one audit row. Not callable by the app role.
    CREATE FUNCTION audit.append(
        p_actor_kind text, p_actor_id text, p_entity text, p_id uuid, p_action text,
        p_from text, p_to text, p_outcome text, p_reason text, p_correlation uuid,
        p_details jsonb DEFAULT '{{}}'
    ) RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        INSERT INTO audit.event (actor_kind, actor_id, entity_type, entity_id, action,
                                 from_state, to_state, outcome, reason, correlation_id, details)
        VALUES (p_actor_kind::audit.actor_kind, p_actor_id, p_entity, p_id,
                p_action::audit.action, p_from, p_to, p_outcome::audit.outcome,
                p_reason, p_correlation, coalesce(p_details, '{{}}'::jsonb));
    $$;
    REVOKE ALL ON FUNCTION audit.append FROM PUBLIC;

    CREATE FUNCTION ledger.transition(
        p_entity text, p_id uuid, p_to text,
        p_actor_kind text, p_actor_id text, p_reason text, p_correlation uuid DEFAULT NULL
    ) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_from text;
    BEGIN
        IF p_entity NOT IN ('task', 'commitment') THEN
            RAISE EXCEPTION 'unknown entity type %', p_entity USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_reason IS NULL OR btrim(p_reason) = '' THEN
            RAISE EXCEPTION 'a reason is required' USING ERRCODE = 'invalid_parameter_value';
        END IF;

        EXECUTE format('SELECT state::text FROM ledger.%I WHERE id = $1 FOR UPDATE', p_entity)
            INTO v_from USING p_id;

        IF v_from IS NULL THEN
            PERFORM audit.append(p_actor_kind, p_actor_id, p_entity, p_id, 'TRANSITION',
                                 NULL, p_to, 'REJECTED', 'not_found: ' || p_reason, p_correlation);
            RETURN 'REJECTED';
        END IF;

        IF NOT EXISTS (SELECT 1 FROM ledger.allowed_transition
                       WHERE entity_type = p_entity AND from_state = v_from AND to_state = p_to) THEN
            PERFORM audit.append(p_actor_kind, p_actor_id, p_entity, p_id, 'TRANSITION',
                                 v_from, p_to, 'REJECTED', p_reason, p_correlation);
            RETURN 'REJECTED';
        END IF;

        EXECUTE format('UPDATE ledger.%I SET state = $1::ledger.%I WHERE id = $2',
                       p_entity, p_entity || '_state')
            USING p_to, p_id;
        PERFORM audit.append(p_actor_kind, p_actor_id, p_entity, p_id, 'TRANSITION',
                             v_from, p_to, 'APPLIED', p_reason, p_correlation);
        RETURN 'APPLIED';
    END;
    $$;
    REVOKE ALL ON FUNCTION ledger.transition FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION ledger.transition TO jarvis_app;

    -- New records must start in their initial state.
    CREATE FUNCTION ledger.require_initial_state() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.state::text <> TG_ARGV[0] THEN
            RAISE EXCEPTION '% must be created in state %, not %', TG_TABLE_NAME, TG_ARGV[0], NEW.state
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    $$;

    -- Creation is audited; the caller must say who is acting.
    CREATE FUNCTION ledger.audit_create() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_kind text := nullif(current_setting('jarvis.actor_kind', true), '');
        v_actor text := nullif(current_setting('jarvis.actor_id', true), '');
        v_reason text := coalesce(nullif(current_setting('jarvis.reason', true), ''), 'created');
        v_corr uuid := nullif(current_setting('jarvis.correlation_id', true), '')::uuid;
    BEGIN
        IF v_kind IS NULL OR v_actor IS NULL THEN
            RAISE EXCEPTION 'set jarvis.actor_kind and jarvis.actor_id before creating a %',
                TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
        END IF;
        PERFORM audit.append(v_kind, v_actor, TG_TABLE_NAME, NEW.id, 'CREATE',
                             NULL, NEW.state::text, 'APPLIED', v_reason, v_corr);
        RETURN NULL;
    END;
    $$;
    REVOKE ALL ON FUNCTION ledger.audit_create FROM PUBLIC;

    CREATE TRIGGER initial_state BEFORE INSERT ON ledger.task
        FOR EACH ROW EXECUTE FUNCTION ledger.require_initial_state('DETECTED');
    CREATE TRIGGER initial_state BEFORE INSERT ON ledger.commitment
        FOR EACH ROW EXECUTE FUNCTION ledger.require_initial_state('EXPECTED');
    CREATE TRIGGER audit_create AFTER INSERT ON ledger.task
        FOR EACH ROW EXECUTE FUNCTION ledger.audit_create();
    CREATE TRIGGER audit_create AFTER INSERT ON ledger.commitment
        FOR EACH ROW EXECUTE FUNCTION ledger.audit_create();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER audit_create ON ledger.commitment;
    DROP TRIGGER audit_create ON ledger.task;
    DROP TRIGGER initial_state ON ledger.commitment;
    DROP TRIGGER initial_state ON ledger.task;
    DROP FUNCTION ledger.audit_create();
    DROP FUNCTION ledger.require_initial_state();
    DROP FUNCTION ledger.transition(text, uuid, text, text, text, text, uuid);
    DROP FUNCTION audit.append(text, text, text, uuid, text, text, text, text, text, uuid, jsonb);
    DROP TABLE ledger.allowed_transition;
    """)
