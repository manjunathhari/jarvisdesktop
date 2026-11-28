"""JARVIS's self-improvement team: tickets, stage runs, its own job queue and Manj's inbox.

Design "JARVIS self-improvement team: design", approved by the owner on 27 Nov 2026.

- Schema `improve`. `improve.ticket` is one complaint and where it stands; `improve.ticket_event`
  records every move with who made it and why; `improve.stage_run` records each stage's cost.
- `improve.allowed_move` holds the design's states table, and a trigger refuses any other change
  of `state`, so code can't skip one of Manj's two gates. Leaving PAUSED goes back only to the
  state it was paused from. DESIGN_READY -> APPROVED needs approved_fingerprint to equal the
  fingerprint of the design as it stands.
- `improve.job` is the team's own queue. Core's worker claims every row of ops.job with no filter
  by kind, so the team can't share it. A paid stage runs at most once (max_attempts 1); a PR
  watch, which costs nothing, may run three times.
- `improve.inbox` is what the team needs from Manj. A check refuses text that looks like a key.
- jarvis_app may SELECT, INSERT and UPDATE the team's tables (the orchestrator uses its role for
  now) and nothing may DELETE. jarvis_gateway has no access to the schema.

Downgrade refuses once a ticket exists (nothing is destroyed); otherwise it drops the schema.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_STATES = (
    "'OPEN', 'DESIGNING', 'DESIGN_READY', 'APPROVED', 'PR_OPEN', 'WATCHING', 'DONE', 'PAUSED',"
    " 'STOPPED'"
)
_FLOW = [
    ("OPEN", "DESIGNING"),
    ("DESIGNING", "DESIGN_READY"),
    ("DESIGN_READY", "APPROVED"),
    ("DESIGN_READY", "DESIGNING"),
    ("APPROVED", "PR_OPEN"),
    ("PR_OPEN", "WATCHING"),
    ("PR_OPEN", "APPROVED"),
    ("WATCHING", "DONE"),
    ("WATCHING", "DESIGNING"),
]
_LIVE = ["OPEN", "DESIGNING", "DESIGN_READY", "APPROVED", "PR_OPEN", "WATCHING"]
_MOVES = list(dict.fromkeys(_FLOW + [(s, to) for s in _LIVE for to in ("PAUSED", "STOPPED")]))

# The same patterns as improve/inbox.py: a key's value never lands in the inbox.
_LOOKS_LIKE_A_KEY = (
    r"sk-ant-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|[A-Za-z0-9+_=-]{40,}"
)


def upgrade() -> None:
    values = ", ".join(f"('{a}', '{b}')" for a, b in _MOVES)
    op.execute(f"""
    CREATE SCHEMA improve;
    REVOKE ALL ON SCHEMA improve FROM PUBLIC;
    GRANT USAGE ON SCHEMA improve TO jarvis_app;

    CREATE TABLE improve.allowed_move (
        from_state text NOT NULL,
        to_state text NOT NULL,
        PRIMARY KEY (from_state, to_state)
    );
    INSERT INTO improve.allowed_move VALUES {values};

    CREATE TABLE improve.ticket (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        words text NOT NULL CHECK (length(words) BETWEEN 1 AND 2000),
        slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{{0,39}}$'),
        evidence jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(evidence) = 'object'),
        state text NOT NULL DEFAULT 'OPEN' CHECK (state IN ({_STATES})),
        paused_from text CHECK (paused_from IN ({_STATES})),
        paused_stage text,
        rounds_used int NOT NULL DEFAULT 0 CHECK (rounds_used BETWEEN 0 AND 3),
        cap_usd numeric(8, 2) NOT NULL DEFAULT 8 CHECK (cap_usd > 0),
        design text,
        design_fingerprint text CHECK (design_fingerprint ~ '^[0-9a-f]{{64}}$'),
        approved_fingerprint text CHECK (approved_fingerprint ~ '^[0-9a-f]{{64}}$'),
        design_ready_at timestamptz,
        base_sha text CHECK (base_sha ~ '^[0-9a-f]{{40}}$'),
        test_fingerprints jsonb NOT NULL DEFAULT '{{}}',
        tests_patch bytea,
        build_patch bytea,
        feedback text[] NOT NULL DEFAULT '{{}}',
        pr_number int,
        pr_url text,
        reviewed_sha text CHECK (reviewed_sha ~ '^[0-9a-f]{{40}}$'),
        handled_comments bigint[] NOT NULL DEFAULT '{{}}',
        told_ready boolean NOT NULL DEFAULT false,
        watch_started_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE improve.ticket_event (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticket_id bigint NOT NULL REFERENCES improve.ticket (id),
        from_state text NOT NULL,
        to_state text NOT NULL,
        actor text NOT NULL,
        reason text NOT NULL,
        at timestamptz NOT NULL DEFAULT now()
    );

    CREATE FUNCTION improve.guard_move() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
        IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
            RETURN NEW;
        END IF;
        IF OLD.state = 'PAUSED' THEN
            IF NEW.state <> 'STOPPED' AND NEW.state IS DISTINCT FROM OLD.paused_from THEN
                RAISE EXCEPTION 'ticket %: PAUSED goes back only to %', OLD.id, OLD.paused_from;
            END IF;
            NEW.paused_from := NULL;
        ELSIF NOT EXISTS (SELECT 1 FROM improve.allowed_move m
                          WHERE m.from_state = OLD.state AND m.to_state = NEW.state) THEN
            RAISE EXCEPTION 'ticket %: % -> % is not an allowed move', OLD.id, OLD.state, NEW.state;
        ELSIF NEW.state = 'PAUSED' THEN
            NEW.paused_from := OLD.state;
        END IF;
        IF OLD.state = 'DESIGN_READY' AND NEW.state = 'APPROVED'
           AND (NEW.approved_fingerprint IS NULL
                OR NEW.approved_fingerprint IS DISTINCT FROM NEW.design_fingerprint) THEN
            RAISE EXCEPTION 'ticket %: the approval is not for this design', OLD.id;
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END $$;
    CREATE TRIGGER ticket_guard_move BEFORE UPDATE ON improve.ticket
        FOR EACH ROW EXECUTE FUNCTION improve.guard_move();

    CREATE TABLE improve.stage_run (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticket_id bigint NOT NULL REFERENCES improve.ticket (id),
        stage text NOT NULL CHECK (stage IN ('design', 'tests', 'build', 'review')),
        outcome text NOT NULL CHECK (outcome IN ('PASSED', 'FAILED', 'REFUSED')),
        cost_usd numeric(10, 4) NOT NULL CHECK (cost_usd >= 0),
        turns int NOT NULL CHECK (turns >= 0),
        error text CHECK (length(error) <= 4000),
        at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX stage_run_ticket ON improve.stage_run (ticket_id);

    CREATE TABLE improve.job (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticket_id bigint NOT NULL REFERENCES improve.ticket (id),
        stage text NOT NULL CHECK (stage IN ('design', 'tests', 'build', 'review', 'pr_watch')),
        status text NOT NULL DEFAULT 'QUEUED'
            CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
        run_at timestamptz NOT NULL DEFAULT now(),
        attempts int NOT NULL DEFAULT 0,
        max_attempts int NOT NULL,
        locked_by text,
        locked_until timestamptz,
        last_error text,
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        CHECK (stage = 'pr_watch' OR max_attempts = 1)
    );
    CREATE INDEX job_ready ON improve.job (run_at, id) WHERE status IN ('QUEUED', 'RUNNING');

    CREATE TABLE improve.inbox (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticket_id bigint REFERENCES improve.ticket (id),
        kind text NOT NULL CHECK (kind IN ('KEY', 'DECISION', 'BLOCKED_SITE', 'READY_TO_MERGE',
                                           'PAUSED', 'STOPPED', 'WARNING')),
        title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
        steps text[] NOT NULL,
        key_name text CHECK (key_name ~ '^[a-z0-9_]{{1,64}}$'),
        created_at timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz,
        CHECK (title !~ '{_LOOKS_LIKE_A_KEY}' AND array_to_string(steps, ' ') !~ '{_LOOKS_LIKE_A_KEY}')
    );

    REVOKE ALL ON ALL TABLES IN SCHEMA improve FROM PUBLIC;
    GRANT SELECT ON improve.allowed_move TO jarvis_app;
    GRANT SELECT, INSERT, UPDATE ON improve.ticket, improve.stage_run, improve.job,
        improve.inbox TO jarvis_app;
    GRANT SELECT, INSERT ON improve.ticket_event TO jarvis_app;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM improve.ticket) THEN
            RAISE EXCEPTION 'improvement tickets exist; 0017 cannot be undone without '
                            'destroying them';
        END IF;
    END $$;
    DROP SCHEMA improve CASCADE;
    """)
