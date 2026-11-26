"""What the desktop reports about the Board (Board slice 1, Task 3): its receipts for Board
actions, and panel state the person changed by hand.

**Receipts** (`on_live_receipt`, called by `actions` for a live receipt of a `board.*` chunk,
after the signature, target-node, person and lateness checks there). Receipts, not claims: a
panel counts as drawn only when the desktop's Board reported it.
- `board.show` / `board.update`, in the shape of `board.show.receipt`. `RENDERED` must name the
  panel, revision and content hash that chunk issued (compared as values, not text); then
  `board.mark_rendered`, and event `board.rendered`. `REFUSED` or `ERROR`: event
  `board.render_failed` with the node's check, and Jarvis says once that the panel could not
  be shown. Nothing is marked.
- `board.set_state` / `board.focus`, in the shape of `board.set_state.receipt`. `OK` must name
  the panel issued. `REFUSED`: event `board.state_refused`, said once.
- Anything else (another shape, other values, a revision the panel never had) is refused
  `malformed` and audited (R12-24), so a faulty receipt never fails the job into a retry.
A receipt sent twice is taken twice and said once (the event of that chunk is the marker).

**Panel state** (job `board.state`, from an inbound `board.state` message). The node's own
signed message (R12-34), in the contract's shape, from a DESKTOP node, for a panel of the
node's person; then
`board.report_state`, which applies reports in arrival order. An unknown panel, or another
person's, is dropped and logged. The server stays the record: the desktop is not answered.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from jarvis_server import comm, contracts, jobs
from jarvis_server.core import actions, board, dispatch, node_signature

log = logging.getLogger("jarvis.core.board_node")

BOARD_NOT_SHOWN_TEXT = "I couldn't show that on the board."
BOARD_NOT_DONE_TEXT = "I couldn't do that on the board."

_SHOWN = frozenset({"board.show", "board.update"})
_STATE = frozenset({"board.set_state", "board.focus"})


def on_live_receipt(
    conn: psycopg.Connection[Any],
    *,
    inbound_id: UUID,
    node_id: UUID,
    person_id: UUID,
    session_id: UUID,
    reply_id: UUID,
    seq: int,
    action_kind: str,
    args: Any,
    payload: dict[str, Any],
) -> None:
    """The receipt of one `board.*` ACTION chunk, already known to be from its target node, for
    that node's person and in time."""

    def malformed() -> None:
        actions.refuse_receipt(
            conn, ref=inbound_id, node_id=node_id, code="malformed", inbound_id=inbound_id
        )

    def failed(event: str, panel_id: UUID, result: str, check: str | None, text: str) -> None:
        subject = f"{reply_id}:{seq}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"board:{subject}",))
        said = conn.execute(
            "SELECT 1 FROM comm.event WHERE kind = %s AND subject_type = 'stream'"
            " AND subject_id = %s LIMIT 1",
            (event, subject),
        ).fetchone()
        comm.record_event(
            conn, event, subject_type="stream", subject_id=subject, person_id=person_id,
            details={"panel_id": str(panel_id), "result": result, "check": check or "none"},
        )  # fmt: skip
        drawn = conn.execute(
            "SELECT 1 FROM comm.event WHERE kind = 'board.rendered' AND subject_type = 'stream'"
            " AND subject_id = %s LIMIT 1",
            (subject,),
        ).fetchone()
        if said is None and drawn is None:  # once, and never about a chunk reported drawn
            actions.speak(conn, session_id, text)

    try:
        if action_kind in _SHOWN:
            issued = contracts.BoardShowArgs.model_validate(args)
            shown = contracts.BoardShowReceipt.model_validate(payload)
        elif action_kind in _STATE:
            panel_id = contracts.BoardFocusArgs.model_validate(
                {"panel_id": args["panel_id"]}
            ).panel_id
            done = contracts.BoardSetStateReceipt.model_validate(payload)
        else:
            raise ValueError("not a Board action a node answers")
    except (ValidationError, ValueError, KeyError, TypeError):
        malformed()
        return

    if action_kind in _STATE:
        if done.result == "OK":
            if done.evidence.panel_id != panel_id:
                malformed()
                return
            actions.set_outcome(conn, inbound_id, "ANSWERED")
            return
        actions.set_outcome(conn, inbound_id, "ANSWERED")
        failed("board.state_refused", panel_id, done.result, done.evidence.check,
               BOARD_NOT_DONE_TEXT)  # fmt: skip
        return

    if shown.result != "RENDERED":
        actions.set_outcome(conn, inbound_id, "ANSWERED")
        failed("board.render_failed", issued.panel_id, shown.result, shown.evidence.check,
               BOARD_NOT_SHOWN_TEXT)  # fmt: skip
        return
    e = shown.evidence
    if (e.panel_id, e.revision, e.content_sha256) != (
        issued.panel_id, issued.revision, issued.content_sha256,
    ):  # fmt: skip
        malformed()
        return
    try:
        with conn.transaction():
            board.mark_rendered(
                conn, person_id=person_id, panel_id=issued.panel_id, revision=issued.revision
            )
    except (ValueError, board.UnknownPanel):
        malformed()
        return
    actions.set_outcome(conn, inbound_id, "ANSWERED")
    comm.record_event(
        conn, "board.rendered", subject_type="stream", subject_id=f"{reply_id}:{seq}",
        person_id=person_id,
        details={"panel_id": str(issued.panel_id), "revision": str(issued.revision)},
    )  # fmt: skip


@dispatch.register("board.state")
def on_state(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Handler of `board.state`, payload `{inbound_id}`."""
    inbound_id = UUID(job.payload["inbound_id"])
    row = conn.execute(
        "SELECT node_id, person_id, kind, payload, outcome, received_at, envelope"
        " FROM conv.inbound WHERE id = %s FOR UPDATE",
        (inbound_id,),
    ).fetchone()
    if row is None or row[4] is not None:
        return  # pruned, or already handled (a re-run job)
    node_id, person_id, kind, payload = UUID(str(row[0])), UUID(str(row[1])), row[2], row[3]
    if kind != "board.state":
        raise ValueError(f"inbound {inbound_id} is a {kind}, not a board.state")
    desktop = conn.execute(
        "SELECT 1 FROM gw.node WHERE id = %s AND kind = 'DESKTOP' AND revoked_at IS NULL",
        (node_id,),
    ).fetchone()
    if desktop is None:  # the Board is the desktop's (R12-39)
        log.info("board.state %s: not from a desktop node; dropped", inbound_id)
        actions.set_outcome(conn, inbound_id, "REFUSED")
        return
    if not node_signature.signed_by_node(
        conn, inbound_id=inbound_id, node_id=node_id, kind=kind, payload=payload,
        envelope=row[6],
    ):  # fmt: skip
        actions.refuse_receipt(
            conn, ref=inbound_id, node_id=node_id, code=node_signature.BAD_NODE_SIGNATURE,
            inbound_id=inbound_id,
        )  # fmt: skip
        return
    try:
        report = contracts.BoardState.model_validate(payload)
        applied = board.report_state(
            conn, person_id=person_id, panel_id=report.panel_id, state=report.state.value,
            reported_at=row[5],
        )  # fmt: skip
    except ValidationError:
        log.info("board.state %s: not in the contract's shape; dropped", inbound_id)
        actions.set_outcome(conn, inbound_id, "REFUSED")
        return
    except board.UnknownPanel:
        log.info("board.state %s: not a panel of this person; dropped", inbound_id)
        actions.set_outcome(conn, inbound_id, "REFUSED")
        return
    if not applied:
        log.info("board.state %s: older than the panel's last change; left out", inbound_id)
    actions.set_outcome(conn, inbound_id, "ANSWERED")
