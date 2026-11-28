"""The drawing store (CAD board slice 1, Task 3; migration 0018).

- `board.picture` keeps a person's pictures, named by their SHA-256. A picture is checked
  (`cad_engine.check_svg`) before it is stored; storing the same picture twice is one row.
  Pictures are per person: one person can never read another's.
- `board.drawing` keeps, for one revision of a Drawing panel, the recipe that drew it (the steps
  replayed), the engine that replayed it and the picture it gave. `save` requires that the panel
  is this person's and a Drawing panel, that its content names this picture, and that the
  picture is this person's.

Nothing is changed or deleted once stored. Recipes and pictures are untrusted data wherever they
are read back (they carry Claude's and the engine's output).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from jarvis_server import contracts as c

from .board import UnknownPanel
from .cad_engine import RECIPE_MAX, Replay, Step, check_svg

MEDIA_TYPE = "image/svg+xml"

__all__ = ["check_svg", "picture", "put_picture", "recipe", "save"]


def put_picture(conn: psycopg.Connection[Any], person_id: UUID, svg: bytes) -> str:
    """Check and store this person's picture; its SHA-256 (lower-case hex). `BadPicture` for a
    picture that fails the check (nothing is stored)."""
    check_svg(svg)
    sha256 = hashlib.sha256(svg).hexdigest()
    conn.execute(
        "INSERT INTO board.picture (person_id, sha256, media_type, body) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (person_id, sha256) DO NOTHING",
        (person_id, sha256, MEDIA_TYPE, svg),
    )
    return sha256


def picture(conn: psycopg.Connection[Any], person_id: UUID, sha256: str) -> bytes:
    """This person's picture with this hash; `LookupError` if they have none."""
    row = conn.execute(
        "SELECT body FROM board.picture WHERE person_id = %s AND sha256 = %s",
        (person_id, sha256),
    ).fetchone()
    if row is None:
        raise LookupError("no such picture of this person")
    return bytes(row[0])


def save(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    panel_id: UUID,
    revision: int,
    replay: Replay,
    steps: Sequence[Step],
    picture_sha256: str,
) -> None:
    """Record the recipe that drew this revision of this person's Drawing panel.

    `UnknownPanel` (a `LookupError`) for a panel or revision that is not this person's;
    `ValueError` for a panel that is not a Drawing, content that does not name this picture, or a
    recipe over `RECIPE_MAX` commands; `LookupError` for a picture this person does not have. A
    revision is saved once (a second save is a unique violation)."""
    recipe_json = [s.wire() for s in steps]
    if sum(len(s.commands) for s in steps) > RECIPE_MAX:
        raise ValueError("a recipe has at most 2,000 commands")
    with conn.transaction():
        row = conn.execute(
            "SELECT p.kind, r.content FROM board.panel p"
            " JOIN board.panel_revision r ON r.panel_id = p.id AND r.revision = %s"
            " WHERE p.id = %s AND p.person_id = %s",
            (revision, panel_id, person_id),
        ).fetchone()
        if row is None:
            raise UnknownPanel(str(panel_id))
        kind, content = row
        if kind != c.PanelKind.DRAWING:
            raise ValueError("the panel is not a drawing")
        if c.parse_drawing_content(content).picture.sha256 != picture_sha256:
            raise ValueError("the panel's content does not name this picture")
        held = conn.execute(
            "SELECT 1 FROM board.picture WHERE person_id = %s AND sha256 = %s",
            (person_id, picture_sha256),
        ).fetchone()
        if held is None:
            raise LookupError("no such picture of this person")
        conn.execute(
            "INSERT INTO board.drawing"
            " (panel_id, revision, engine, engine_version, recipe, picture_sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (panel_id, revision, replay.engine, replay.engine_version, Jsonb(recipe_json),
             picture_sha256),
        )  # fmt: skip


def recipe(conn: psycopg.Connection[Any], person_id: UUID, panel_id: UUID) -> tuple[Step, ...]:
    """The recipe of this person's Drawing panel at its newest saved revision; `LookupError` if
    there is none (or the panel is not theirs)."""
    row = conn.execute(
        "SELECT d.recipe FROM board.drawing d JOIN board.panel p ON p.id = d.panel_id"
        " WHERE d.panel_id = %s AND p.person_id = %s ORDER BY d.revision DESC LIMIT 1",
        (panel_id, person_id),
    ).fetchone()
    if row is None:
        raise LookupError("no drawing recipe for this panel")
    return tuple(
        Step(label=s["label"], commands=tuple(dict(cmd) for cmd in s["commands"])) for s in row[0]
    )
