"""The Board's panel store, and `board.state` as an inbound kind (Board slice 1, Task 2).

Plan `.10x/plans/2026-11-26-board-slice-1.md`, approved by the owner on 26 Nov 2026.

- Schema `board`. `board.panel` is one panel of a person: its kind, title, state, current
  revision, the revision the desktop last reported drawn, and what it has cost so far.
  `number` is the panel's stable number for that person; Jarvis refers to a panel as `p<number>`.
  `board.panel_revision` keeps every revision's title and content.
- jarvis_app may SELECT, INSERT and UPDATE board.panel, and SELECT and INSERT
  board.panel_revision: a panel is never deleted and a stored revision never changes (closing a
  panel is a state). jarvis_gateway has no access to the schema.
- Limits as in `contracts/messages/board.show.args`: a title of 1 to 120 characters, content of
  1 to 30,000 bytes, kinds `text` and `canvas`, the four states. Core checks the rest (control
  characters, the hash) before it stores anything.
- `conv.inbound.kind` gains `board.state` (the person changed a panel's state by hand), and
  `conv.record_inbound` queues it as job `board.state`. The function is replaced with the same
  signature, so its owner and grants stay; nothing else in it changes.

Downgrade refuses once a panel or a `board.state` inbound row exists (nothing is destroyed);
otherwise it restores 0009's function and check and drops the schema.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_KINDS = "'utterance', 'approval.decision', 'action.receipt'"

# 0009's function, word for word, with one more kind when `board` is filled in.
_RECORD_INBOUND = """
CREATE OR REPLACE FUNCTION conv.record_inbound(
    p_conn_id bigint, p_message_id uuid, p_kind text, p_payload jsonb, p_envelope jsonb
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
       OR jsonb_typeof(p_payload) <> 'object'
       OR (p_envelope IS NOT NULL AND jsonb_typeof(p_envelope) <> 'object') THEN
        RAISE EXCEPTION 'malformed record_inbound call' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_job_kind := CASE p_kind
        WHEN 'utterance' THEN 'conv.turn'
        WHEN 'approval.decision' THEN 'approval.decision'
        WHEN 'action.receipt' THEN 'action.receipt'
{board}    END;
    IF v_job_kind IS NULL THEN
        -- Never the kind itself in the audit row: it is untrusted text from the node.
        PERFORM audit.append('AGENT', 'gateway', 'inbound', p_message_id, 'INBOUND', NULL, NULL,
                             'REJECTED', 'unknown_kind', NULL,
                             jsonb_build_object('node_id', v_node));
        RETURN QUERY SELECT false, 'unknown_kind'::text;
        RETURN;
    END IF;
    SELECT n.person_id INTO v_person FROM gw.node n WHERE n.id = v_node;
    INSERT INTO conv.inbound (id, node_id, person_id, kind, payload, envelope)
        VALUES (p_message_id, v_node, v_person, p_kind, p_payload, p_envelope)
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
"""

_BOARD_KIND = "        WHEN 'board.state' THEN 'board.state'\n"


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA board;
    REVOKE ALL ON SCHEMA board FROM PUBLIC;
    GRANT USAGE ON SCHEMA board TO jarvis_app;

    CREATE TABLE board.panel (
        id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        person_id          uuid NOT NULL REFERENCES ledger.person(id),
        number             integer NOT NULL CHECK (number >= 1),
        session_id         uuid REFERENCES conv.session(id),
        kind               text NOT NULL CHECK (kind IN ('text', 'canvas')),
        title              text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
        state              text NOT NULL DEFAULT 'open'
                           CHECK (state IN ('open', 'minimised', 'maximised', 'closed')),
        revision           integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
        -- The newest revision the desktop reported drawn (receipt RENDERED); NULL: none yet.
        rendered_revision  integer
                           CHECK (rendered_revision IS NULL
                                  OR rendered_revision BETWEEN 1 AND revision),
        cost_paise         integer NOT NULL DEFAULT 0 CHECK (cost_paise >= 0),
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        state_changed_at   timestamptz NOT NULL DEFAULT now(),
        -- When a state was last set or reported, even one that changed nothing: reports
        -- from the desktop older than this are left out (core.board.report_state).
        state_seen_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (person_id, number)
    );

    CREATE TABLE board.panel_revision (
        panel_id        uuid NOT NULL REFERENCES board.panel(id),
        revision        integer NOT NULL CHECK (revision >= 1),
        title           text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
        content         text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 30000),
        content_sha256  text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        created_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (panel_id, revision)
    );

    REVOKE ALL ON board.panel, board.panel_revision FROM PUBLIC;
    GRANT SELECT, INSERT, UPDATE ON board.panel TO jarvis_app;
    GRANT SELECT, INSERT ON board.panel_revision TO jarvis_app;
    """)
    op.execute(f"""
    ALTER TABLE conv.inbound DROP CONSTRAINT inbound_kind_check;
    ALTER TABLE conv.inbound ADD CONSTRAINT inbound_kind_check
        CHECK (kind IN ({_KINDS}, 'board.state'));
    """)
    op.execute(_RECORD_INBOUND.format(board=_BOARD_KIND))


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM board.panel)
           OR EXISTS (SELECT 1 FROM conv.inbound WHERE kind = 'board.state') THEN
            RAISE EXCEPTION 'Board panels exist, or board.state messages; 0016 cannot be undone '
                            'without destroying them';
        END IF;
    END $$;
    """)
    op.execute(_RECORD_INBOUND.format(board=""))
    op.execute(f"""
    ALTER TABLE conv.inbound DROP CONSTRAINT inbound_kind_check;
    ALTER TABLE conv.inbound ADD CONSTRAINT inbound_kind_check CHECK (kind IN ({_KINDS}));
    DROP TABLE board.panel_revision;
    DROP TABLE board.panel;
    DROP SCHEMA board;
    """)
