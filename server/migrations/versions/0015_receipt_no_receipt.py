"""Receipt audit code `no_receipt` (server task, owner-approved 26 Nov).

An approved action whose receipt never arrives by its `valid_until` plus a grace is failed by
core's `action.receipt_timeout` job. `ledger.refuse_receipt` gains the fixed code `no_receipt`
for it: like `listing_missing`, it is about an issued action, not an inbound message, so
`p_ref` is the approval ID and the audit entity is 'approval'. Nothing else changes: same
signature, definer, search_path and grants (CREATE OR REPLACE keeps them).

Downgrade restores the 0010 body. Audit rows already written with the code stay (the trail is
append-only).

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_BODY = """
CREATE OR REPLACE FUNCTION ledger.refuse_receipt(p_ref uuid, p_node_id uuid, p_code text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_ref IS NULL OR p_node_id IS NULL OR p_code IS NULL
       OR p_code NOT IN ('bad_node_signature', 'unknown_key', 'reused_key', 'unknown_action',
                         'late', 'malformed', 'not_recycled', 'file_mismatch',
                         'listing_failed', 'still_listed', {about_approval}) THEN
        RAISE EXCEPTION 'malformed refuse_receipt call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM audit.append('AGENT', 'core',
                         CASE WHEN p_code IN ({about_approval}) THEN 'approval'
                              ELSE 'inbound' END,
                         p_ref, 'RECEIPT', NULL, NULL, 'REJECTED', p_code, NULL,
                         jsonb_build_object('node_id', p_node_id));
    RETURN p_code;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_BODY.format(about_approval="'listing_missing', 'no_receipt'"))


def downgrade() -> None:
    op.execute(_BODY.format(about_approval="'listing_missing'"))
