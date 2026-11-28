"""The drawing store: Drawing panels, their pictures and their recipes (CAD board slice 1, Task 3).

Plan `.10x/plans/2026-11-28-cad-board-slice-1.md`.

- `board.panel.kind` gains `drawing`.
- `board.picture` is one picture of one person, named by its SHA-256: the engine's SVG, checked
  by core (`cad_engine.check_svg`) before it is stored. The hash must be the body's own, and the
  body 1 to 2,000,000 bytes. The same picture stored twice is one row (the primary key).
- `board.drawing` is the recipe that drew one revision of a Drawing panel: the steps replayed,
  the engine and its version, and the picture's hash. It refers to the panel revision.
- jarvis_app may SELECT and INSERT both, nothing else: a stored picture or recipe never changes
  and is never deleted. jarvis_gateway has no access to the schema.

Downgrade refuses once a picture, a drawing or a Drawing panel exists (nothing is destroyed);
otherwise it drops both tables and restores 0016's kind check.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE board.panel DROP CONSTRAINT panel_kind_check;
    ALTER TABLE board.panel ADD CONSTRAINT panel_kind_check
        CHECK (kind IN ('text', 'canvas', 'drawing'));

    CREATE TABLE board.picture (
        person_id   uuid NOT NULL REFERENCES ledger.person(id),
        sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        media_type  text NOT NULL CHECK (media_type = 'image/svg+xml'),
        body        bytea NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 2000000),
        created_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (person_id, sha256),
        CHECK (sha256 = encode(sha256(body), 'hex'))
    );

    CREATE TABLE board.drawing (
        panel_id        uuid NOT NULL,
        revision        integer NOT NULL CHECK (revision >= 1),
        engine          text NOT NULL CHECK (engine ~ '^[a-z0-9-]{1,30}$'),
        engine_version  text NOT NULL CHECK (engine_version ~ '^[0-9A-Za-z.+-]{1,30}$'),
        recipe          jsonb NOT NULL CHECK (jsonb_typeof(recipe) = 'array'),
        picture_sha256  text NOT NULL CHECK (picture_sha256 ~ '^[0-9a-f]{64}$'),
        created_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (panel_id, revision),
        FOREIGN KEY (panel_id, revision) REFERENCES board.panel_revision (panel_id, revision)
    );

    REVOKE ALL ON board.picture, board.drawing FROM PUBLIC;
    GRANT SELECT, INSERT ON board.picture, board.drawing TO jarvis_app;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM board.picture)
           OR EXISTS (SELECT 1 FROM board.drawing)
           OR EXISTS (SELECT 1 FROM board.panel WHERE kind = 'drawing') THEN
            RAISE EXCEPTION 'Drawings or their pictures exist; 0018 cannot be undone without '
                            'destroying them';
        END IF;
    END $$;
    DROP TABLE board.drawing;
    DROP TABLE board.picture;
    ALTER TABLE board.panel DROP CONSTRAINT panel_kind_check;
    ALTER TABLE board.panel ADD CONSTRAINT panel_kind_check CHECK (kind IN ('text', 'canvas'));
    """)
