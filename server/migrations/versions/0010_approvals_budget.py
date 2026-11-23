"""Approvals with single, audited consumption, and the brain budget tables (spec §5.5, §5.6,
§9.4; R7-1, R10-1, R10-2).

- ledger.approval (§5.5). jarvis_app may read and insert approvals and update only the decision
  columns (decision, decided_at, decided_by_node_id). The execution key and consumption time
  are set only by ledger.consume_approval.
- Two layers:
  - the refusal path: ledger.consume_approval(approval_id) wraps §9.4's UPDATE. It returns
    (execution_key, NULL) with a CONSUME audit row, or (NULL, reason) with a REJECTED audit row
    in the same transaction, where reason is unknown_approval, not_approved (PENDING, DENIED or
    EXPIRED), expired (APPROVED, but now() >= expires_at) or already_consumed. It never raises
    for a refusal (R7-1, R10-1). A malformed call raises invalid_parameter_value.
  - the backstop: the trigger ledger.approval_guard raises check_violation on any write that
    breaks the guards, and writes no audit row (R10-2). A new approval is PENDING, undecided and
    unconsumed; the request columns never change; the decision changes only from PENDING, and
    to APPROVED or DENIED only while now() < expires_at; consumed_at and execution_key are set
    exactly once, from NULL, on an APPROVED in-date row, and only while consume_approval's
    transaction-local flag names that row.
- The consumption flag: consume_approval sets the custom setting jarvis.consume_approval to the
  approval's ID with set_config(..., is_local => true) for its own UPDATE alone and clears it
  straight after. Any session can set a custom setting, so the flag is not an access control:
  jarvis_app holds no UPDATE privilege on the two columns at all, and only owner-level code can
  reach the trigger with them. The flag stops such code (a later migration or function) from
  setting them by mistake outside consume_approval, and it authorises one row only, in the
  exact consumption shape.
- ops.brain_budget, ops.brain_reservation and ops.brain_usage (§5.6): tables only, amounts in
  paise. At most one live (ACTIVE, SETTLED or LAPSED) reservation per turn. The reserve, settle
  and lapse logic (§10.1) comes in a later task.
- audit.event: entity type 'approval'; actions CONSUME, DECIDE and RECEIPT. Rows carry codes and IDs
  only.
- Approval audit (R12-21): an AFTER INSERT trigger writes the CREATE row; an AFTER UPDATE OF
  decision trigger writes DECIDE APPLIED (APPROVED, DENIED, EXPIRED); and
  ledger.refuse_decision(approval_id, node_id, code) writes the DECIDE REJECTED row of a decision
  core refused under §9.3 step 6 and changes nothing else. Its codes are fixed, in check order:
  unknown_approval, bad_card_sig, other_person, node_revoked, not_pending, expired. Any other
  code is a malformed call. EXECUTE for jarvis_app only.
- Receipt audit (R12-24): ledger.refuse_receipt(ref, node_id, code) writes the RECEIPT REJECTED
  row of a receipt core rejected (§9.4), or of a verification listing that never came, and
  changes nothing else. Fixed codes (listed with the function); EXECUTE for jarvis_app only.
- All of it is for the core (jarvis_app); the gateway has no grant on any 0010 object, and
  PUBLIC has no EXECUTE on either function, the trigger function included.
- An APPROVED or DENIED approval records both decided_at and decided_by_node_id (CHECK
  approval_decided_by_node_and_time); EXPIRED is not a node decision and needs neither.

Downgrade refuses once any approval, budget, reservation or usage row, or any approval audit
row, exists.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APPROVAL = r"""
CREATE TABLE ledger.approval (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                   uuid NOT NULL REFERENCES ledger.task(id),
    requested_from_person_id  uuid NOT NULL REFERENCES ledger.person(id),
    requesting_node_id        uuid NOT NULL REFERENCES gw.node(id),
    tier                      text NOT NULL
                              CHECK (tier IN ('READ_ONLY', 'REVERSIBLE', 'IRREVERSIBLE')),
    action_kind               text NOT NULL CHECK (action_kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    action_args               jsonb NOT NULL CHECK (jsonb_typeof(action_args) = 'object'),
    summary                   text NOT NULL CHECK (length(summary) BETWEEN 1 AND 500),
    card_authz                jsonb NOT NULL CHECK (jsonb_typeof(card_authz) = 'object'
                                                    AND card_authz ?& ARRAY['object', 'key_id', 'sig']),
    expires_at                timestamptz NOT NULL,
    decision                  text NOT NULL DEFAULT 'PENDING'
                              CHECK (decision IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED')),
    decided_at                timestamptz,
    decided_by_node_id        uuid REFERENCES gw.node(id),
    consumed_at               timestamptz,
    execution_key             uuid CONSTRAINT approval_execution_key_unique UNIQUE,
    CHECK (decision <> 'PENDING' OR (decided_at IS NULL AND decided_by_node_id IS NULL)),
    CHECK (decided_by_node_id IS NULL OR decision IN ('APPROVED', 'DENIED')),
    -- A node's decision records which node decided and when; EXPIRED is not a node decision.
    CONSTRAINT approval_decided_by_node_and_time
        CHECK (decision NOT IN ('APPROVED', 'DENIED')
               OR (decided_at IS NOT NULL AND decided_by_node_id IS NOT NULL)),
    CHECK ((consumed_at IS NULL) = (execution_key IS NULL)),
    CHECK (execution_key IS NULL OR decision = 'APPROVED')
);
CREATE INDEX ON ledger.approval (task_id);
CREATE INDEX approval_pending_expiry ON ledger.approval (expires_at) WHERE decision = 'PENDING';

-- The backstop (R10-2): correct code never reaches these raises, so they write no audit row.
CREATE FUNCTION ledger.approval_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.decision IS DISTINCT FROM 'PENDING' OR NEW.decided_at IS NOT NULL
           OR NEW.decided_by_node_id IS NOT NULL OR NEW.consumed_at IS NOT NULL
           OR NEW.execution_key IS NOT NULL THEN
            RAISE EXCEPTION 'a new approval is PENDING, undecided and unconsumed'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.task_id, NEW.requested_from_person_id, NEW.requesting_node_id, NEW.tier,
        NEW.action_kind, NEW.action_args, NEW.summary, NEW.card_authz, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.task_id, OLD.requested_from_person_id, OLD.requesting_node_id, OLD.tier,
        OLD.action_kind, OLD.action_args, OLD.summary, OLD.card_authz, OLD.expires_at) THEN
        RAISE EXCEPTION 'an approval''s request never changes' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decision IS DISTINCT FROM OLD.decision THEN
        IF OLD.decision <> 'PENDING' THEN
            RAISE EXCEPTION 'approval decision % cannot change', OLD.decision
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.decision IN ('APPROVED', 'DENIED') AND now() >= NEW.expires_at THEN
            RAISE EXCEPTION 'approval expired before it was decided (P2-E11)'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF (NEW.decided_at, NEW.decided_by_node_id)
          IS DISTINCT FROM (OLD.decided_at, OLD.decided_by_node_id) THEN
        RAISE EXCEPTION 'decided_at and decided_by_node_id change only with the decision'
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.consumed_at, NEW.execution_key) IS DISTINCT FROM (OLD.consumed_at, OLD.execution_key)
       AND NOT (
           coalesce(current_setting('jarvis.consume_approval', true), '') = NEW.id::text
           AND OLD.consumed_at IS NULL AND OLD.execution_key IS NULL
           AND NEW.consumed_at IS NOT NULL AND NEW.execution_key IS NOT NULL
           AND OLD.decision = 'APPROVED' AND NEW.decision = 'APPROVED'
           AND now() < NEW.expires_at
       ) THEN
        RAISE EXCEPTION 'an approval is consumed only once, through ledger.consume_approval'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER approval_guard BEFORE INSERT OR UPDATE ON ledger.approval
    FOR EACH ROW EXECUTE FUNCTION ledger.approval_guard();
REVOKE ALL ON FUNCTION ledger.approval_guard() FROM PUBLIC;  -- a trigger function, never called

REVOKE ALL ON ledger.approval FROM PUBLIC;
GRANT SELECT, INSERT ON ledger.approval TO jarvis_app;
GRANT UPDATE (decision, decided_at, decided_by_node_id) ON ledger.approval TO jarvis_app;

-- §9.4: issued once, atomically. The UPDATE's row lock serialises concurrent calls; a waiter
-- re-reads the committed row, sees consumed_at set and matches nothing. On zero rows the row
-- is locked and classified; if it turns out consumable after all (it was approved after the
-- UPDATE's snapshot), the UPDATE runs once more, now holding the lock.
CREATE FUNCTION ledger.consume_approval(p_approval_id uuid)
RETURNS TABLE (execution_key uuid, refusal text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_key uuid;
    v_task uuid;
    v_found boolean;
    v_decision text;
    v_consumed timestamptz;
    v_expires timestamptz;
    v_refusal text;
BEGIN
    IF p_approval_id IS NULL THEN
        RAISE EXCEPTION 'malformed consume_approval call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR attempt IN 1..2 LOOP
        PERFORM pg_catalog.set_config('jarvis.consume_approval', p_approval_id::text, true);
        UPDATE ledger.approval a
           SET consumed_at = now(), execution_key = pg_catalog.gen_random_uuid()
         WHERE a.id = p_approval_id AND a.decision = 'APPROVED' AND a.consumed_at IS NULL
           AND a.expires_at > now()
        RETURNING a.execution_key, a.task_id INTO v_key, v_task;
        PERFORM pg_catalog.set_config('jarvis.consume_approval', '', true);
        IF v_key IS NOT NULL THEN
            PERFORM audit.append('AGENT', 'core', 'approval', p_approval_id, 'CONSUME', NULL, NULL,
                                 'APPLIED', 'consumed', NULL,
                                 jsonb_build_object('task_id', v_task));
            RETURN QUERY SELECT v_key, NULL::text;
            RETURN;
        END IF;
        v_found := NULL;
        SELECT true, a.decision, a.consumed_at, a.expires_at
          INTO v_found, v_decision, v_consumed, v_expires
          FROM ledger.approval a WHERE a.id = p_approval_id FOR UPDATE;
        IF v_found IS NULL THEN
            v_refusal := 'unknown_approval';
        ELSIF v_consumed IS NOT NULL THEN
            v_refusal := 'already_consumed';
        ELSIF v_decision <> 'APPROVED' THEN
            v_refusal := 'not_approved';
        ELSIF now() >= v_expires THEN
            v_refusal := 'expired';
        END IF;
        EXIT WHEN v_refusal IS NOT NULL;
    END LOOP;
    IF v_refusal IS NULL THEN  -- unreachable: the second UPDATE held the row lock
        RAISE EXCEPTION 'consume_approval found a consumable approval it could not consume';
    END IF;
    PERFORM audit.append('AGENT', 'core', 'approval', p_approval_id, 'CONSUME', NULL, NULL,
                         'REJECTED', v_refusal, NULL,
                         jsonb_strip_nulls(jsonb_build_object('decision', v_decision)));
    RETURN QUERY SELECT NULL::uuid, v_refusal;
END;
$$;

REVOKE ALL ON FUNCTION ledger.consume_approval(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger.consume_approval(uuid) TO jarvis_app;
"""

# R12-21: the approval's own audit rows. Creation and every decision change are audited by
# triggers in the same transaction as the write; a refused decision (§9.3 step 6) is recorded by
# ledger.refuse_decision, which writes the REJECTED row and changes nothing else.
APPROVAL_AUDIT = r"""
-- CREATE, APPLIED, to_state PENDING. The actor comes from jarvis.actor_kind / jarvis.actor_id
-- (ledger._declare) when the caller set them; approvals are created only by core, so the
-- default is AGENT core.
CREATE FUNCTION ledger.approval_audit_create() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_kind text := coalesce(nullif(current_setting('jarvis.actor_kind', true), ''), 'AGENT');
    v_actor text := coalesce(nullif(current_setting('jarvis.actor_id', true), ''), 'core');
BEGIN
    PERFORM audit.append(v_kind, v_actor, 'approval', NEW.id, 'CREATE', NULL, 'PENDING',
                         'APPLIED', 'created', NULL,
                         jsonb_build_object('task_id', NEW.task_id,
                                            'requesting_node_id', NEW.requesting_node_id));
    RETURN NULL;
END;
$$;
CREATE TRIGGER approval_audit_create AFTER INSERT ON ledger.approval
    FOR EACH ROW EXECUTE FUNCTION ledger.approval_audit_create();
REVOKE ALL ON FUNCTION ledger.approval_audit_create() FROM PUBLIC;  -- a trigger function

-- DECIDE, APPLIED, from the old decision to the new one (APPROVED, DENIED or EXPIRED). It runs
-- after approval_guard accepted the change, so it records only legal decisions.
CREATE FUNCTION ledger.approval_audit_decide() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM audit.append('AGENT', 'core', 'approval', NEW.id, 'DECIDE', OLD.decision,
                         NEW.decision, 'APPLIED', lower(NEW.decision), NULL,
                         jsonb_strip_nulls(jsonb_build_object(
                             'task_id', NEW.task_id,
                             'decided_by_node_id', NEW.decided_by_node_id)));
    RETURN NULL;
END;
$$;
CREATE TRIGGER approval_audit_decide AFTER UPDATE OF decision ON ledger.approval
    FOR EACH ROW WHEN (OLD.decision IS DISTINCT FROM NEW.decision)
    EXECUTE FUNCTION ledger.approval_audit_decide();
REVOKE ALL ON FUNCTION ledger.approval_audit_decide() FROM PUBLIC;  -- a trigger function

-- §9.3 step 6: a decision refused by core's checks (made under the approval's row lock). Writes
-- one DECIDE REJECTED row with the failing check as its reason and changes nothing else. The
-- codes are fixed, in check order; any other code, or a NULL argument, is a malformed call.
CREATE FUNCTION ledger.refuse_decision(p_approval_id uuid, p_node_id uuid, p_code text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_decision text;
BEGIN
    IF p_approval_id IS NULL OR p_node_id IS NULL OR p_code IS NULL
       OR p_code NOT IN ('unknown_approval', 'bad_card_sig', 'other_person', 'node_revoked',
                         'not_pending', 'expired') THEN
        RAISE EXCEPTION 'malformed refuse_decision call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT a.decision INTO v_decision FROM ledger.approval a WHERE a.id = p_approval_id;
    PERFORM audit.append('AGENT', 'core', 'approval', p_approval_id, 'DECIDE', v_decision, NULL,
                         'REJECTED', p_code, NULL,
                         jsonb_strip_nulls(jsonb_build_object('node_id', p_node_id,
                                                              'decision', v_decision)));
    RETURN p_code;
END;
$$;
REVOKE ALL ON FUNCTION ledger.refuse_decision(uuid, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger.refuse_decision(uuid, uuid, text) TO jarvis_app;

-- R12-24, §9.4: a receipt core rejected. Writes one RECEIPT REJECTED row with the code as its
-- reason and changes nothing else. The codes are fixed:
--   about one receipt (p_ref is its conv.inbound ID; entity 'inbound'):
--     unknown_key      no issued action has this execution key, or not for this node
--     reused_key       the key's one receipt was already accepted
--     unknown_action   a live receipt for no ACTION chunk of this node and person
--     late             received after the action's signed valid_until
--     malformed        evidence not in the shape of its contract (R12-25)
--     not_recycled     a delete receipt whose result is not RECYCLED
--     file_mismatch    the receipt's file ID or SHA-256 differs from the approved file
--     listing_failed   the verification listing's result is not OK, or it is malformed
--     still_listed     the verification listing still has the approved file ID
--   about an issued action whose verification listing never came (p_ref is the approval ID;
--   entity 'approval'):
--     listing_missing
-- Any other code, or a NULL argument, is a malformed call.
CREATE FUNCTION ledger.refuse_receipt(p_ref uuid, p_node_id uuid, p_code text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_ref IS NULL OR p_node_id IS NULL OR p_code IS NULL
       OR p_code NOT IN ('unknown_key', 'reused_key', 'unknown_action', 'late', 'malformed',
                         'not_recycled', 'file_mismatch', 'listing_failed', 'still_listed',
                         'listing_missing') THEN
        RAISE EXCEPTION 'malformed refuse_receipt call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM audit.append('AGENT', 'core',
                         CASE WHEN p_code = 'listing_missing' THEN 'approval' ELSE 'inbound' END,
                         p_ref, 'RECEIPT', NULL, NULL, 'REJECTED', p_code, NULL,
                         jsonb_build_object('node_id', p_node_id));
    RETURN p_code;
END;
$$;
REVOKE ALL ON FUNCTION ledger.refuse_receipt(uuid, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger.refuse_receipt(uuid, uuid, text) TO jarvis_app;
"""

BUDGET = r"""
-- §5.6. Amounts are integers in paise. "This month" and "today" are Asia/Kolkata dates.
CREATE TABLE ops.brain_budget (
    month         date PRIMARY KEY CHECK (month = date_trunc('month', month)::date),
    cap_inr       bigint NOT NULL CHECK (cap_inr >= 0),
    settled_inr   bigint NOT NULL DEFAULT 0 CHECK (settled_inr >= 0),
    reserved_inr  bigint NOT NULL DEFAULT 0 CHECK (reserved_inr >= 0)
);

-- turn_id is the conv.inbound id (null for probes). It has no foreign key: conv.inbound is
-- pruned after 30 days, reservations are kept 13 months (§5.7).
CREATE TABLE ops.brain_reservation (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        bigint NOT NULL REFERENCES ops.job(id),
    turn_id       uuid,
    month         date NOT NULL REFERENCES ops.brain_budget(month),
    day           date NOT NULL,
    reserved_inr  bigint NOT NULL CHECK (reserved_inr >= 0),
    state         text NOT NULL DEFAULT 'ACTIVE'
                  CHECK (state IN ('ACTIVE', 'SETTLED', 'RELEASED', 'LAPSED')),
    actual_inr    bigint CHECK (actual_inr >= 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    lease_until   timestamptz NOT NULL,
    CHECK (date_trunc('month', day)::date = month),
    CHECK (state <> 'ACTIVE' OR actual_inr IS NULL),
    CHECK (state <> 'SETTLED' OR actual_inr IS NOT NULL)
);
-- At most one live reservation per turn; a RELEASED one frees the turn for a retry.
CREATE UNIQUE INDEX brain_reservation_one_live_per_turn ON ops.brain_reservation (turn_id)
    WHERE state IN ('ACTIVE', 'SETTLED', 'LAPSED');
CREATE INDEX brain_reservation_active_lease ON ops.brain_reservation (lease_until)
    WHERE state = 'ACTIVE';
CREATE INDEX ON ops.brain_reservation (created_at);  -- 13-month retention (§5.7)

CREATE TABLE ops.brain_usage (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_id  uuid NOT NULL REFERENCES ops.brain_reservation(id),
    model           text NOT NULL CHECK (length(model) BETWEEN 1 AND 100),
    input_tokens    integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens   integer NOT NULL CHECK (output_tokens >= 0),
    cost_inr        bigint NOT NULL CHECK (cost_inr >= 0),
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ops.brain_usage (reservation_id);
CREATE INDEX ON ops.brain_usage (at);  -- 13-month retention (§5.7)

REVOKE ALL ON ops.brain_budget, ops.brain_reservation, ops.brain_usage FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON ops.brain_budget TO jarvis_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.brain_reservation TO jarvis_app;
GRANT SELECT, INSERT, DELETE ON ops.brain_usage TO jarvis_app;
"""

NEW_ACTIONS = ("CONSUME", "DECIDE", "RECEIPT")
ACTIONS_0009 = (
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
)
ENTITY_TYPES_0009 = ("task", "commitment", "memory", "node", "inbound", "outbox", "stream")
TABLE_NAMES = ("ledger.approval", "ops.brain_budget", "ops.brain_reservation", "ops.brain_usage")


def _entity_check(types: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{t}'" for t in types)
    return f"""
    ALTER TABLE audit.event DROP CONSTRAINT event_entity_type_check;
    ALTER TABLE audit.event ADD CONSTRAINT event_entity_type_check
        CHECK (entity_type IN ({listed}));
    """


def upgrade() -> None:
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit.action ADD VALUE IF NOT EXISTS '{action}'")
    op.execute(_entity_check((*ENTITY_TYPES_0009, "approval")))
    op.execute(APPROVAL)
    op.execute(APPROVAL_AUDIT)
    op.execute(BUDGET)


def downgrade() -> None:
    actions = ", ".join(f"'{a}'" for a in NEW_ACTIONS)
    any_rows = " OR ".join(f"EXISTS (SELECT 1 FROM {t})" for t in TABLE_NAMES)
    op.execute(f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM audit.event
                   WHERE entity_type = 'approval' OR action::text IN ({actions}))
           OR {any_rows} THEN
            RAISE EXCEPTION 'approval or budget rows exist; 0010 cannot be undone without '
                            'destroying them or rewriting the append-only audit trail (ADR-008)';
        END IF;
    END $$;
    """)
    op.execute("""
    DROP TABLE ops.brain_usage;
    DROP TABLE ops.brain_reservation;
    DROP TABLE ops.brain_budget;
    DROP FUNCTION ledger.refuse_receipt(uuid, uuid, text);
    DROP FUNCTION ledger.refuse_decision(uuid, uuid, text);
    DROP FUNCTION ledger.consume_approval(uuid);
    DROP TABLE ledger.approval;
    DROP FUNCTION ledger.approval_audit_decide();
    DROP FUNCTION ledger.approval_audit_create();
    DROP FUNCTION ledger.approval_guard();
    """)
    op.execute(_entity_check(ENTITY_TYPES_0009))
    op.execute(f"""
    -- Remove the added enum values by rebuilding the type (no rows use them).
    ALTER TYPE audit.action RENAME TO action_0010;
    CREATE TYPE audit.action AS ENUM ({", ".join(f"'{a}'" for a in ACTIONS_0009)});
    ALTER TABLE audit.event ALTER COLUMN action TYPE audit.action USING action::text::audit.action;
    DROP TYPE audit.action_0010;
    """)
