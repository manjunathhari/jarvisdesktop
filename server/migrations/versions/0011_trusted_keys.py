"""Trusted keys and node key state (spec §7.1, §7.2; R5-1, R6-6; plan Task 12).

- gw.trusted_key: the CORE and GATEWAY public keys (never a private key). A key is NEXT
  (announced, not yet used), ACTIVE (at most one per role, a partial unique index) or RETIRED.
  A RETIRED key with no `retired_at` is retired-pending: core still verifies with it during the
  24 h overlap, until `jarvis-core keys retire`. `key_id` is `fingerprint(public_key)`, checked
  by core; `announced_outbox_ids` are the `keys.keyset` items announcing a NEXT key.
- gw.node.key_state: CURRENT (default) or STALE (missed a rotation; R5-1). The gateway refuses a
  STALE node with 4409 after its challenge (R6-6).
- Grants: jarvis_app may SELECT, INSERT and UPDATE gw.trusted_key (no DELETE) and UPDATE only
  gw.node.key_state. jarvis_gateway may only SELECT gw.trusted_key and gw.node.key_state. The
  gateway's function surface is unchanged (six gw functions, R12-2).

Downgrade refuses once a trusted key exists or a node is STALE: undoing it would silently
forget which keys nodes trust.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE gw.trusted_key (
        key_id                text PRIMARY KEY CHECK (key_id ~ '^[a-z2-7]{16}$'),
        role                  text NOT NULL CHECK (role IN ('CORE', 'GATEWAY')),
        public_key            bytea NOT NULL UNIQUE CHECK (length(public_key) = 32),
        state                 text NOT NULL CHECK (state IN ('NEXT', 'ACTIVE', 'RETIRED')),
        announced_outbox_ids  uuid[] NOT NULL DEFAULT '{}',
        created_at            timestamptz NOT NULL DEFAULT now(),
        activated_at          timestamptz,
        retired_at            timestamptz,
        CHECK (retired_at IS NULL OR state = 'RETIRED')
    );
    -- One ACTIVE key per role.
    CREATE UNIQUE INDEX trusted_key_one_active ON gw.trusted_key (role) WHERE state = 'ACTIVE';

    REVOKE ALL ON gw.trusted_key FROM PUBLIC;
    GRANT SELECT, INSERT, UPDATE ON gw.trusted_key TO jarvis_app;
    GRANT SELECT ON gw.trusted_key TO jarvis_gateway;

    ALTER TABLE gw.node ADD COLUMN key_state text NOT NULL DEFAULT 'CURRENT'
        CHECK (key_state IN ('CURRENT', 'STALE'));
    GRANT UPDATE (key_state) ON gw.node TO jarvis_app;
    GRANT SELECT (key_state) ON gw.node TO jarvis_gateway;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM gw.trusted_key)
           OR EXISTS (SELECT 1 FROM gw.node WHERE key_state = 'STALE') THEN
            RAISE EXCEPTION 'trusted keys or STALE nodes exist; 0011 cannot be undone without '
                            'forgetting which keys nodes trust';
        END IF;
    END $$;
    """)
    op.execute("""
    REVOKE SELECT (key_state) ON gw.node FROM jarvis_gateway;
    REVOKE UPDATE (key_state) ON gw.node FROM jarvis_app;
    ALTER TABLE gw.node DROP COLUMN key_state;
    DROP TABLE gw.trusted_key;
    """)
