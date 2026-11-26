"""The Board's panel store (migration 0016; plan `.10x/plans/2026-11-26-board-slice-1.md`, Task 2).

Runs in its own database: panels block the 0016 downgrade, so they must never reach the
session database that P0-6 takes down and up. People and panel content are fictional.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from jarvis_server import contracts as c
from jarvis_server.core import board
from jarvis_server.core.board import BadPanel, TooLarge, UnknownPanel

from .conftest import Dsns, fresh_database
from .helpers import flow
from .helpers.threads import run_all
from .schema_registry import CONTRACTS, validator

Conn = psycopg.Connection[Any]

TEXT = (
    "## December trip\n\n**Recommendation: Coorg.**\n\n"
    "| Place | Travel |\n|---|---|\n| Coorg | 5 h |\n"
)
PAGE = "<!doctype html><title>Cost</title><svg width='10' height='10'></svg>"


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def person(owner: Conn) -> uuid.UUID:
    return flow.new_person(owner)


@pytest.fixture
def session(app: Conn, person: uuid.UUID) -> uuid.UUID:
    return flow.open_session(app, person)


def _make(app: Conn, person: uuid.UUID, session: uuid.UUID, title: str = "December trip",
          *, kind: str = "text", content: str = TEXT, cost: int = 0) -> board.Panel:  # fmt: skip
    return board.create(
        app, person_id=person, session_id=session, kind=kind, title=title, content=content,
        cost_paise=cost,
    )  # fmt: skip


def _count(app: Conn, person: uuid.UUID) -> int:
    row = app.execute("SELECT count(*) FROM board.panel WHERE person_id = %s", (person,)).fetchone()
    assert row is not None
    return int(row[0])


def test_create_then_listing_is_newest_first_with_stable_handles(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    first = _make(app, person, session, "December trip", cost=1240)
    second = _make(app, person, session, "Cost per option", kind="canvas", content=PAGE)
    assert (first.revision, first.state, first.cost_paise) == (1, "open", 1240)
    assert first.content == TEXT and first.content_sha256 == c.board_content_sha256(TEXT)
    lines = board.listing(app, person)
    assert [(p.handle, p.panel_id, p.kind, p.title, p.state) for p in lines] == [
        ("p2", second.panel_id, "canvas", "Cost per option", "open"),
        ("p1", first.panel_id, "text", "December trip", "open"),
    ]
    third = _make(app, person, session, "School holiday dates")
    again = {p.panel_id: p.handle for p in board.listing(app, person)}
    assert again == {third.panel_id: "p3", second.panel_id: "p2", first.panel_id: "p1"}


def test_handles_are_per_person(
    app: Conn, owner: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    other = flow.new_person(owner, "Ravi Fictional")
    mine = _make(app, person, session)
    theirs = _make(app, other, flow.open_session(app, other))
    assert board.resolve(app, person, "p1") == mine.panel_id
    assert board.resolve(app, other, "p1") == theirs.panel_id


@pytest.mark.parametrize("handle", ["p2", "p0", "P1", "1", "p", "p1 ", "p-1", "p99999999999", ""])
def test_unknown_handles(app: Conn, person: uuid.UUID, session: uuid.UUID, handle: str) -> None:
    _make(app, person, session)
    with pytest.raises(UnknownPanel):
        board.resolve(app, person, handle)


def test_update_adds_a_revision_and_keeps_the_old_one(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    made = _make(app, person, session, cost=1240)
    longer = TEXT + "| Goa | 1 h flight |\n"
    new = board.update(
        app, person_id=person, panel_id=made.panel_id, title="December trip: two options",
        content=longer, cost_paise=310,
    )  # fmt: skip
    assert (new.revision, new.title, new.content) == (2, "December trip: two options", longer)
    assert new.cost_paise == 1550  # the panel's total so far
    assert (new.kind, new.created_at, new.state) == (made.kind, made.created_at, made.state)
    rows = app.execute(
        "SELECT revision, title, content, content_sha256 FROM board.panel_revision"
        " WHERE panel_id = %s ORDER BY revision",
        (made.panel_id,),
    ).fetchall()
    assert rows == [
        (1, "December trip", TEXT, c.board_content_sha256(TEXT)),
        (2, "December trip: two options", longer, c.board_content_sha256(longer)),
    ]
    assert board.get(app, person, made.panel_id) == new


def test_another_persons_panel_is_unknown(
    app: Conn, owner: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    other = flow.new_person(owner, "Ravi Fictional")
    theirs = _make(app, other, flow.open_session(app, other))
    with pytest.raises(UnknownPanel):
        board.update(app, person_id=person, panel_id=theirs.panel_id, title="x", content="y",
                     cost_paise=0)  # fmt: skip
    with pytest.raises(UnknownPanel):
        board.set_state(app, person_id=person, panel_id=theirs.panel_id, state="closed")
    with pytest.raises(UnknownPanel):
        board.mark_rendered(app, person_id=person, panel_id=theirs.panel_id, revision=1)
    with pytest.raises(UnknownPanel):
        board.get(app, person, theirs.panel_id)
    with pytest.raises(UnknownPanel):
        board.get(app, person, uuid.uuid4())
    assert board.get(app, other, theirs.panel_id) == theirs  # untouched
    assert board.listing(app, person) == []


def test_content_over_30000_bytes_is_too_large(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    wide = "é" * 15_001  # 30,002 bytes in 15,001 characters
    with pytest.raises(TooLarge):
        board.check_content("text", "Fits a title", wide)
    with pytest.raises(TooLarge):
        _make(app, person, session, content=wide)
    assert _count(app, person) == 0
    made = _make(app, person, session, content="é" * 15_000)
    with pytest.raises(TooLarge):
        board.update(app, person_id=person, panel_id=made.panel_id, title="t", content=wide,
                     cost_paise=5)  # fmt: skip
    assert board.get(app, person, made.panel_id) == made  # no revision, no cost added


@pytest.mark.parametrize(
    ("kind", "title", "content"),
    [
        ("html", "A title", TEXT),
        ("text", "", TEXT),
        ("text", "   ", TEXT),
        ("text", "two\nlines", TEXT),
        ("text", "t" * 121, TEXT),
        ("text", "A title", ""),
        ("text", "A title", "nul \x00 inside"),
        ("canvas", "A title", "bell \x07 inside"),
    ],
)
def test_bad_panels_are_refused_and_not_stored(
    app: Conn, person: uuid.UUID, session: uuid.UUID, kind: str, title: str, content: str
) -> None:
    with pytest.raises(BadPanel):
        board.check_content(kind, title, content)
    with pytest.raises(BadPanel):
        _make(app, person, session, title, kind=kind, content=content)
    assert _count(app, person) == 0


def test_set_state_closed_keeps_the_panel(app: Conn, person: uuid.UUID, session: uuid.UUID) -> None:
    made = _make(app, person, session)
    for state in ("minimised", "maximised", "closed", "open"):
        assert (
            board.set_state(app, person_id=person, panel_id=made.panel_id, state=state).state
            == state
        )
    closed = board.set_state(app, person_id=person, panel_id=made.panel_id, state="closed")
    assert (closed.content, closed.revision) == (TEXT, 1)
    assert [p.state for p in board.listing(app, person)] == ["closed"]
    with pytest.raises(ValueError):
        board.set_state(app, person_id=person, panel_id=made.panel_id, state="hidden")


def test_listing_keeps_only_the_ten_most_recently_closed(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    """Closed panels are kept for ever, but Jarvis's list is not: the open ones, and the ten
    most recently closed."""
    panels = [_make(app, person, session, f"Panel {i}") for i in range(1, 15)]
    for p in panels[:12]:  # close 1..12, in that order
        board.set_state(app, person_id=person, panel_id=p.panel_id, state="closed")
    lines = board.listing(app, person)
    assert [p.title for p in lines if p.state == "open"] == ["Panel 14", "Panel 13"]
    assert {p.title for p in lines if p.state == "closed"} == {f"Panel {i}" for i in range(3, 13)}
    assert _count(app, person) == 14


def test_mark_rendered_records_the_revision(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    made = _make(app, person, session)
    sql = "SELECT rendered_revision FROM board.panel WHERE id = %s"
    assert app.execute(sql, (made.panel_id,)).fetchone() == (None,)
    board.mark_rendered(app, person_id=person, panel_id=made.panel_id, revision=1)
    assert app.execute(sql, (made.panel_id,)).fetchone() == (1,)
    board.update(
        app, person_id=person, panel_id=made.panel_id, title="t", content="v2", cost_paise=0
    )
    assert app.execute(sql, (made.panel_id,)).fetchone() == (1,)  # revision 2 is not drawn yet
    board.mark_rendered(app, person_id=person, panel_id=made.panel_id, revision=2)
    board.mark_rendered(
        app, person_id=person, panel_id=made.panel_id, revision=1
    )  # late: no step back
    assert app.execute(sql, (made.panel_id,)).fetchone() == (2,)
    with pytest.raises(ValueError):  # a revision that was never made
        board.mark_rendered(app, person_id=person, panel_id=made.panel_id, revision=3)


def test_open_content_is_newest_first_within_the_budget(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    a = _make(app, person, session, "A", content="a" * 100)
    b = _make(app, person, session, "B", content="b" * 100)
    c_ = _make(app, person, session, "C", content="c" * 100)
    d = _make(app, person, session, "D", content="d" * 100)
    board.set_state(app, person_id=person, panel_id=b.panel_id, state="minimised")
    board.set_state(app, person_id=person, panel_id=a.panel_id, state="closed")
    board.set_state(app, person_id=person, panel_id=c_.panel_id, state="maximised")
    got = board.open_content(app, person)
    assert [(line.title, text) for line, text in got] == [("D", "d" * 100), ("C", "c" * 100)]
    cut = board.open_content(app, person, budget_chars=150)
    assert [(line.title, len(text)) for line, text in cut] == [("D", 100), ("C", 50)]
    assert board.open_content(app, person, budget_chars=100) == [got[0]]
    assert d.panel_id == got[0][0].panel_id


def test_panel_args_conform_to_the_contract(
    app: Conn, person: uuid.UUID, session: uuid.UUID
) -> None:
    made = _make(app, person, session, cost=1240)
    args = made.args()
    schema = (CONTRACTS / "messages" / "board.show.args.schema.json").read_text()
    validator(json.loads(schema)).validate(args)
    c.BoardShowArgs.model_validate(args)
    assert args["panel_id"] == str(made.panel_id) and args["revision"] == 1
    assert args["cost_paise"] == 1240 and args["kind"] == "text"


def test_concurrent_creates_get_distinct_handles(dsns: Dsns, app: Conn, person: uuid.UUID,
                                                 session: uuid.UUID) -> None:  # fmt: skip
    def one(i: int) -> None:
        with psycopg.connect(dsns.app, autocommit=True) as conn:
            for j in range(5):
                board.create(conn, person_id=person, session_id=session, kind="text",
                             title=f"Panel {i}.{j}", content=TEXT, cost_paise=0)  # fmt: skip

    run_all([lambda i=i: one(i) for i in range(4)])
    handles = [p.handle for p in board.listing(app, person)]
    assert sorted(handles, key=lambda h: int(h[1:])) == [f"p{n}" for n in range(1, 21)]


def test_gateway_role_cannot_reach_panels(gateway: Conn, app: Conn, person: uuid.UUID,
                                          session: uuid.UUID) -> None:  # fmt: skip
    made = _make(app, person, session)
    for sql in (
        "SELECT 1 FROM board.panel",
        "SELECT 1 FROM board.panel_revision",
        "UPDATE board.panel SET state = 'closed'",
        "DELETE FROM board.panel_revision",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(sql)
    assert board.get(app, person, made.panel_id) == made


def test_app_role_cannot_delete_or_rewrite_a_revision(app: Conn, person: uuid.UUID,
                                                      session: uuid.UUID) -> None:  # fmt: skip
    """Nothing is deleted, and a stored revision is never changed: the app role may only add."""
    made = _make(app, person, session)
    for sql in (
        "DELETE FROM board.panel WHERE id = %s",
        "DELETE FROM board.panel_revision WHERE panel_id = %s",
        "UPDATE board.panel_revision SET content = 'x' WHERE panel_id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(sql, (made.panel_id,))
