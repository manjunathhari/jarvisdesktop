"""Conversations, append-only events, outbox and multi-node delivery (spec §4.1, §5.2, §5.3,
§8.2; R7-1, R8-1).

- Schema conv: conv.session (one open session per person), conv.inbound, conv.message,
  conv.stream_chunk, conv.stream_cursor and conv.session_ticket.
- Schema comm: comm.event (append-only, codes and IDs only), comm.outbox and comm.delivery.
- jarvis_gateway may SELECT conv.stream_chunk, conv.stream_cursor, comm.outbox and
  comm.delivery, and EXECUTE conv.record_inbound, comm.record_sent, comm.record_received,
  comm.acknowledge and conv.record_chunk_received. Each takes a connection ID, never a node ID,
  and calls gw.current_node(p_conn_id) first, in the same transaction; it takes no other lock
  on gw.node or gw.node_connection itself, and never calls the heartbeat (R8-1).
- jarvis_app writes conversations and events, creates outbox items (always PENDING), and
  EXECUTEs comm.supersede, comm.supersede_for_create (create_outbox only, R11-4) and
  comm.expire. Outbox state changes only through these functions: each locks the item FOR
  UPDATE and moves its state forward only. Every call writes one audit row, except a 'skipped'
  comm.supersede_for_create, which is a non-audited no-op (R11-2). A trigger refuses any other
  change, for every role.
- Refused requests write the REJECTED audit row and return a reason code (R7-1): an unknown
  inbound kind (unknown_kind); a message_id already recorded by another node (id_conflict,
  R9-4); an outbox item unknown or of another person (unknown_item, R9-5); a stream chunk of
  another person's session (other_person, R9-1). Each connection function acts only on records
  of the fenced node's person. A fenced connection raises JF001. A
  malformed call raises invalid_parameter_value.
- audit.event: entity types 'inbound', 'outbox' and 'stream'; actions INBOUND, SEND, RECEIVE,
  ACK, SUPERSEDE, EXPIRE and CHUNK_RECEIVE. Rows carry codes and IDs only.

The trigger functions comm.refuse_change and comm.outbox_guard have PUBLIC EXECUTE revoked
like every other function here.

Foreign keys from conv.inbound, comm.delivery and conv.stream_cursor to gw.node take a KEY SHARE
lock on the same node row that gw.current_node already holds FOR SHARE in that transaction, so
they can never wait and add nothing to the lock order.

Downgrade refuses once any conv or comm row, or any audit row of the new entity types or
actions, exists.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = r"""
CREATE SCHEMA conv;
CREATE SCHEMA comm;
REVOKE ALL ON SCHEMA conv, comm FROM PUBLIC;
GRANT USAGE ON SCHEMA conv, comm TO jarvis_app, jarvis_gateway;

-- §5.2. A person has at most one open session.
CREATE TABLE conv.session (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id         uuid NOT NULL REFERENCES ledger.person(id),
    started_at        timestamptz NOT NULL DEFAULT now(),
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    CHECK (closed_at IS NULL OR closed_at >= started_at)
);
CREATE UNIQUE INDEX session_one_open ON conv.session (person_id) WHERE closed_at IS NULL;

-- id is the node's message_id, so a resend is a no-op. person_id comes from the node row.
CREATE TABLE conv.inbound (
    id           uuid PRIMARY KEY,
    node_id      uuid NOT NULL REFERENCES gw.node(id),
    person_id    uuid NOT NULL REFERENCES ledger.person(id),
    session_id   uuid REFERENCES conv.session(id),
    kind         text NOT NULL CHECK (kind IN ('utterance', 'approval.decision', 'action.receipt')),
    payload      jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    received_at  timestamptz NOT NULL DEFAULT now(),
    outcome      text CHECK (outcome IN ('ANSWERED', 'INDETERMINATE', 'REFUSED'))
);
CREATE INDEX ON conv.inbound (received_at);  -- 30-day retention (§5.7)

-- inbound_id has no foreign key: conv.inbound is pruned after 30 days, messages are kept.
CREATE TABLE conv.message (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid NOT NULL REFERENCES conv.session(id),
    seq         integer NOT NULL CHECK (seq >= 1),
    role        text NOT NULL CHECK (role IN ('PERSON', 'JARVIS', 'SYSTEM')),
    person_id   uuid NOT NULL REFERENCES ledger.person(id),
    node_id     uuid REFERENCES gw.node(id),
    text        text NOT NULL CHECK (length(text) BETWEEN 1 AND 20000),
    inbound_id  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq),
    CHECK (role <> 'JARVIS' OR node_id IS NULL),
    CHECK (role <> 'PERSON' OR node_id IS NOT NULL)
);

CREATE TABLE conv.stream_chunk (
    reply_id        uuid NOT NULL,
    session_id      uuid NOT NULL REFERENCES conv.session(id),  -- whose reply it is (R9-1)
    seq             integer NOT NULL CHECK (seq >= 1),
    type            text NOT NULL CHECK (type IN ('SPEECH', 'ACTION')),
    text            text CHECK (length(text) BETWEEN 1 AND 4000),
    authz           jsonb CHECK (jsonb_typeof(authz) = 'object'),
    final           boolean NOT NULL,
    target_node_id  uuid REFERENCES gw.node(id),
    valid_until     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (reply_id, seq),
    CHECK ((type = 'SPEECH' AND text IS NOT NULL AND authz IS NULL
            AND target_node_id IS NULL AND valid_until IS NULL)
        OR (type = 'ACTION' AND text IS NULL AND authz IS NOT NULL
            AND target_node_id IS NOT NULL AND valid_until IS NOT NULL))
);
CREATE INDEX ON conv.stream_chunk (created_at);  -- 24 h retention (§5.7)

-- The durable stream cursor: changed only through conv.record_chunk_received, deleted with
-- its chunks after 24 h.
CREATE TABLE conv.stream_cursor (
    reply_id   uuid NOT NULL,
    node_id    uuid NOT NULL REFERENCES gw.node(id),
    acked_seq  integer NOT NULL DEFAULT 0 CHECK (acked_seq >= 0),
    PRIMARY KEY (reply_id, node_id)
);

CREATE TABLE conv.session_ticket (
    session_id  uuid NOT NULL REFERENCES conv.session(id),
    task_id     uuid NOT NULL REFERENCES ledger.task(id),
    PRIMARY KEY (session_id, task_id)
);

-- §5.3. Append-only; codes and IDs only, never free text.
CREATE TABLE comm.event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    subject_type  text NOT NULL CHECK (subject_type ~ '^[a-z][a-z0-9_.]{0,63}$'),
    subject_id    text NOT NULL CHECK (subject_id ~ '^[A-Za-z0-9_.:-]{1,100}$'),
    person_id     uuid REFERENCES ledger.person(id),
    details       jsonb NOT NULL DEFAULT '{}' CHECK (
        -- A flat object of code or ID strings (R9-2). Strict jsonpath: lax mode would unwrap
        -- arrays and let {"k": ["x"]} or {"k": []} through.
        jsonb_typeof(details) = 'object'
        AND NOT jsonb_path_exists(details, 'strict $.* ? (@.type() != "string")', '{}', true)
        AND NOT jsonb_path_exists(details, 'strict $.* ? (!(@ like_regex "^[A-Za-z0-9_.:-]{1,100}$"))', '{}', true)
        AND NOT jsonb_path_exists(details, 'strict $.keyvalue() ? (!(@.key like_regex "^[a-z][a-z0-9_.]{0,63}$"))', '{}', true)
    ),
    occurred_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ON comm.event (occurred_at);

CREATE FUNCTION comm.refuse_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'comm.event is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE TRIGGER append_only_rows BEFORE UPDATE OR DELETE ON comm.event
    FOR EACH ROW EXECUTE FUNCTION comm.refuse_change();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON comm.event
    FOR EACH STATEMENT EXECUTE FUNCTION comm.refuse_change();
REVOKE ALL ON FUNCTION comm.refuse_change() FROM PUBLIC;  -- a trigger function, never called

CREATE TABLE comm.outbox (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id         uuid NOT NULL REFERENCES ledger.person(id),
    event_id          uuid NOT NULL REFERENCES comm.event(id),
    priority          text NOT NULL CHECK (priority IN ('URGENT', 'TODAY', 'DIGEST', 'SILENT')),
    kind              text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    body              jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    authz             jsonb CHECK (jsonb_typeof(authz) = 'object'),
    group_key         text NOT NULL CHECK (group_key ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    supersedes_id     uuid REFERENCES comm.outbox(id),
    expires_at        timestamptz,
    state             text NOT NULL DEFAULT 'PENDING' CHECK (state IN
                      ('PENDING', 'DELIVERING', 'DELIVERED', 'ACKNOWLEDGED', 'SUPERSEDED',
                       'EXPIRED')),
    state_changed_at  timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);
CREATE INDEX ON comm.outbox (person_id, state);

-- A new item starts PENDING. Afterwards only state and state_changed_at change, and the state
-- only moves forward (PENDING < DELIVERING < DELIVERED < ACKNOWLEDGED; SUPERSEDED or EXPIRED
-- from any non-terminal state). Terminal states never change. Applies to every role.
CREATE FUNCTION comm.outbox_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_order text[] := ARRAY['PENDING', 'DELIVERING', 'DELIVERED', 'ACKNOWLEDGED'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.state := 'PENDING';
        NEW.state_changed_at := now();
        NEW.created_at := now();
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.person_id, NEW.event_id, NEW.priority, NEW.kind, NEW.body, NEW.authz,
        NEW.group_key, NEW.supersedes_id, NEW.expires_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.person_id, OLD.event_id, OLD.priority, OLD.kind, OLD.body, OLD.authz,
        OLD.group_key, OLD.supersedes_id, OLD.expires_at, OLD.created_at) THEN
        RAISE EXCEPTION 'an outbox item changes only its state' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        OLD.state IN ('PENDING', 'DELIVERING', 'DELIVERED')
        AND (NEW.state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED')
             OR array_position(v_order, NEW.state) > array_position(v_order, OLD.state))
    ) THEN
        RAISE EXCEPTION 'outbox state % cannot move to %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER outbox_guard BEFORE INSERT OR UPDATE ON comm.outbox
    FOR EACH ROW EXECUTE FUNCTION comm.outbox_guard();
REVOKE ALL ON FUNCTION comm.outbox_guard() FROM PUBLIC;  -- a trigger function, never called

-- One row per item per node, updated on each retry.
CREATE TABLE comm.delivery (
    outbox_id      uuid NOT NULL REFERENCES comm.outbox(id),
    node_id        uuid NOT NULL REFERENCES gw.node(id),
    attempts       integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    first_sent_at  timestamptz,
    last_sent_at   timestamptz,
    received_at    timestamptz,
    PRIMARY KEY (outbox_id, node_id),
    CHECK ((attempts = 0) = (first_sent_at IS NULL)),
    CHECK ((first_sent_at IS NULL) = (last_sent_at IS NULL))
);
CREATE INDEX ON comm.delivery (last_sent_at);  -- 90-day retention (§5.7)

REVOKE ALL ON conv.session, conv.inbound, conv.message, conv.stream_chunk, conv.stream_cursor,
    conv.session_ticket, comm.event, comm.outbox, comm.delivery FROM PUBLIC;

-- Core.
GRANT SELECT, INSERT, UPDATE ON conv.session TO jarvis_app;
GRANT SELECT, DELETE ON conv.inbound TO jarvis_app;
GRANT UPDATE (session_id, outcome) ON conv.inbound TO jarvis_app;
GRANT SELECT, INSERT ON conv.message TO jarvis_app;
GRANT SELECT, INSERT, DELETE ON conv.stream_chunk TO jarvis_app;
GRANT SELECT, DELETE ON conv.stream_cursor TO jarvis_app;
GRANT SELECT, INSERT ON conv.session_ticket TO jarvis_app;
GRANT SELECT, INSERT ON comm.event TO jarvis_app;
GRANT SELECT, INSERT ON comm.outbox TO jarvis_app;
GRANT SELECT, DELETE ON comm.delivery TO jarvis_app;

-- The gateway (§4.1): reads only; every change goes through the functions below.
GRANT SELECT ON conv.stream_chunk, conv.stream_cursor, comm.outbox, comm.delivery
    TO jarvis_gateway;
"""

FUNCTIONS = r"""
-- Gateway: one message from a node (§6.2). The node and person come from the connection, never
-- from the message. A resend of the same message_id by the same node is a no-op, (false, NULL);
-- one already recorded by a different node is refused, (false, 'id_conflict') (R9-4).
CREATE FUNCTION conv.record_inbound(
    p_conn_id bigint, p_message_id uuid, p_kind text, p_payload jsonb
) RETURNS TABLE (recorded boolean, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_node uuid;
    v_person uuid;
    v_job_kind text;
    v_inserted uuid;
    v_owner uuid;
    v_job bigint;
BEGIN
    v_node := gw.current_node(p_conn_id);  -- the fence, first (R8-1)
    IF p_message_id IS NULL OR p_kind IS NULL OR p_payload IS NULL
       OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'malformed record_inbound call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_job_kind := CASE p_kind
        WHEN 'utterance' THEN 'conv.turn'
        WHEN 'approval.decision' THEN 'approval.decision'
        WHEN 'action.receipt' THEN 'action.receipt'
    END;
    IF v_job_kind IS NULL THEN
        -- Never the kind itself in the audit row: it is untrusted text from the node.
        PERFORM audit.append('AGENT', 'gateway', 'inbound', p_message_id, 'INBOUND', NULL, NULL,
                             'REJECTED', 'unknown_kind', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN QUERY SELECT false, 'unknown_kind'::text;
        RETURN;
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;
    INSERT INTO conv.inbound (id, node_id, person_id, kind, payload)
        VALUES (p_message_id, v_node, v_person, p_kind, p_payload)
        ON CONFLICT (id) DO NOTHING
        RETURNING id INTO v_inserted;
    IF v_inserted IS NULL THEN
        SELECT i.node_id INTO v_owner FROM conv.inbound i WHERE i.id = p_message_id;
        IF v_owner IS DISTINCT FROM v_node THEN
            PERFORM audit.append('AGENT', 'gateway', 'inbound', p_message_id, 'INBOUND', NULL,
                                 NULL, 'REJECTED', 'id_conflict', NULL,
                                 jsonb_build_object('node_id', v_node));
            RETURN QUERY SELECT false, 'id_conflict'::text;
            RETURN;
        END IF;
        RETURN QUERY SELECT false, NULL::text;
        RETURN;
    END IF;
    INSERT INTO ops.job (kind, payload)
        VALUES (v_job_kind, jsonb_build_object('inbound_id', p_message_id))
        RETURNING id INTO v_job;
    PERFORM pg_notify('jarvis_jobs', v_job::text);
    RETURN QUERY SELECT true, NULL::text;
END;
$$;

-- Gateway: the item is about to be sent to this connection's node (§8.2). False ("don't send")
-- when it is terminal, or not addressed to this node's person. Creates or updates the
-- (item, node) delivery row: attempts + 1.
CREATE FUNCTION comm.record_sent(p_conn_id bigint, p_outbox_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
    v_person uuid;
    v_state text;
    v_new text;
BEGIN
    v_node := gw.current_node(p_conn_id);  -- the fence, first (R8-1)
    IF p_outbox_id IS NULL THEN
        RAISE EXCEPTION 'malformed record_sent call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;  -- no lock of its own
    SELECT o.state INTO v_state FROM comm.outbox o
        WHERE o.id = p_outbox_id AND o.person_id = v_person FOR UPDATE;
    IF v_state IS NULL THEN
        PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'SEND', NULL, NULL,
                             'REJECTED', 'unknown_item', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN false;
    END IF;
    IF v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED') THEN
        PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'SEND', v_state, NULL,
                             'REJECTED', 'terminal', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN false;
    END IF;
    INSERT INTO comm.delivery AS d (outbox_id, node_id, attempts, first_sent_at, last_sent_at)
        VALUES (p_outbox_id, v_node, 1, now(), now())
        ON CONFLICT (outbox_id, node_id) DO UPDATE
        SET attempts = d.attempts + 1,
            first_sent_at = coalesce(d.first_sent_at, now()),
            last_sent_at = now();
    v_new := CASE WHEN v_state = 'PENDING' THEN 'DELIVERING' ELSE v_state END;
    IF v_new <> v_state THEN
        UPDATE comm.outbox o SET state = v_new, state_changed_at = now() WHERE o.id = p_outbox_id;
    END IF;
    PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'SEND', v_state, v_new,
                         'APPLIED', 'sent', NULL, jsonb_build_object('node_id', v_node));
    RETURN true;
END;
$$;

-- Gateway: the node confirmed receipt. Returns the item's state afterwards, or the refusal
-- 'unknown_item' (lower case, never a state name). A late receipt for a terminal item is
-- recorded on the delivery row and changes nothing else.
CREATE FUNCTION comm.record_received(p_conn_id bigint, p_outbox_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
    v_person uuid;
    v_state text;
    v_new text;
BEGIN
    v_node := gw.current_node(p_conn_id);  -- the fence, first (R8-1)
    IF p_outbox_id IS NULL THEN
        RAISE EXCEPTION 'malformed record_received call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;  -- no lock of its own
    SELECT o.state INTO v_state FROM comm.outbox o
        WHERE o.id = p_outbox_id AND o.person_id = v_person FOR UPDATE;
    IF v_state IS NULL THEN
        PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'RECEIVE', NULL, NULL,
                             'REJECTED', 'unknown_item', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN 'unknown_item';
    END IF;
    INSERT INTO comm.delivery AS d (outbox_id, node_id, received_at)
        VALUES (p_outbox_id, v_node, now())
        ON CONFLICT (outbox_id, node_id) DO UPDATE
        SET received_at = coalesce(d.received_at, now());
    v_new := CASE WHEN v_state IN ('PENDING', 'DELIVERING') THEN 'DELIVERED' ELSE v_state END;
    IF v_new <> v_state THEN
        UPDATE comm.outbox o SET state = v_new, state_changed_at = now() WHERE o.id = p_outbox_id;
    END IF;
    PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'RECEIVE', v_state, v_new,
                         'APPLIED',
                         CASE WHEN v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED')
                              THEN 'late' ELSE 'received' END,
                         NULL, jsonb_build_object('node_id', v_node));
    RETURN v_new;
END;
$$;

-- Gateway: the person saw or acted on the item on this node. Some node acknowledging ends
-- delivery (ACKNOWLEDGED, terminal); NOTIFY jarvis_outbox so the gateway clears it on the other
-- nodes. Returns the state afterwards, or 'unknown_item'. A late ack changes nothing else.
CREATE FUNCTION comm.acknowledge(p_conn_id bigint, p_outbox_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_node uuid;
    v_person uuid;
    v_state text;
    v_new text;
BEGIN
    v_node := gw.current_node(p_conn_id);  -- the fence, first (R8-1)
    IF p_outbox_id IS NULL THEN
        RAISE EXCEPTION 'malformed acknowledge call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;  -- no lock of its own
    SELECT o.state INTO v_state FROM comm.outbox o
        WHERE o.id = p_outbox_id AND o.person_id = v_person FOR UPDATE;
    IF v_state IS NULL THEN
        PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'ACK', NULL, NULL,
                             'REJECTED', 'unknown_item', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN 'unknown_item';
    END IF;
    INSERT INTO comm.delivery AS d (outbox_id, node_id, received_at)
        VALUES (p_outbox_id, v_node, now())
        ON CONFLICT (outbox_id, node_id) DO UPDATE
        SET received_at = coalesce(d.received_at, now());
    v_new := CASE WHEN v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED') THEN v_state
                  ELSE 'ACKNOWLEDGED' END;
    IF v_new <> v_state THEN
        UPDATE comm.outbox o SET state = v_new, state_changed_at = now() WHERE o.id = p_outbox_id;
        PERFORM pg_notify('jarvis_outbox', p_outbox_id::text);
    END IF;
    PERFORM audit.append('AGENT', 'gateway', 'outbox', p_outbox_id, 'ACK', v_state, v_new,
                         'APPLIED',
                         CASE WHEN v_new = v_state THEN 'late' ELSE 'acknowledged' END,
                         NULL, jsonb_build_object('node_id', v_node));
    RETURN v_new;
END;
$$;

-- Gateway: the node has every chunk of the reply up to p_seq. The cursor only moves forward.
-- A chunk of another person's session is refused, (false, 'other_person'), and no cursor row
-- is created or changed (R9-1). A chunk that doesn't exist is a malformed call here.
CREATE FUNCTION conv.record_chunk_received(p_conn_id bigint, p_reply_id uuid, p_seq integer)
RETURNS TABLE (recorded boolean, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_node uuid;
    v_person uuid;
    v_owner uuid;
BEGIN
    v_node := gw.current_node(p_conn_id);  -- the fence, first (R8-1)
    IF p_reply_id IS NULL OR p_seq IS NULL OR p_seq < 1 THEN
        RAISE EXCEPTION 'malformed record_chunk_received call'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT s.person_id INTO v_owner FROM conv.stream_chunk k
        JOIN conv.session s ON s.id = k.session_id
        WHERE k.reply_id = p_reply_id AND k.seq = p_seq;
    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'malformed record_chunk_received call: no such chunk'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;  -- no lock of its own
    IF v_owner <> v_person THEN
        PERFORM audit.append('AGENT', 'gateway', 'stream', p_reply_id, 'CHUNK_RECEIVE', NULL,
                             NULL, 'REJECTED', 'other_person', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN QUERY SELECT false, 'other_person'::text;
        RETURN;
    END IF;
    INSERT INTO conv.stream_cursor AS c (reply_id, node_id, acked_seq)
        VALUES (p_reply_id, v_node, p_seq)
        ON CONFLICT (reply_id, node_id) DO UPDATE
        SET acked_seq = greatest(c.acked_seq, EXCLUDED.acked_seq);
    RETURN QUERY SELECT true, NULL::text;
END;
$$;

-- Core: end an item's delivery. Same row lock as the gateway's transitions; NOTIFY
-- jarvis_outbox so the gateway sends outbox.cleared. Returns 'applied', or 'terminal' when the
-- item was already terminal: left as it is, with a REJECTED audit row (R9-3).
CREATE FUNCTION comm.supersede(p_outbox_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_state text;
BEGIN
    SELECT o.state INTO v_state FROM comm.outbox o WHERE o.id = p_outbox_id FOR UPDATE;
    IF v_state IS NULL THEN
        RAISE EXCEPTION 'malformed supersede call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED') THEN
        PERFORM audit.append('AGENT', 'core', 'outbox', p_outbox_id, 'SUPERSEDE', v_state, NULL,
                             'REJECTED', 'terminal', NULL, '{}'::jsonb);
        RETURN 'terminal';
    END IF;
    UPDATE comm.outbox o SET state = 'SUPERSEDED', state_changed_at = now()
        WHERE o.id = p_outbox_id;
    PERFORM audit.append('AGENT', 'core', 'outbox', p_outbox_id, 'SUPERSEDE', v_state,
                         'SUPERSEDED', 'APPLIED', 'superseded', NULL, '{}'::jsonb);
    PERFORM pg_notify('jarvis_outbox', p_outbox_id::text);
    RETURN 'applied';
END;
$$;

-- Core, for comm.create_outbox only (R11-4): the target's state is checked here, under this
-- function's own FOR UPDATE lock, never by an application-side read. An item that is already
-- terminal is a non-audited no-op, 'skipped' (R11-2): nothing is written, so nothing can be
-- lost if the caller's transaction rolls back. A live item is superseded by comm.supersede
-- itself, in the same transaction ('applied', with its audit row and NOTIFY). Its terminal
-- branch can't run from here: this transaction holds the row lock, so the state can't change
-- in between.
CREATE FUNCTION comm.supersede_for_create(p_outbox_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_state text;
BEGIN
    SELECT o.state INTO v_state FROM comm.outbox o WHERE o.id = p_outbox_id FOR UPDATE;
    IF v_state IS NULL THEN
        RAISE EXCEPTION 'malformed supersede_for_create call'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED') THEN
        RETURN 'skipped';
    END IF;
    RETURN comm.supersede(p_outbox_id);
END;
$$;

CREATE FUNCTION comm.expire(p_outbox_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_state text;
BEGIN
    SELECT o.state INTO v_state FROM comm.outbox o WHERE o.id = p_outbox_id FOR UPDATE;
    IF v_state IS NULL THEN
        RAISE EXCEPTION 'malformed expire call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF v_state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED') THEN
        PERFORM audit.append('AGENT', 'core', 'outbox', p_outbox_id, 'EXPIRE', v_state, NULL,
                             'REJECTED', 'terminal', NULL, '{}'::jsonb);
        RETURN 'terminal';
    END IF;
    UPDATE comm.outbox o SET state = 'EXPIRED', state_changed_at = now()
        WHERE o.id = p_outbox_id;
    PERFORM audit.append('AGENT', 'core', 'outbox', p_outbox_id, 'EXPIRE', v_state, 'EXPIRED',
                         'APPLIED', 'expired', NULL, '{}'::jsonb);
    PERFORM pg_notify('jarvis_outbox', p_outbox_id::text);
    RETURN 'applied';
END;
$$;

REVOKE ALL ON FUNCTION conv.record_inbound(bigint, uuid, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.record_sent(bigint, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.record_received(bigint, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.acknowledge(bigint, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION conv.record_chunk_received(bigint, uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.supersede(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.supersede_for_create(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION comm.expire(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION conv.record_inbound(bigint, uuid, text, jsonb) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION comm.record_sent(bigint, uuid) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION comm.record_received(bigint, uuid) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION comm.acknowledge(bigint, uuid) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION conv.record_chunk_received(bigint, uuid, integer) TO jarvis_gateway;
GRANT EXECUTE ON FUNCTION comm.supersede(uuid) TO jarvis_app;
GRANT EXECUTE ON FUNCTION comm.supersede_for_create(uuid) TO jarvis_app;
GRANT EXECUTE ON FUNCTION comm.expire(uuid) TO jarvis_app;
"""

NEW_ACTIONS = ("INBOUND", "SEND", "RECEIVE", "ACK", "SUPERSEDE", "EXPIRE", "CHUNK_RECEIVE")
ACTIONS_0008 = (
    "CREATE",
    "TRANSITION",
    "UPDATE",
    "CONFIRM",
    "ARCHIVE",
    "ENROL_CREATE",
    "ENROL",
    "REVOKE",
    "CONNECT_REFUSED",
    "CONNECT",
    "DISCONNECT",
)
TABLE_NAMES = (
    "conv.session",
    "conv.inbound",
    "conv.message",
    "conv.stream_chunk",
    "conv.stream_cursor",
    "conv.session_ticket",
    "comm.event",
    "comm.outbox",
    "comm.delivery",
)


def upgrade() -> None:
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit.action ADD VALUE IF NOT EXISTS '{action}'")
    op.execute("""
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment', 'memory', 'node', 'inbound', 'outbox',
                               'stream'));
    """)
    op.execute(TABLES)
    op.execute(FUNCTIONS)


def downgrade() -> None:
    actions = ", ".join(f"'{a}'" for a in NEW_ACTIONS)
    any_rows = " OR ".join(f"EXISTS (SELECT 1 FROM {t})" for t in TABLE_NAMES)
    op.execute(f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM audit.event
                   WHERE entity_type IN ('inbound', 'outbox', 'stream')
                      OR action::text IN ({actions}))
           OR {any_rows} THEN
            RAISE EXCEPTION 'conversation, event or outbox rows exist; 0009 cannot be undone '
                            'without destroying them or rewriting the append-only audit trail '
                            '(ADR-008)';
        END IF;
    END $$;
    """)
    op.execute(f"""
    DROP FUNCTION comm.expire(uuid);
    DROP FUNCTION comm.supersede_for_create(uuid);
    DROP FUNCTION comm.supersede(uuid);
    DROP FUNCTION conv.record_chunk_received(bigint, uuid, integer);
    DROP FUNCTION comm.acknowledge(bigint, uuid);
    DROP FUNCTION comm.record_received(bigint, uuid);
    DROP FUNCTION comm.record_sent(bigint, uuid);
    DROP FUNCTION conv.record_inbound(bigint, uuid, text, jsonb);
    DROP TABLE comm.delivery;
    DROP TABLE comm.outbox;
    DROP FUNCTION comm.outbox_guard();
    DROP TABLE comm.event;
    DROP FUNCTION comm.refuse_change();
    DROP TABLE conv.session_ticket;
    DROP TABLE conv.stream_cursor;
    DROP TABLE conv.stream_chunk;
    DROP TABLE conv.message;
    DROP TABLE conv.inbound;
    DROP TABLE conv.session;
    DROP SCHEMA comm;
    DROP SCHEMA conv;

    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ('task', 'commitment', 'memory', 'node'));
    -- Remove the added enum values by rebuilding the type (no rows use them).
    ALTER TYPE audit.action RENAME TO action_0009;
    CREATE TYPE audit.action AS ENUM ({", ".join(f"'{a}'" for a in ACTIONS_0008)});
    ALTER TABLE audit.event ALTER COLUMN action TYPE audit.action USING action::text::audit.action;
    DROP TYPE audit.action_0009;
    """)
