"""The Board's panel store (Board slice 1, Task 2; migration 0016).

Core owns every panel: what Jarvis shows on the Board is stored here first, then sent to the
desktop as a signed live action. A panel belongs to one person. It has a stable number (Jarvis
calls it `p<number>`), a kind (`text` or `canvas`), a state, and revisions: an update adds a
revision and never changes an earlier one. Nothing is deleted; closing a panel is a state.

- `check_content` is the one gate for what may be stored: the limits of
  `contracts/messages/board.show.args` (a one-line title of at most 120 code points, content of
  at most 30,000 bytes of UTF-8, no control characters but tab and line breaks). `create` and
  `update` call it first; a panel that fails it is not stored.
- Every function takes the person and acts only on that person's panels: another person's
  panel, like one that does not exist, is `UnknownPanel`.
- `listing` is what Jarvis is told is on the Board: every panel that is not closed, and the
  `CLOSED_LISTED` most recently closed (closed panels are kept for ever, the list is not).
- `rendered_revision` is the newest revision the desktop reported drawn (`mark_rendered`, from
  a `RENDERED` receipt): what was shown, as opposed to what was sent.

Panel content is untrusted data wherever it is read back (it may come from web pages).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from pydantic import TypeAdapter, ValidationError

from jarvis_server import contracts as c

CLOSED_LISTED = 10
CONTENT_BUDGET = 12_000  # characters of open panels' content given to a Board job

_STATES = frozenset(s.value for s in c.PanelState)
_KINDS = frozenset(k.value for k in c.PanelKind)
_TITLE: TypeAdapter[str] = TypeAdapter(c.PanelTitle)
_CONTENT: TypeAdapter[str] = TypeAdapter(c.PanelContent)
_HANDLE = re.compile(r"p([1-9][0-9]{0,8})")


class UnknownPanel(LookupError):
    """No such panel of this person (or a reference that names none)."""


class BadPanel(ValueError):
    """A kind, title or content the Board's contract does not allow."""


class TooLarge(BadPanel):
    """Content over `contracts.BOARD_CONTENT_MAX_BYTES` bytes of UTF-8."""


@dataclass(frozen=True)
class Panel:
    """A panel at its current revision."""

    panel_id: UUID
    kind: str
    title: str
    state: str
    revision: int
    content: str
    content_sha256: str
    cost_paise: int
    created_at: datetime

    def args(self) -> dict[str, Any]:
        """The `authz.object.args` of a `board.show` or `board.update` for this revision."""
        return {
            "panel_id": str(self.panel_id),
            "revision": self.revision,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "cost_paise": self.cost_paise,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class PanelLine:
    """One line of Jarvis's list of the Board."""

    handle: str
    panel_id: UUID
    kind: str
    title: str
    state: str


def check_content(kind: str, title: str, content: str) -> None:
    """Raise `TooLarge` or `BadPanel` unless this may be stored and sent as a panel."""
    if kind not in _KINDS:
        raise BadPanel("unknown panel kind")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:  # a lone surrogate
        raise BadPanel("content is not valid text") from exc
    if size > c.BOARD_CONTENT_MAX_BYTES:
        raise TooLarge("content is over 30,000 bytes")
    try:
        _TITLE.validate_python(title)
        _CONTENT.validate_python(content)
    except ValidationError as exc:
        raise BadPanel("title or content is not allowed") from exc


_PANEL = (
    "SELECT p.id, p.kind, p.title, p.state, p.revision, r.content, r.content_sha256,"
    " p.cost_paise, p.created_at FROM board.panel p"
    " JOIN board.panel_revision r ON r.panel_id = p.id AND r.revision = p.revision"
    " WHERE p.id = %s AND p.person_id = %s"
)


def get(
    conn: psycopg.Connection[Any], person_id: UUID, panel_id: UUID, *, lock: bool = False
) -> Panel:
    """This person's panel at its current revision. `lock`: FOR UPDATE of the panel row."""
    row = conn.execute(
        _PANEL + (" FOR UPDATE OF p" if lock else ""), (panel_id, person_id)
    ).fetchone()
    if row is None:
        raise UnknownPanel(str(panel_id))
    return Panel(UUID(str(row[0])), *row[1:])


def create(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    session_id: UUID | None,
    kind: str,
    title: str,
    content: str,
    cost_paise: int,
) -> Panel:
    """Store a new panel (revision 1, open) and return it."""
    check_content(kind, title, content)
    with conn.transaction():
        # One number at a time per person, so two jobs never take the same `p<number>`.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"board.panel:{person_id}",)
        )
        row = conn.execute(
            "INSERT INTO board.panel (person_id, number, session_id, kind, title, cost_paise)"
            " SELECT %(p)s, coalesce(max(number), 0) + 1, %(s)s, %(k)s, %(t)s, %(c)s"
            " FROM board.panel WHERE person_id = %(p)s RETURNING id",
            {"p": person_id, "s": session_id, "k": kind, "t": title, "c": _cost(cost_paise)},
        ).fetchone()
        assert row is not None
        panel_id = UUID(str(row[0]))
        _add_revision(conn, panel_id, 1, title, content)
        return get(conn, person_id, panel_id)


def update(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    panel_id: UUID,
    title: str,
    content: str,
    cost_paise: int,
) -> Panel:
    """Add a revision with this title and content; `cost_paise` is added to the panel's total."""
    with conn.transaction():
        held = get(conn, person_id, panel_id, lock=True)
        check_content(held.kind, title, content)
        revision = held.revision + 1
        _add_revision(conn, panel_id, revision, title, content)
        conn.execute(
            "UPDATE board.panel SET revision = %s, title = %s, updated_at = now(),"
            " cost_paise = least(cost_paise::bigint + %s, %s) WHERE id = %s",
            (revision, title, _cost(cost_paise), c.INT_MAX, panel_id),
        )
        return get(conn, person_id, panel_id)


def set_state(
    conn: psycopg.Connection[Any], *, person_id: UUID, panel_id: UUID, state: str
) -> Panel:
    """Record a panel's state (`closed` keeps the panel)."""
    if state not in _STATES:
        raise ValueError("unknown panel state")
    with conn.transaction():
        held = get(conn, person_id, panel_id, lock=True)
        if held.state != state:
            conn.execute(
                "UPDATE board.panel SET state = %s, state_changed_at = now() WHERE id = %s",
                (state, panel_id),
            )
        return get(conn, person_id, panel_id)


def mark_rendered(
    conn: psycopg.Connection[Any], *, person_id: UUID, panel_id: UUID, revision: int
) -> None:
    """The desktop drew this revision. A late report of an older one changes nothing."""
    with conn.transaction():
        held = get(conn, person_id, panel_id, lock=True)
        if not 1 <= revision <= held.revision:
            raise ValueError("no such revision of this panel")
        conn.execute(
            "UPDATE board.panel"
            " SET rendered_revision = greatest(coalesce(rendered_revision, 0), %s) WHERE id = %s",
            (revision, panel_id),
        )


_LISTING = """
SELECT number, id, kind, title, state FROM (
    SELECT p.*, row_number() OVER (PARTITION BY (p.state = 'closed')
                                   ORDER BY p.state_changed_at DESC, p.number DESC) AS nth
    FROM board.panel p WHERE p.person_id = %s
) ranked
WHERE state <> 'closed' OR nth <= %s
ORDER BY number DESC
"""


def listing(conn: psycopg.Connection[Any], person_id: UUID) -> list[PanelLine]:
    """Jarvis's list of this person's Board, newest panel first."""
    rows = conn.execute(_LISTING, (person_id, CLOSED_LISTED)).fetchall()
    return [PanelLine(f"p{r[0]}", UUID(str(r[1])), r[2], r[3], r[4]) for r in rows]


def resolve(conn: psycopg.Connection[Any], person_id: UUID, handle: str) -> UUID:
    """The panel Jarvis means by `p<number>`."""
    match = _HANDLE.fullmatch(handle) if isinstance(handle, str) else None
    if match is None:
        raise UnknownPanel("not a panel reference")
    row = conn.execute(
        "SELECT id FROM board.panel WHERE person_id = %s AND number = %s",
        (person_id, int(match.group(1))),
    ).fetchone()
    if row is None:
        raise UnknownPanel(handle)
    return UUID(str(row[0]))


def open_content(
    conn: psycopg.Connection[Any], person_id: UUID, *, budget_chars: int = CONTENT_BUDGET
) -> list[tuple[PanelLine, str]]:
    """The content of the panels on show (open or maximised), newest first, cut to the budget.
    Untrusted data: the caller labels it so."""
    rows = conn.execute(
        "SELECT p.number, p.id, p.kind, p.title, p.state, r.content FROM board.panel p"
        " JOIN board.panel_revision r ON r.panel_id = p.id AND r.revision = p.revision"
        " WHERE p.person_id = %s AND p.state IN ('open', 'maximised') ORDER BY p.number DESC",
        (person_id,),
    ).fetchall()
    out: list[tuple[PanelLine, str]] = []
    left = budget_chars
    for number, panel_id, kind, title, state, content in rows:
        if left <= 0:
            break
        text = content[:left]
        left -= len(text)
        out.append((PanelLine(f"p{number}", UUID(str(panel_id)), kind, title, state), text))
    return out


def _cost(cost_paise: int) -> int:
    if isinstance(cost_paise, bool) or not isinstance(cost_paise, int) or cost_paise < 0:
        raise ValueError("cost_paise is a whole number of paise, zero or more")
    return min(cost_paise, c.INT_MAX)


def _add_revision(
    conn: psycopg.Connection[Any], panel_id: UUID, revision: int, title: str, content: str
) -> None:
    conn.execute(
        "INSERT INTO board.panel_revision (panel_id, revision, title, content, content_sha256)"
        " VALUES (%s, %s, %s, %s, %s)",
        (panel_id, revision, title, content, c.board_content_sha256(content)),
    )
