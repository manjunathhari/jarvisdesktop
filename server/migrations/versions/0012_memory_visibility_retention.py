"""Memory visibility and retention (spec §5.7, §12; R12-30, R12-31; plan Task 13).

- audit.action gains SET_VISIBILITY and RETENTION; audit.event's entity types gain 'retention'.
- audit.record_memory() accepts SET_VISIBILITY: an applied change keeps the tag (from = to) on
  an entry that isn't EXPIRED, and needs a commit, like every applied memory change.
- ops.retention_sweep(p_now) deletes past-retention rows (§5.7, R12-31) and writes one SYSTEM
  RETENTION audit row per table, with the real count, in the same transaction (R12-30). It is
  SECURITY DEFINER, owned by jarvis_owner, with a fixed search_path, and only jarvis_app may
  EXECUTE it. No table grant is added: the rows are deleted as the function's owner.
  `p_now` later than now(), or NULL, is a malformed call, so an injected clock can only narrow
  the windows. It never deletes an open connection, an ACTIVE reservation, or a row a kept row
  still needs:

  | Table | Deleted when (cutoff = p_now minus the window) |
  |---|---|
  | conv.stream_chunk, with the reply's conv.stream_cursor rows | the reply's final chunk, or its latest when it has none, is 24 h old |
  | conv.inbound | received 30 days ago, and neither an ACTIVE reservation (turn_id) nor a QUEUED or RUNNING job (payload inbound_id) names it |
  | comm.delivery | its item is terminal, and its last send or receipt is 90 days old; a row never sent counts from the item's state_changed_at |
  | gw.node_connection | closed 90 days ago |
  | ops.brain_usage | 13 months old |
  | ops.brain_reservation | created 13 months ago, not ACTIVE, and no usage row is left for it |

  Each audit row: entity 'retention' with a fixed ID per table (md5 of 'retention:<table>'),
  reason 'retention-sweep', details {table, deleted (a decimal string), cutoff}.

Downgrade refuses once a SET_VISIBILITY or retention audit row exists (ADR-008).

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

NEW_ACTIONS = ("SET_VISIBILITY", "RETENTION")
ACTIONS_0011 = (
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
    "INBOUND",
    "SEND",
    "RECEIVE",
    "ACK",
    "SUPERSEDE",
    "EXPIRE",
    "CHUNK_RECEIVE",
    "CONSUME",
    "DECIDE",
    "RECEIPT",
)
ENTITY_TYPES_0011 = (
    "task",
    "commitment",
    "memory",
    "node",
    "inbound",
    "outbox",
    "stream",
    "approval",
)


def _entity_check(types: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{t}'" for t in types)
    return f"""
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ({listed}));
    """


# audit.record_memory as migration 0007 created it; @ACTIONS@ and @SET_VISIBILITY@ are the only
# differences between the 0007 and the 0012 versions.
_RECORD_MEMORY = r"""
CREATE OR REPLACE FUNCTION audit.record_memory(
    p_actor_kind text, p_actor_id text, p_ref text, p_action text,
    p_from text, p_to text, p_outcome text, p_reason text, p_commit text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_ref !~ '^(m-[0-9]{4,}|m-unassigned|person:[a-z][a-z0-9-]{1,30})$' THEN
        RAISE EXCEPTION 'bad memory reference' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_action NOT IN (@ACTIONS@) THEN
        RAISE EXCEPTION 'bad memory action' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF coalesce(p_from, 'STATED') NOT IN ('STATED','CONFIRMED','INFERRED','HISTORICAL','EXPIRED')
       OR coalesce(p_to, 'STATED') NOT IN ('STATED','CONFIRMED','INFERRED','HISTORICAL','EXPIRED') THEN
        RAISE EXCEPTION 'bad memory tag' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_reason !~ '^[a-z][a-z0-9-]{0,63}$' THEN
        RAISE EXCEPTION 'memory audit reason must be a short code'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT (
        (p_actor_kind = 'PERSON' AND (p_actor_id = 'unknown' OR p_actor_id ~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
        OR (p_actor_kind = 'AGENT' AND p_actor_id ~ '^[a-z][a-z0-9-]{0,39}$')
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
        OR (p_action = 'ARCHIVE' AND p_from <> 'EXPIRED' AND p_to = 'EXPIRED')@SET_VISIBILITY@
    ) THEN
        RAISE EXCEPTION 'illegal memory tag change' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_outcome = 'REJECTED' AND (p_from IS NOT NULL OR p_to IS NOT NULL) THEN
        RAISE EXCEPTION 'refusals carry no tags' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_commit IS NOT NULL AND p_commit !~ '^[0-9a-f]{40}$' THEN
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
"""


def _record_memory(*, set_visibility: bool) -> str:
    actions = ["CREATE", "UPDATE", "CONFIRM", "ARCHIVE"]
    if set_visibility:
        actions.append("SET_VISIBILITY")
    rule = (
        "\n        OR (p_action = 'SET_VISIBILITY' AND p_from IS NOT NULL AND p_to = p_from"
        " AND p_from <> 'EXPIRED')"
        if set_visibility
        else ""
    )
    return _RECORD_MEMORY.replace("@ACTIONS@", ", ".join(f"'{a}'" for a in actions)).replace(
        "@SET_VISIBILITY@", rule
    )


RETENTION = r"""
CREATE FUNCTION ops.retention_sweep(p_now timestamptz)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_day      timestamptz;
    v_month    timestamptz;
    v_quarter  timestamptz;
    v_year     timestamptz;
    v_replies  uuid[];
    v_n        bigint;
    v_tables   text[] := '{}';
    v_counts   bigint[] := '{}';
    v_cutoffs  timestamptz[] := '{}';
BEGIN
    IF p_now IS NULL OR p_now > now() THEN
        RAISE EXCEPTION 'retention_sweep needs a time no later than now()'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_day := p_now - interval '24 hours';
    v_month := p_now - interval '30 days';
    v_quarter := p_now - interval '90 days';
    v_year := p_now - interval '13 months';

    -- conv.stream_chunk: whole replies, 24 h after the final chunk, or after the latest chunk
    -- of a reply that never got one (R12-31). Their cursor rows go with them.
    SELECT coalesce(array_agg(r.reply_id), '{}') INTO v_replies FROM (
        SELECT c.reply_id FROM conv.stream_chunk c GROUP BY c.reply_id
        HAVING coalesce(max(c.created_at) FILTER (WHERE c.final), max(c.created_at)) <= v_day
    ) r;
    DELETE FROM conv.stream_chunk c WHERE c.reply_id = ANY(v_replies);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'conv.stream_chunk'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_day;
    DELETE FROM conv.stream_cursor s WHERE s.reply_id = ANY(v_replies);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'conv.stream_cursor'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_day;

    -- conv.inbound: 30 days, unless a live reservation or an unfinished job still names it.
    DELETE FROM conv.inbound i
    WHERE i.received_at <= v_month
      AND NOT EXISTS (SELECT 1 FROM ops.brain_reservation r
                      WHERE r.turn_id = i.id AND r.state = 'ACTIVE')
      AND NOT EXISTS (SELECT 1 FROM ops.job j
                      WHERE j.status IN ('QUEUED', 'RUNNING')
                        AND j.payload->>'inbound_id' = i.id::text);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'conv.inbound'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_month;

    -- comm.delivery: only for a terminal item, whose delivery state is no longer live; 90 days
    -- after the last send or receipt, or, never sent, after the item ended (R12-31).
    DELETE FROM comm.delivery d USING comm.outbox o
    WHERE o.id = d.outbox_id
      AND o.state IN ('ACKNOWLEDGED', 'SUPERSEDED', 'EXPIRED')
      AND CASE WHEN d.last_sent_at IS NULL THEN o.state_changed_at
               ELSE greatest(d.last_sent_at, d.received_at) END <= v_quarter;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'comm.delivery'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_quarter;

    -- gw.node_connection: closed ones only; CONNECT and DISCONNECT stay in audit (R8-2).
    DELETE FROM gw.node_connection c
    WHERE c.closed_at IS NOT NULL AND c.closed_at <= v_quarter;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'gw.node_connection'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_quarter;

    -- ops.brain_usage, then the reservations no usage row still needs (never an ACTIVE one).
    DELETE FROM ops.brain_usage u WHERE u.at <= v_year;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'ops.brain_usage'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_year;
    DELETE FROM ops.brain_reservation r
    WHERE r.created_at <= v_year AND r.state <> 'ACTIVE'
      AND NOT EXISTS (SELECT 1 FROM ops.brain_usage u WHERE u.reservation_id = r.id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'ops.brain_reservation'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_year;

    -- One audit row per table, with the real count, in this transaction (R12-30).
    FOR i IN 1 .. array_length(v_tables, 1) LOOP
        PERFORM audit.append('SYSTEM', 'system', 'retention',
                             md5('retention:' || v_tables[i])::uuid, 'RETENTION', NULL, NULL,
                             'APPLIED', 'retention-sweep', NULL,
                             jsonb_build_object('table', v_tables[i],
                                                'deleted', v_counts[i]::text,
                                                'cutoff', v_cutoffs[i]::text));
        table_name := v_tables[i];
        deleted := v_counts[i];
        RETURN NEXT;
    END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION ops.retention_sweep(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.retention_sweep(timestamptz) TO jarvis_app;
"""


def upgrade() -> None:
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit.action ADD VALUE IF NOT EXISTS '{action}'")
    op.execute(_entity_check((*ENTITY_TYPES_0011, "retention")))
    op.execute(_record_memory(set_visibility=True))
    op.execute(RETENTION)


def downgrade() -> None:
    actions = ", ".join(f"'{a}'" for a in NEW_ACTIONS)
    op.execute(f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM audit.event
                   WHERE entity_type = 'retention' OR action::text IN ({actions})) THEN
            RAISE EXCEPTION 'visibility or retention audit rows exist; 0012 cannot be undone '
                            'without rewriting the append-only audit trail (ADR-008)';
        END IF;
    END $$;
    DROP FUNCTION ops.retention_sweep(timestamptz);
    """)
    op.execute(_record_memory(set_visibility=False))
    op.execute(_entity_check(ENTITY_TYPES_0011))
    op.execute(f"""
    -- Remove the added enum values by rebuilding the type (no rows use them).
    ALTER TYPE audit.action RENAME TO action_0012;
    CREATE TYPE audit.action AS ENUM ({", ".join(f"'{a}'" for a in ACTIONS_0011)});
    ALTER TABLE audit.event ALTER COLUMN action TYPE audit.action USING action::text::audit.action;
    DROP TYPE audit.action_0012;
    """)
