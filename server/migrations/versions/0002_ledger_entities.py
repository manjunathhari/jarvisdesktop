"""The 13 v1 ledger entities (Architecture v2 §10.1), kept minimal.

Every table: id, created_at, updated_at, visibility, source_ref. State columns are filled and
changed only by the database (revision 0004). The app role may read, insert and update other
columns; it may not delete.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

COMMON = """
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    visibility  ledger.visibility NOT NULL DEFAULT 'PRIVATE',
    source_ref  text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 200)"""

# (table, specific columns) in dependency order.
TABLES: list[tuple[str, str]] = [
    (
        "organization",
        """
    name  text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    kind  text NOT NULL DEFAULT 'household'""",
    ),
    (
        "person",
        """
    display_name     text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    organization_id  uuid REFERENCES ledger.organization(id)""",
    ),
    (
        "relationship",
        """
    from_person_id  uuid NOT NULL REFERENCES ledger.person(id),
    to_person_id    uuid NOT NULL REFERENCES ledger.person(id),
    kind            text NOT NULL,
    CHECK (from_person_id <> to_person_id)""",
    ),
    (
        "information",
        """
    subject_person_id  uuid REFERENCES ledger.person(id),
    kind               text NOT NULL,
    content            jsonb NOT NULL DEFAULT '{}'""",
    ),
    (
        "event",
        """
    title      text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    starts_at  timestamptz NOT NULL,
    ends_at    timestamptz,
    location   text,
    CHECK (ends_at IS NULL OR ends_at >= starts_at)""",
    ),
    (
        "commitment",
        """
    person_id    uuid NOT NULL REFERENCES ledger.person(id),
    description  text NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    due_at       timestamptz,
    event_id     uuid REFERENCES ledger.event(id),
    state        ledger.commitment_state NOT NULL DEFAULT 'EXPECTED'""",
    ),
    (
        "responsibility",
        """
    owner_person_id  uuid NOT NULL REFERENCES ledger.person(id),
    description      text NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    recurrence       text""",
    ),
    (
        "task",
        """
    title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    definition_of_done  text NOT NULL CHECK (length(definition_of_done) BETWEEN 1 AND 2000),
    owner_person_id     uuid REFERENCES ledger.person(id),
    responsibility_id   uuid REFERENCES ledger.responsibility(id),
    due_at              timestamptz,
    state               ledger.task_state NOT NULL DEFAULT 'DETECTED'""",
    ),
    (
        "dependency",
        """
    from_task_id  uuid NOT NULL REFERENCES ledger.task(id),
    to_task_id    uuid NOT NULL REFERENCES ledger.task(id),
    kind          text NOT NULL CHECK (kind IN ('BLOCKS', 'RELATES')),
    CHECK (from_task_id <> to_task_id),
    UNIQUE (from_task_id, to_task_id, kind)""",
    ),
    (
        "action",
        """
    task_id          uuid NOT NULL REFERENCES ledger.task(id),
    actor_kind       audit.actor_kind NOT NULL,
    actor_id         text NOT NULL,
    idempotency_key  text NOT NULL UNIQUE,
    attempted_at     timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL DEFAULT 'STARTED'""",
    ),
    (
        "outcome",
        """
    action_id    uuid NOT NULL REFERENCES ledger.action(id),
    verified     boolean NOT NULL DEFAULT false,
    evidence     jsonb NOT NULL DEFAULT '{}',
    verified_at  timestamptz""",
    ),
    (
        "permission",
        """
    person_id             uuid NOT NULL REFERENCES ledger.person(id),
    scope                 text NOT NULL,
    tier                  text NOT NULL CHECK (tier IN ('READ_ONLY', 'REVERSIBLE', 'IRREVERSIBLE')),
    granted_by_person_id  uuid REFERENCES ledger.person(id),
    expires_at            timestamptz""",
    ),
    (
        "context",
        """
    subject_person_id  uuid REFERENCES ledger.person(id),
    kind               text NOT NULL,
    summary            text NOT NULL CHECK (length(summary) BETWEEN 1 AND 2000),
    valid_from         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,
    CHECK (expires_at > valid_from)""",
    ),
]

PROTECTED_COLUMNS = ("id", "state", "created_at", "updated_at")


def upgrade() -> None:
    for name, columns in TABLES:
        op.execute(f"""
        CREATE TABLE ledger.{name} ({COMMON},{columns}
        );
        CREATE TRIGGER stamp_times BEFORE INSERT OR UPDATE ON ledger.{name}
            FOR EACH ROW EXECUTE FUNCTION ledger.stamp_times();
        GRANT SELECT, INSERT ON ledger.{name} TO jarvis_app;
        """)
    protected = ", ".join(f"'{c}'" for c in PROTECTED_COLUMNS)
    op.execute(f"""
    DO $$
    DECLARE t record;
    BEGIN
        FOR t IN
            SELECT table_name, string_agg(quote_ident(column_name), ', ') AS cols
            FROM information_schema.columns
            WHERE table_schema = 'ledger' AND column_name NOT IN ({protected})
            GROUP BY table_name
        LOOP
            EXECUTE format('GRANT UPDATE (%s) ON ledger.%I TO jarvis_app', t.cols, t.table_name);
        END LOOP;
    END $$;

    CREATE INDEX ON ledger.task (state);
    CREATE INDEX ON ledger.commitment (state);
    CREATE INDEX ON ledger.context (expires_at);

    -- Context is time-bound: only rows valid right now (P0-5).
    CREATE VIEW ledger.current_context AS
        SELECT * FROM ledger.context WHERE valid_from <= now() AND now() < expires_at;
    GRANT SELECT ON ledger.current_context TO jarvis_app;
    """)


def downgrade() -> None:
    op.execute("DROP VIEW ledger.current_context;")
    for name, _ in reversed(TABLES):
        op.execute(f"DROP TABLE ledger.{name};")
