"""Which utterance a reply answers (spec §6.2; R12-37, owner, 25 Nov).

`conv.stream_chunk.in_reply_to`: the `conv.inbound` id (the utterance envelope's
`message_id`) of the turn this chunk's reply answers; NULL for replies that answer no
utterance (core's own `speak()` results, listings issued by the delete flow). Every chunk of
a reply carries the same value. No foreign key: `conv.inbound` is pruned after 30 days. The
gateway already has SELECT on the table (0009), so no grant changes.

Downgrade drops the column (the value is only a routing hint, never needed for safety).

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conv.stream_chunk ADD COLUMN in_reply_to uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE conv.stream_chunk DROP COLUMN in_reply_to")
