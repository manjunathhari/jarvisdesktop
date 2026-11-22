"""Gateway role surface and node registry (spec §4.1, §5.1, §7.2; R7-1 … R7-6).

- Schema gw: gw.node, gw.node_connection, gw.enrolment.
- jarvis_gateway may SELECT six columns of gw.node and EXECUTE exactly five functions:
  gw.enrol_node, gw.open_connection, gw.close_connection, gw.touch_connection and
  gw.current_node. It holds no other grant; every state change goes through those functions.
- jarvis_app may read the three tables and EXECUTE gw.create_enrolment and gw.revoke_node.
  Nobody inserts enrolments or nodes directly.
- Refused requests (a bad token, a key or identity in use, an unknown or revoked node) write
  the REJECTED audit row and return a reason code; they never raise, so the audit row
  survives (R7-1). A fenced connection raises 'connection_fenced' (SQLSTATE JF001, R7-5).
  A malformed call raises invalid_parameter_value.
- audit.event: entity_type 'node'; actions ENROL_CREATE, ENROL, REVOKE, CONNECT_REFUSED,
  CONNECT and DISCONNECT (R8-2). Audit rows carry codes and IDs only, never names or keys;
  heartbeats aren't audited.
- Strict connection fencing with one lock order, node row then connection rows (R8-1).

Downgrade refuses once a node audit row, node or enrolment exists (R7-6). The jarvis_gateway
role is cluster-wide and belongs to bootstrap, so it stays.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLES = """
CREATE SCHEMA gw;
REVOKE ALL ON SCHEMA gw FROM PUBLIC;
GRANT USAGE ON SCHEMA gw TO jarvis_app, jarvis_gateway;

CREATE TABLE gw.node (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id           uuid NOT NULL REFERENCES ledger.person(id),
    kind                text NOT NULL CHECK (kind IN ('DESKTOP', 'PHONE')),
    name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    public_key          bytea NOT NULL CHECK (length(public_key) = 32),
    tailscale_identity  text NOT NULL CHECK (length(tailscale_identity) BETWEEN 1 AND 200),
    registered_at       timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz,
    last_seen_at        timestamptz,
    CONSTRAINT node_public_key_unique UNIQUE (public_key)
);
-- One active node per Tailscale identity; a revoked node frees it for re-enrolment.
CREATE UNIQUE INDEX node_active_identity_unique ON gw.node (tailscale_identity)
    WHERE revoked_at IS NULL;

CREATE TABLE gw.node_connection (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id       uuid NOT NULL REFERENCES gw.node(id),
    opened_at     timestamptz NOT NULL DEFAULT now(),
    last_ping_at  timestamptz,
    closed_at     timestamptz,
    close_reason  text CHECK (close_reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    CHECK ((closed_at IS NULL) = (close_reason IS NULL))
);
-- At most one open connection per node: opening a new one fences (supersedes) the old one.
CREATE UNIQUE INDEX node_connection_one_open ON gw.node_connection (node_id)
    WHERE closed_at IS NULL;
CREATE INDEX ON gw.node_connection (opened_at);  -- 90-day retention (§5.7)

CREATE TABLE gw.enrolment (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash  bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),  -- SHA-256 of the token
    person_id   uuid NOT NULL REFERENCES ledger.person(id),
    kind        text NOT NULL CHECK (kind IN ('DESKTOP', 'PHONE')),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    node_id     uuid REFERENCES gw.node(id),
    CHECK (node_id IS NULL OR used_at IS NOT NULL)
);

REVOKE ALL ON gw.node, gw.node_connection, gw.enrolment FROM PUBLIC;
GRANT SELECT ON gw.node, gw.node_connection, gw.enrolment TO jarvis_app;
GRANT SELECT (id, person_id, kind, public_key, tailscale_identity, revoked_at)
    ON gw.node TO jarvis_gateway;
"""

FUNCTIONS = """
-- Core: a one-time enrolment, valid for 15 minutes from now (set here, never by the caller).
CREATE FUNCTION gw.create_enrolment(
    p_token_hash bytea, p_person_id uuid, p_kind text, p_name text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    IF p_token_hash IS NULL OR length(p_token_hash) <> 32
       OR p_kind IS NULL OR p_kind NOT IN ('DESKTOP', 'PHONE')
       OR p_name IS NULL OR length(p_name) NOT BETWEEN 1 AND 100
       OR p_person_id IS NULL
       OR NOT EXISTS (SELECT 1 FROM ledger.person p WHERE p.id = p_person_id) THEN
        RAISE EXCEPTION 'malformed create_enrolment call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO gw.enrolment (token_hash, person_id, kind, name, expires_at)
        VALUES (p_token_hash, p_person_id, p_kind, p_name, now() + interval '15 minutes')
        RETURNING id INTO v_id;
    PERFORM audit.append('AGENT', 'core', 'node', v_id, 'ENROL_CREATE', NULL, NULL, 'APPLIED',
                         'created', NULL,
                         jsonb_build_object('person_id', p_person_id, 'kind', p_kind));
    RETURN v_id;
END;
$$;

-- Gateway, path B (§6.1 step 6, §7.2 step 4). The token is consumed and the node inserted in one
-- protected block: a key or identity clash rolls back both, so the token stays unused.
CREATE FUNCTION gw.enrol_node(p_token text, p_public_key bytea, p_tailscale_identity text)
RETURNS TABLE (node_id uuid, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_hash bytea;
    v_enrolment uuid;
    v_person uuid;
    v_kind text;
    v_name text;
    v_node uuid;
    v_refusal text;
    v_constraint text;
BEGIN
    IF p_token IS NULL OR length(p_token) <> 26  -- the 128-bit token, base32 without padding
       OR p_public_key IS NULL OR length(p_public_key) <> 32
       OR p_tailscale_identity IS NULL OR length(p_tailscale_identity) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'malformed enrol_node call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_hash := pg_catalog.sha256(pg_catalog.convert_to(p_token, 'UTF8'));
    BEGIN
        UPDATE gw.enrolment e SET used_at = now()
            WHERE e.token_hash = v_hash AND e.used_at IS NULL AND e.expires_at > now()
            RETURNING e.id, e.person_id, e.kind, e.name
            INTO v_enrolment, v_person, v_kind, v_name;
        IF NOT FOUND THEN
            v_refusal := 'bad_token';
        ELSE
            INSERT INTO gw.node (person_id, kind, name, public_key, tailscale_identity)
                VALUES (v_person, v_kind, v_name, p_public_key, p_tailscale_identity)
                RETURNING id INTO v_node;
            UPDATE gw.enrolment e SET node_id = v_node WHERE e.id = v_enrolment;
        END IF;
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
        IF v_constraint = 'node_public_key_unique' THEN
            v_refusal := 'key_in_use';
        ELSIF v_constraint = 'node_active_identity_unique' THEN
            v_refusal := 'identity_in_use';
        ELSE
            RAISE;
        END IF;
    END;

    IF v_refusal IS NOT NULL THEN
        IF v_enrolment IS NULL THEN
            SELECT e.id INTO v_enrolment FROM gw.enrolment e WHERE e.token_hash = v_hash;
        END IF;
        -- The enrolment's ID when the token matched one (used or expired), else the nil UUID.
        PERFORM audit.append('AGENT', 'gateway', 'node',
                             coalesce(v_enrolment, '00000000-0000-0000-0000-000000000000'::uuid),
                             'ENROL', NULL, NULL, 'REJECTED', v_refusal, NULL,
                             jsonb_strip_nulls(jsonb_build_object('enrolment_id', v_enrolment)));
        RETURN QUERY SELECT NULL::uuid, v_refusal;
        RETURN;
    END IF;
    PERFORM audit.append('AGENT', 'gateway', 'node', v_node, 'ENROL', NULL, NULL, 'APPLIED',
                         'enrolled', NULL,
                         jsonb_build_object('enrolment_id', v_enrolment, 'person_id', v_person,
                                            'kind', v_kind));
    RETURN QUERY SELECT v_node, NULL::text;
END;
$$;

-- Strict fencing (R8-1). One lock order everywhere: the node row first, then connection rows.
-- A connection's node_id never changes, so reading it before taking any lock is safe. Under
-- READ COMMITTED a lock request that waited re-reads the committed row, so the fence
-- conditions below are checked against what has actually committed.
--
--   function           node row            connection row(s)
--   current_node       FOR SHARE           FOR SHARE (this one)
--   touch_connection   FOR NO KEY UPDATE   FOR NO KEY UPDATE (this one)
--   close_connection   FOR SHARE           FOR UPDATE (this one)
--   open_connection    FOR UPDATE          FOR UPDATE (the node's open one, if any)
--   revoke_node        FOR UPDATE          FOR UPDATE (all the node's open ones)
--
-- DISCONNECT and CONNECT audit rows (R8-2) are written in the same transaction as the change.

-- The fence, called first by every conv and comm connection function (Tasks 3 and 7). Its
-- share locks are held until that transaction ends, so a revocation or supersede waits for
-- the write, and a write after one is refused with JF001 (R7-5).
CREATE FUNCTION gw.current_node(p_conn_id bigint) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
BEGIN
    IF p_conn_id IS NULL THEN
        RAISE EXCEPTION 'malformed current_node call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.node_id INTO v_node FROM gw.node_connection c WHERE c.id = p_conn_id;
    IF FOUND THEN
        PERFORM 1 FROM gw.node n WHERE n.id = v_node AND n.revoked_at IS NULL FOR SHARE;
    END IF;
    IF FOUND THEN
        PERFORM 1 FROM gw.node_connection c WHERE c.id = p_conn_id AND c.closed_at IS NULL
            FOR SHARE;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'connection_fenced' USING ERRCODE = 'JF001';
    END IF;
    RETURN v_node;
END;
$$;

-- Gateway: open a connection for a node, superseding (closing) any open one in the same
-- transaction.
CREATE FUNCTION gw.open_connection(p_node_id uuid)
RETURNS TABLE (conn_id bigint, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_found boolean;
    v_revoked timestamptz;
    v_refusal text;
    v_old bigint;
    v_conn bigint;
BEGIN
    IF p_node_id IS NULL THEN
        RAISE EXCEPTION 'malformed open_connection call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT true, n.revoked_at INTO v_found, v_revoked FROM gw.node n WHERE n.id = p_node_id
        FOR UPDATE;
    IF v_found IS NULL THEN
        v_refusal := 'unknown_node';
    ELSIF v_revoked IS NOT NULL THEN
        v_refusal := 'revoked';
    END IF;
    IF v_refusal IS NOT NULL THEN
        PERFORM audit.append('AGENT', 'gateway', 'node', p_node_id, 'CONNECT_REFUSED', NULL, NULL,
                             'REJECTED', v_refusal, NULL, '{}'::jsonb);
        RETURN QUERY SELECT NULL::bigint, v_refusal;
        RETURN;
    END IF;
    SELECT c.id INTO v_old FROM gw.node_connection c
        WHERE c.node_id = p_node_id AND c.closed_at IS NULL FOR UPDATE;
    IF v_old IS NOT NULL THEN
        UPDATE gw.node_connection c SET closed_at = now(), close_reason = 'superseded'
            WHERE c.id = v_old;
        PERFORM audit.append('AGENT', 'gateway', 'node', p_node_id, 'DISCONNECT', NULL, NULL,
                             'APPLIED', 'superseded', NULL,
                             jsonb_build_object('node_id', p_node_id, 'conn_id', v_old,
                                                'reason', 'superseded'));
    END IF;
    INSERT INTO gw.node_connection (node_id) VALUES (p_node_id) RETURNING id INTO v_conn;
    PERFORM audit.append('AGENT', 'gateway', 'node', p_node_id, 'CONNECT', NULL, NULL, 'APPLIED',
                         'connected', NULL,
                         jsonb_build_object('node_id', p_node_id, 'conn_id', v_conn,
                                            'superseded_conn_id', v_old));
    RETURN QUERY SELECT v_conn, NULL::text;
END;
$$;

-- Gateway: close a connection with a reason code. True when it closed it; false, with no
-- second DISCONNECT, when it was already closed (also after a revocation, which closes it).
CREATE FUNCTION gw.close_connection(p_conn_id bigint, p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
    v_closed timestamptz;
BEGIN
    IF p_conn_id IS NULL OR p_reason IS NULL OR p_reason !~ '^[a-z][a-z0-9_]{0,63}$' THEN
        RAISE EXCEPTION 'malformed close_connection call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.node_id INTO v_node FROM gw.node_connection c WHERE c.id = p_conn_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown connection' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM 1 FROM gw.node n WHERE n.id = v_node FOR SHARE;
    SELECT c.closed_at INTO v_closed FROM gw.node_connection c WHERE c.id = p_conn_id FOR UPDATE;
    IF v_closed IS NOT NULL THEN
        RETURN false;
    END IF;
    UPDATE gw.node_connection c SET closed_at = now(), close_reason = p_reason
        WHERE c.id = p_conn_id;
    PERFORM audit.append('AGENT', 'gateway', 'node', v_node, 'DISCONNECT', NULL, NULL, 'APPLIED',
                         p_reason, NULL,
                         jsonb_build_object('node_id', v_node, 'conn_id', p_conn_id,
                                            'reason', p_reason));
    RETURN true;
END;
$$;

-- Gateway: the heartbeat. Takes its stronger locks from the start (no share lock to upgrade),
-- and repeats both fence conditions in its own updates. Not audited.
CREATE FUNCTION gw.touch_connection(p_conn_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
BEGIN
    IF p_conn_id IS NULL THEN
        RAISE EXCEPTION 'malformed touch_connection call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.node_id INTO v_node FROM gw.node_connection c WHERE c.id = p_conn_id;
    IF FOUND THEN
        PERFORM 1 FROM gw.node n WHERE n.id = v_node AND n.revoked_at IS NULL
            FOR NO KEY UPDATE;
    END IF;
    IF FOUND THEN
        PERFORM 1 FROM gw.node_connection c WHERE c.id = p_conn_id AND c.closed_at IS NULL
            FOR NO KEY UPDATE;
    END IF;
    IF FOUND THEN
        UPDATE gw.node n SET last_seen_at = now() WHERE n.id = v_node AND n.revoked_at IS NULL;
    END IF;
    IF FOUND THEN
        UPDATE gw.node_connection c SET last_ping_at = now()
            WHERE c.id = p_conn_id AND c.closed_at IS NULL;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'connection_fenced' USING ERRCODE = 'JF001';
    END IF;
END;
$$;

-- Core: revoke a node and close its open connections, which fences them at once.
CREATE FUNCTION gw.revoke_node(p_node_id uuid)
RETURNS TABLE (applied boolean, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_found boolean;
    v_revoked timestamptz;
    v_refusal text;
    v_conn bigint;
BEGIN
    IF p_node_id IS NULL THEN
        RAISE EXCEPTION 'malformed revoke_node call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT true, n.revoked_at INTO v_found, v_revoked FROM gw.node n WHERE n.id = p_node_id
        FOR UPDATE;
    IF v_found IS NULL THEN
        v_refusal := 'unknown_node';
    ELSIF v_revoked IS NOT NULL THEN
        v_refusal := 'already_revoked';
    END IF;
    IF v_refusal IS NOT NULL THEN
        PERFORM audit.append('AGENT', 'core', 'node', p_node_id, 'REVOKE', NULL, NULL,
                             'REJECTED', v_refusal, NULL, '{}'::jsonb);
        RETURN QUERY SELECT false, v_refusal;
        RETURN;
    END IF;
    UPDATE gw.node n SET revoked_at = now() WHERE n.id = p_node_id;
    PERFORM audit.append('AGENT', 'core', 'node', p_node_id, 'REVOKE', NULL, NULL, 'APPLIED',
                         'revoked', NULL, '{}'::jsonb);
    FOR v_conn IN
        SELECT c.id FROM gw.node_connection c
            WHERE c.node_id = p_node_id AND c.closed_at IS NULL ORDER BY c.id FOR UPDATE
    LOOP
        UPDATE gw.node_connection c SET closed_at = now(), close_reason = 'revoked'
            WHERE c.id = v_conn;
        PERFORM audit.append('AGENT', 'core', 'node', p_node_id, 'DISCONNECT', NULL, NULL,
                             'APPLIED', 'revoked', NULL,
                             jsonb_build_object('node_id', p_node_id, 'conn_id', v_conn,
                                                'reason', 'revoked'));
    END LOOP;
    RETURN QUERY SELECT true, NULL::text;
END;
$$;

REVOKE ALL ON FUNCTION gw.create_enrolment(bytea, uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.enrol_node(text, bytea, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.current_node(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.open_connection(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.close_connection(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.touch_connection(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION gw.revoke_node(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION gw.create_enrolment(bytea, uuid, text, text) TO jarvis_app;
GRANT EXECUTE ON FUNCTION gw.revoke_node(uuid) TO jarvis_app;
GRANT EXECUTE ON FUNCTION gw.enrol_node(text, bytea, text) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION gw.open_connection(uuid) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION gw.close_connection(bigint, text) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION gw.touch_connection(bigint) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION gw.current_node(bigint) TO jarvis_gateway;
"""

NEW_ACTIONS = ("ENROL_CREATE", "ENROL", "REVOKE", "CONNECT_REFUSED", "CONNECT", "DISCONNECT")


def upgrade() -> None:
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit.action ADD VALUE IF NOT EXISTS '{action}'")
    op.execute("""
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment', 'memory', 'node'));
    """)
    op.execute(TABLES)
    op.execute(FUNCTIONS)


def downgrade() -> None:
    actions = ", ".join(f"'{a}'" for a in NEW_ACTIONS)
    op.execute(f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM audit.event
                   WHERE entity_type = 'node' OR action::text IN ({actions}))
           OR EXISTS (SELECT 1 FROM gw.node)
           OR EXISTS (SELECT 1 FROM gw.enrolment) THEN
            RAISE EXCEPTION 'node rows exist (audit rows, nodes or enrolments); 0008 cannot be '
                            'undone without rewriting the append-only audit trail (ADR-008)';
        END IF;
    END $$;
    """)
    op.execute("""
    DROP FUNCTION gw.revoke_node(uuid);
    DROP FUNCTION gw.touch_connection(bigint);
    DROP FUNCTION gw.close_connection(bigint, text);
    DROP FUNCTION gw.open_connection(uuid);
    DROP FUNCTION gw.current_node(bigint);
    DROP FUNCTION gw.enrol_node(text, bytea, text);
    DROP FUNCTION gw.create_enrolment(bytea, uuid, text, text);
    DROP TABLE gw.enrolment;
    DROP TABLE gw.node_connection;
    DROP TABLE gw.node;
    DROP SCHEMA gw;

    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment', 'memory'));
    -- Remove the added enum values by rebuilding the type (no rows use them).
    ALTER TYPE audit.action RENAME TO action_0008;
    CREATE TYPE audit.action AS ENUM ('CREATE', 'TRANSITION', 'UPDATE', 'CONFIRM', 'ARCHIVE');
    ALTER TABLE audit.event ALTER COLUMN action TYPE audit.action USING action::text::audit.action;
    DROP TYPE audit.action_0008;
    """)
