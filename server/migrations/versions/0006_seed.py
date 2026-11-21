"""Seed data (D6 "me only"): the household and one Person.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

HOUSEHOLD_ID = "00000000-0000-4000-8000-000000000001"
MANJ_ID = "00000000-0000-4000-8000-000000000002"


def upgrade() -> None:
    op.execute(f"""
    INSERT INTO ledger.organization (id, name, kind, source_ref)
        VALUES ('{HOUSEHOLD_ID}', 'Household', 'household', 'seed') ON CONFLICT (id) DO NOTHING;
    INSERT INTO ledger.person (id, display_name, organization_id, source_ref)
        VALUES ('{MANJ_ID}', 'Manj', '{HOUSEHOLD_ID}', 'seed') ON CONFLICT (id) DO NOTHING;
    """)


def downgrade() -> None:
    # Seed rows stay if other records already point at them; dropping the tables (0002) removes
    # them on a full downgrade.
    op.execute(f"""
    DO $$
    BEGIN
        DELETE FROM ledger.person WHERE id = '{MANJ_ID}';
        DELETE FROM ledger.organization WHERE id = '{HOUSEHOLD_ID}';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'seed rows are referenced; left in place';
    END $$;
    """)
