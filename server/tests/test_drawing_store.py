"""The drawing store (migration 0018; plan `.10x/plans/2026-11-28-cad-board-slice-1.md`, Task 3):
pictures per person, a drawing's recipe per panel revision, and the Drawing panel kind.

Runs in its own database: drawings block the 0018 downgrade, so they must never reach the session
database that P0-6 takes down and up. People, drawings and recipes are fictional. The tests of
`board.check_content` for the new kind need no database.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jarvis_server import contracts as c
from jarvis_server import migrate
from jarvis_server.core import board, cad_engine, drawing
from jarvis_server.core.board import BadPanel, UnknownPanel
from jarvis_server.core.cad_engine import BadPicture, CommandResult, Replay, Step, step_from

from .conftest import Dsns, fresh_database
from .fakes.cad_runner import PNG, SVG
from .helpers import flow

Conn = psycopg.Connection[Any]

OTHER_SVG = SVG.replace(b"M 20 20", b"M 25 20")
STEPS = (
    step_from(
        {"label": "Outer walls", "commands": [
            {"command": "rectang", "params": {"corner1": [0, 0], "corner2": [4000, 3000]}},
        ]}
    ),
    step_from(
        {"label": "Room names", "commands": [
            {"command": "text", "params": {"insert": [2000, 1500], "text": "Living room"}},
            {"command": "text", "params": {"insert": [500, 500], "text": "Store"}},
        ]}
    ),
)  # fmt: skip


def _replay(svg: bytes = SVG) -> Replay:
    results = tuple(
        CommandResult(step=s, n=n, command=str(cmd["command"]), ok=True, text="done")
        for s, step in enumerate(STEPS, start=1)
        for n, cmd in enumerate(step.commands, start=1)
    )
    return Replay(
        results=results, failed=0, counts={"Rectang": 1, "Text": 2}, items=3,
        extents=(4000.0, 3000.0), texts=("Living room", "Store"), png=PNG, svg=svg,
        engine="cadcraft", engine_version="0.12.0",
    )  # fmt: skip


def _content(svg: bytes = SVG, steps: tuple[Step, ...] = STEPS) -> str:
    return c.drawing_content(
        c.DrawingContent(
            engine="cadcraft",
            engine_version="0.12.0",
            units="mm",
            items=3,
            steps=tuple(s.label for s in steps),
            picture=c.DrawingPicture(
                sha256=hashlib.sha256(svg).hexdigest(), bytes=len(svg), media_type="image/svg+xml"
            ),
        )
    )


# --- board.check_content for the new kind (no database) -----------------------------------------


def test_check_content_accepts_a_drawing_description() -> None:
    board.check_content("drawing", "Ground floor plan", _content())


@pytest.mark.parametrize(
    "content",
    [
        "hello",
        _content().replace(",", ", ", 1),  # not canonical
        _content().replace('"mm"', '"in"'),
        '{"engine":"cadcraft"}',
        "[" * 10_000,
    ],
)
def test_check_content_refuses_a_bad_drawing_description(content: str) -> None:
    with pytest.raises(BadPanel):
        board.check_content("drawing", "Ground floor plan", content)


def test_text_and_canvas_are_as_before() -> None:
    for kind in ("text", "canvas"):
        board.check_content(kind, "A title", "hello")
        board.check_content(kind, "A title", _content())
        with pytest.raises(BadPanel):
            board.check_content(kind, "A title", "bell \x07 inside")


# --- the store ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    return flow.new_person(owner)


@pytest.fixture
def other(owner: Conn) -> uuid.UUID:
    return flow.new_person(owner, "Ravi Fictional")


def _panel(app: Conn, person: uuid.UUID, svg: bytes = SVG, *, kind: str = "drawing") -> board.Panel:
    content = _content(svg) if kind == "drawing" else "A fictional note."
    return board.create(
        app, person_id=person, session_id=None, kind=kind, title="Ground floor plan",
        content=content, cost_paise=1_200,
    )  # fmt: skip


def _holders(app: Conn, sha256: str, *people: uuid.UUID) -> list[uuid.UUID]:
    """Which of these people have a stored picture with this hash, one entry per row (the
    database is shared by the module's tests)."""
    rows = app.execute(
        "SELECT person_id FROM board.picture WHERE sha256 = %s AND person_id = ANY(%s)"
        " ORDER BY person_id",
        (sha256, list(people)),
    ).fetchall()
    return [r[0] for r in rows]


def _drawings(app: Conn, panel_id: uuid.UUID) -> int:
    row = app.execute("SELECT count(*) FROM board.drawing WHERE panel_id = %s", (panel_id,))
    return int(row.fetchone()[0])  # type: ignore[index]


def test_picture_round_trips_and_is_per_person(app: Conn, person: uuid.UUID) -> None:
    sha = drawing.put_picture(app, person, SVG)
    assert sha == hashlib.sha256(SVG).hexdigest()
    assert drawing.picture(app, person, sha) == SVG
    assert isinstance(drawing.picture(app, person, sha), bytes)
    with pytest.raises(LookupError):
        drawing.picture(app, person, hashlib.sha256(OTHER_SVG).hexdigest())


def test_same_picture_twice_is_one_row(app: Conn, person: uuid.UUID, other: uuid.UUID) -> None:
    first = drawing.put_picture(app, person, OTHER_SVG)
    again = drawing.put_picture(app, person, OTHER_SVG)
    assert first == again
    assert _holders(app, first, person, other) == [person]
    drawing.put_picture(app, other, OTHER_SVG)  # the same bytes, another person: their own row
    assert _holders(app, first, person, other) == sorted([person, other])


def test_another_person_cannot_read_a_picture(
    app: Conn, person: uuid.UUID, other: uuid.UUID
) -> None:
    sha = drawing.put_picture(app, person, SVG)
    with pytest.raises(LookupError):
        drawing.picture(app, other, sha)
    assert drawing.picture(app, person, sha) == SVG


@pytest.mark.parametrize("bad", [b"", b"<svg/>", SVG.replace(b"<path", b"<script")])
def test_a_bad_picture_is_never_stored(app: Conn, person: uuid.UUID, bad: bytes) -> None:
    with pytest.raises(BadPicture):
        drawing.put_picture(app, person, bad)
    assert _holders(app, hashlib.sha256(bad).hexdigest(), person) == []


def test_panel_kind_drawing_is_storable(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    assert (made.kind, made.content) == ("drawing", _content())
    assert board.get(app, person, made.panel_id) == made
    assert [(p.kind, p.title) for p in board.listing(app, person)] == [
        ("drawing", "Ground floor plan")
    ]
    with pytest.raises(BadPanel):
        board.create(app, person_id=person, session_id=None, kind="drawing", title="Plan",
                     content="hello", cost_paise=0)  # fmt: skip


def test_recipe_comes_back_as_saved(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1, replay=_replay(),
                 steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert drawing.recipe(app, person, made.panel_id) == STEPS
    row = app.execute(
        "SELECT engine, engine_version, picture_sha256, recipe FROM board.drawing"
        " WHERE panel_id = %s AND revision = 1",
        (made.panel_id,),
    ).fetchone()
    assert row is not None
    assert row[:3] == ("cadcraft", "0.12.0", sha)
    assert row[3] == [{"label": s.label, "commands": list(s.commands)} for s in STEPS]


def test_another_person_cannot_read_a_recipe(
    app: Conn, person: uuid.UUID, other: uuid.UUID
) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1, replay=_replay(),
                 steps=STEPS, picture_sha256=sha)  # fmt: skip
    with pytest.raises(LookupError):
        drawing.recipe(app, other, made.panel_id)
    with pytest.raises(LookupError):
        drawing.recipe(app, person, uuid.uuid4())


def test_a_drawing_panel_without_a_recipe_has_none(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    with pytest.raises(LookupError):
        drawing.recipe(app, person, made.panel_id)


def test_save_refuses_another_persons_panel(app: Conn, person: uuid.UUID, other: uuid.UUID) -> None:
    theirs = _panel(app, other)
    drawing.put_picture(app, other, SVG)
    sha = drawing.put_picture(app, person, SVG)
    with pytest.raises(UnknownPanel):
        drawing.save(app, person_id=person, panel_id=theirs.panel_id, revision=1,
                     replay=_replay(), steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, theirs.panel_id) == 0


def test_save_refuses_a_picture_the_person_does_not_have(
    app: Conn, person: uuid.UUID, other: uuid.UUID
) -> None:
    made = _panel(app, person, OTHER_SVG)
    sha = drawing.put_picture(app, other, OTHER_SVG)  # only the other person has it
    assert _holders(app, sha, person, other) == [other]
    with pytest.raises(LookupError):
        drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1,
                     replay=_replay(OTHER_SVG), steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, made.panel_id) == 0


def test_save_refuses_a_revision_the_panel_does_not_have(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    with pytest.raises(LookupError):
        drawing.save(app, person_id=person, panel_id=made.panel_id, revision=2,
                     replay=_replay(), steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, made.panel_id) == 0


def test_save_refuses_a_panel_that_is_not_a_drawing(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person, kind="text")
    sha = drawing.put_picture(app, person, SVG)
    with pytest.raises(ValueError, match="drawing"):
        drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1,
                     replay=_replay(), steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, made.panel_id) == 0


def test_save_refuses_a_picture_the_panel_does_not_name(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person, SVG)
    sha = drawing.put_picture(app, person, OTHER_SVG)
    with pytest.raises(ValueError, match="picture"):
        drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1,
                     replay=_replay(OTHER_SVG), steps=STEPS, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, made.panel_id) == 0


def test_a_revision_is_saved_once(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    kw: dict[str, Any] = {"person_id": person, "panel_id": made.panel_id, "revision": 1,
                          "replay": _replay(), "steps": STEPS, "picture_sha256": sha}  # fmt: skip
    drawing.save(app, **kw)
    with pytest.raises(psycopg.errors.UniqueViolation):
        drawing.save(app, **kw)
    assert _drawings(app, made.panel_id) == 1


def test_save_refuses_a_recipe_over_the_limit(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    line = {"command": "line", "params": {"from": [0, 0], "to": [1, 1]}}
    many = tuple(step_from({"label": f"Lines {i}", "commands": [line] * 400}) for i in range(6))
    with pytest.raises(ValueError, match="2,000"):
        drawing.save(app, person_id=person, panel_id=made.panel_id, revision=1,
                     replay=_replay(), steps=many, picture_sha256=sha)  # fmt: skip
    assert _drawings(app, made.panel_id) == 0


def test_the_database_keeps_a_picture_true_to_its_hash(app: Conn, person: uuid.UUID) -> None:
    for sha, body, media in (
        (hashlib.sha256(b"other").hexdigest(), SVG, "image/svg+xml"),
        (hashlib.sha256(SVG).hexdigest().upper(), SVG, "image/svg+xml"),
        (hashlib.sha256(SVG).hexdigest(), SVG, "text/html"),
        (hashlib.sha256(b"").hexdigest(), b"", "image/svg+xml"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO board.picture (person_id, sha256, media_type, body)"
                " VALUES (%s, %s, %s, %s)",
                (person, sha, media, body),
            )


def test_the_database_refuses_a_malformed_drawing_row(app: Conn, person: uuid.UUID) -> None:
    made = _panel(app, person)
    sha = drawing.put_picture(app, person, SVG)
    for engine, version, recipe, picture in (
        ("CADcraft", "0.12.0", [], sha),
        ("cadcraft", "0.12 0", [], sha),
        ("cadcraft", "0.12.0", {"steps": []}, sha),
        ("cadcraft", "0.12.0", [], "not a hash"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            app.execute(
                "INSERT INTO board.drawing (panel_id, revision, engine, engine_version, recipe,"
                " picture_sha256) VALUES (%s, 1, %s, %s, %s, %s)",
                (made.panel_id, engine, version, Jsonb(recipe), picture),
            )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app.execute(
            "INSERT INTO board.drawing (panel_id, revision, engine, engine_version, recipe,"
            " picture_sha256) VALUES (%s, 7, 'cadcraft', '0.12.0', '[]', %s)",
            (made.panel_id, sha),
        )


def test_app_role_may_only_read_and_add(owner: Conn) -> None:
    for table in ("board.picture", "board.drawing"):
        granted = {
            p
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
            if owner.execute(
                "SELECT has_table_privilege('jarvis_app', %s, %s)", (table, p)
            ).fetchone()
            == (True,)
        }
        assert granted == {"SELECT", "INSERT"}, table


def test_gateway_role_cannot_reach_drawings(gateway: Conn) -> None:
    for sql in ("SELECT 1 FROM board.picture", "SELECT 1 FROM board.drawing"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(sql)


def test_0018_goes_down_and_up_and_refuses_once_a_drawing_exists() -> None:
    tables = (
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_schema = 'board' AND table_name IN ('picture', 'drawing')"
    )
    kinds = (
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'board.panel'::regclass AND conname = 'panel_kind_check'"
    )
    with fresh_database() as d:
        migrate.downgrade(d.owner, "0017")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(tables).fetchone() == (0,)
            assert "drawing" not in str(conn.execute(kinds).fetchone())
            assert "canvas" in str(conn.execute(kinds).fetchone())
            person = flow.new_person(conn)
        with (
            psycopg.connect(d.app, autocommit=True) as conn,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            conn.execute(
                "INSERT INTO board.panel (person_id, number, kind, title)"
                " VALUES (%s, 1, 'drawing', 'Plan')",
                (person,),
            )
        migrate.upgrade(d.owner, "head")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute(tables).fetchone() == (2,)
            assert "drawing" in str(conn.execute(kinds).fetchone())
        with psycopg.connect(d.app, autocommit=True) as conn:
            drawing.put_picture(conn, person, SVG)
        with pytest.raises(Exception, match="cannot be undone"):
            migrate.downgrade(d.owner, "0017")
        with psycopg.connect(d.app, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM board.picture").fetchone() == (1,)


def test_0018_downgrade_refuses_a_drawing_panel_alone() -> None:
    """A Drawing panel whose picture was never stored still blocks the downgrade: the kind check
    of 0017 could not be put back."""
    with fresh_database() as d:
        with psycopg.connect(d.owner, autocommit=True) as conn:
            person = flow.new_person(conn)
        with psycopg.connect(d.app, autocommit=True) as conn:
            _panel(conn, person)
        with pytest.raises(Exception, match="cannot be undone"):
            migrate.downgrade(d.owner, "0017")


def test_the_engine_module_and_store_agree_on_the_picture_limit() -> None:
    assert cad_engine.check_svg is drawing.check_svg
    assert c.BOARD_PICTURE_MAX_BYTES == 2_000_000
