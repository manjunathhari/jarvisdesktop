"""The conversation turn: job `conv.turn` (spec §6.2, §10.1, §10.2).

One job runs one utterance (`conv.inbound`) through these steps:

1. Read the turn, without a lock, in the job's transaction. A turn with an outcome is done.
2. Refuse a blank utterance (`REFUSED`, no notification, no call) or a person who is not in
   memory.
3. Append the PERSON message to the person's session (`sessions.open_or_join`).
4. If the brain is DOWN, hold the turn before reserving anything (§10.2).
5. Build the request (`prompt.build`, memory viewer = the authenticated person, P2-S2) and
   validate it. Price it: a model without a price is a configuration error (`REFUSED`, event
   and TODAY notification `brain.config_error`, no call).
6. `budget.reserve`, then the step-6 table of §10.1: RESERVED calls; OWNED_ELSEWHERE and
   ANSWERED change nothing; INDETERMINATE was handled by `reserve`; NO_BUDGET holds the turn
   for at most `CAP_HOLD`, retried every `CAP_RETRY`, then expires (`turn.cap_expired`).
   A model without a price notifies the owner, not the turn's person (R12-19).
7. Call the brain, streaming SPEECH chunks with NOTIFY `jarvis_stream` (only the reply_id),
   each carrying the turn's `session_id` (R9-1). Each ToolCall's wire name is mapped to its
   action kind (`prompt.TOOL_ACTIONS`; an unmapped name is refused as `unknown_action`), then
   decided by `policy.decide_tool_call` for the node that spoke, with the memory the request
   was built with. A refused call is not executed, writes no ACTION chunk, records event
   `policy.refused` (codes only) and is explained once in the reply; the turn goes on. READ_ONLY:
   a signed live ACTION chunk in this reply (`actions.issue_live`). REVERSIBLE (`memory.write`):
   a STATED entry by the authenticated person. IRREVERSIBLE (`pc.delete_test_file`):
   `approvals.propose_delete`, which lists the folder and proposes the approval.
   The Board's tools (slice 1) are READ_ONLY and handled by `_board`: `board_make` queues one
   `board.make` job for the turn and issues nothing; `board_set_state` and `board_focus`
   resolve the panel's handle for the authenticated person, record the state, and issue a live
   action carrying the panel's ID. A handle that is not the person's panel is refused
   (`unknown_panel`), like any refused call.
8. After the call: CONNECT failure -> `release`, hold, re-run (the turn may try again);
   SEND failure, any unplaceable exception, or a completed stream with no usage -> `lapse`
   now, which runs the indeterminate path once (Task 9); success -> `settle`, the final chunk,
   the JARVIS message and `ANSWERED`, in one transaction. A turn whose person was told about
   its hold (`brain.down.with_held`, recorded as `told_held` in the held job's payload) sends
   `turn.completed_after_hold` with the reply; a quiet re-queue or a cap hold sends none
   (R12-20).

Transactions and locks (R12-13, Task 9's lesson). Every step that touches the turn row, the
session or the budget runs on `budget.committing_connection(conn)`, a sibling connection whose
short transactions commit at once. So the reservation is committed before any request byte is
sent, chunks are visible to the gateway as they are written, and no lock (turn, session,
budget) is held during the call. The job's own transaction reads the turn and the brain state,
and writes only what must commit with the job's completion: the re-queued job of a hold, its
notification and the brain-health count. Lock order on the sibling, always: the turn row, then
budget rows, then the session row.

A turn reaches Claude at most once without the person asking again (P2-B4, P2-B5): a re-run
job meets the turn's outcome, or its live reservation in `budget.reserve`.

Holds re-enqueue a new `conv.turn` job with `hold` in its payload. A turn held for the brain
(DOWN, or re-queued after a CONNECT failure) that has not run within `DOWN_HOLD` (1 h) of its
arrival expires: `REFUSED`, `turn.down_expired`, no call (R12-18). When the brain comes back
(`health.came_back`), `release_held` makes every brain-held turn job ready at once, ordered by
the turn's arrival (`run_at` = `received_at`), so held turns run once each, in order.

Time: `now` for sessions and holds comes from `set_clock` (tests) or the database. Budget steps
always use the database's `now()` and the job's lease.

The overall limit on a call is `TURN_MAX_TOKENS` of output plus the job lease: a call still
running when its lease ends is lapsed by the sweep, and its late `settle` is refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from jarvis_server import comm, config, jobs, policy
from jarvis_server.brain import budget, health, prices
from jarvis_server.brain.claude import count_input_tokens
from jarvis_server.brain.types import (
    Brain,
    BrainCallFailed,
    BrainRequest,
    CallPhase,
    Speech,
    ToolCall,
)
from jarvis_server.contracts import MemoryCategory
from jarvis_server.core import (
    actions,
    approvals,
    board,
    dispatch,
    identity,
    prompt,
    rules,
    sessions,
)
from jarvis_server.core import board_node as _board_jobs  # noqa: F401  registers job kinds
from jarvis_server.core.prompt import TURN_MAX_TOKENS
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.memory.errors import MemoryEngineError
from jarvis_server.policy import NeedsApproval, Refuse, Tier, decide_tool_call

log = logging.getLogger("jarvis.core.turn")

__all__ = [
    "CAP_HOLD",
    "CAP_RETRY",
    "DOWN_HOLD",
    "TURN_MAX_TOKENS",
    "NoMemory",
    "NoTurnBrain",
    "Refuse",
    "decide_tool_call",
    "release_held",
    "run",
    "set_brain",
    "set_clock",
    "set_memory",
]

CAP_HOLD = timedelta(hours=1)  # a cap-held turn waits at most this (§10.1, K2-5)
CAP_RETRY = timedelta(minutes=5)  # and retries its reservation this often
BRAIN_RETRY = timedelta(minutes=2)  # a brain-held turn re-checks at the probe's interval
DOWN_HOLD = timedelta(hours=1)  # and expires this long after it arrived (R12-18)
CHUNK_MAX = 4000  # conv.stream_chunk.text
MESSAGE_MAX = 20000  # conv.message.text

# The reply when the brain answered with no speech (only tool calls, or nothing): a turn always
# ends with a final chunk and a JARVIS message.
NO_SPEECH_TEXT = "I can't do that from here yet."
# The reply when the brain only ran actions on the node (a panel, say) and said nothing: the
# action is the answer, and the node's module speaks its own result (live check, 25 Nov).
ACTION_ONLY_TEXT = "Okay."
# What Jarvis says about a tool call (§9.2: a refusal is explained in the reply).
REFUSED_TEXT = "I'm not allowed to do that."
APPROVAL_TEXT = "That needs your approval."
NOT_LISTED_TEXT = "I can't find {name} in the test folder."
NO_LISTING_TEXT = "I couldn't check the test folder just now."
REMEMBERED_TEXT = "I'll remember that."
NOT_REMEMBERED_TEXT = "I couldn't remember that."
# The Board. The reply when the brain queued Board work and said nothing itself.
BOARD_ACK_TEXT = "On it. I'll put it on the board."
NO_PANEL_TEXT = "I can't find that panel on the board."
BOARD_UNCLEAR_TEXT = "I didn't catch what to do on the board."
# A fact the person states about themselves: their own identity file (no domain or date needed).
MEMORY_CATEGORY = MemoryCategory.IDENTITY
CAP_EXPIRED_TEXT = {
    "monthly": "I couldn't answer that: the monthly limit is reached.",
    "daily": "I couldn't answer that: today's limit is reached.",
}

BrainProvider = Callable[[], tuple[Brain, str]]  # -> (a new brain for this job, model name)
Clock = Callable[[psycopg.Connection[Any]], datetime]

_brain: BrainProvider | None = None
_memory: MemoryEngine | None = None
_clock: Clock | None = None


class NoTurnBrain(RuntimeError):
    """A turn ran with no brain configured."""


class NoMemory(RuntimeError):
    """A turn ran with no memory engine configured."""


def set_brain(provider: BrainProvider | None) -> None:
    """The brain factory: called once per job, for a new brain instance (Task 8 carry-over b).
    The model it names must be in the price table (R12-12: `prices.default_model()`)."""
    global _brain
    _brain = provider


def set_memory(engine: MemoryEngine | None) -> None:
    """The memory engine for the prompt, the policy's memory check and `memory.write`."""
    global _memory
    _memory = engine
    policy.set_memory(engine)


def set_clock(clock: Clock | None) -> None:
    """Tests only: the time sessions and holds use. None: the database's `now()`."""
    global _clock
    _clock = clock


# --- the turn row ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Inbound:
    id: UUID
    node_id: UUID
    node_kind: str
    person_id: UUID
    kind: str
    payload: dict[str, Any]
    outcome: str | None
    received_at: datetime


def _read(conn: psycopg.Connection[Any], turn_id: UUID) -> _Inbound | None:
    """The turn, without a lock (the job's transaction never locks it)."""
    row = conn.execute(
        "SELECT i.node_id, n.kind, i.person_id, i.kind, i.payload, i.outcome, i.received_at"
        " FROM conv.inbound i JOIN gw.node n ON n.id = i.node_id WHERE i.id = %s",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    return _Inbound(
        turn_id,
        UUID(str(row[0])),
        str(row[1]),
        UUID(str(row[2])),
        str(row[3]),
        row[4],
        row[5],
        row[6],
    )


def _utterance(payload: dict[str, Any]) -> tuple[str, str]:
    """(text, screen summary) from the node's payload; anything else in it is ignored."""
    text = payload.get("text")
    screen = payload.get("screen_summary")
    return (
        text if isinstance(text, str) else "",
        screen if isinstance(screen, str) else "",
    )


def _now(conn: psycopg.Connection[Any]) -> datetime:
    if _clock is not None:
        return _clock(conn)
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def _lease(conn: psycopg.Connection[Any], job_id: int) -> tuple[datetime, datetime]:
    """(the database's now, the job's lease end): the reservation's lease is the job's."""
    row = conn.execute(
        "SELECT now(), locked_until FROM ops.job WHERE id = %s", (job_id,)
    ).fetchone()
    if row is None or row[1] is None or row[1] <= row[0]:
        raise ValueError(f"turn job {job_id} holds no lease")
    return row[0], row[1]


def _finish(
    c: psycopg.Connection[Any],
    turn: _Inbound,
    outcome: str,
    event: str | None = None,
    details: dict[str, str] | None = None,
    context: rules.NotifyContext | None = None,
    *,
    recipient: UUID | None = None,
) -> None:
    """Set the turn's outcome once, with its event and notification, in one transaction. The
    notification goes to `recipient`, by default the turn's person."""
    with c.transaction():
        updated = c.execute(
            "UPDATE conv.inbound SET outcome = %s WHERE id = %s AND outcome IS NULL",
            (outcome, turn.id),
        )
        if updated.rowcount != 1 or event is None:
            return
        person_id = turn.person_id if recipient is None else recipient
        event_id = comm.record_event(
            c, event, subject_type="inbound", subject_id=str(turn.id), person_id=person_id,
            details=details or {},
        )  # fmt: skip
        rules.notify(c, event, person_id=person_id, event_id=event_id, context=context or {})


def _expire_unreached(
    c: psycopg.Connection[Any],
    turn: _Inbound,
    event: str,
    details: dict[str, str] | None = None,
    context: rules.NotifyContext | None = None,
) -> bool:
    """Expire a held turn (`REFUSED`, `event`) only if it has never reached Claude: no
    ACTIVE, SETTLED or LAPSED reservation (a RELEASED one never reached it). Under the turn
    lock, in one transaction; reserve inserts live reservations under the same lock. False
    when such a reservation exists: the caller leaves the turn to the step-6 table."""
    with c.transaction():
        c.execute("SELECT 1 FROM conv.inbound WHERE id = %s FOR UPDATE", (turn.id,))
        live = c.execute(
            "SELECT 1 FROM ops.brain_reservation WHERE turn_id = %s"
            " AND state IN ('ACTIVE', 'SETTLED', 'LAPSED') LIMIT 1",
            (turn.id,),
        ).fetchone()
        if live is not None:
            return False
        _finish(c, turn, "REFUSED", event, details, context)
        return True


def _person_message(
    c: psycopg.Connection[Any], turn: _Inbound, text: str, now: datetime
) -> UUID | None:
    """Put the turn in a session and append its PERSON message, once. The session, or None if
    the turn has meanwhile got an outcome."""
    with c.transaction():
        row = c.execute(
            "SELECT outcome, session_id FROM conv.inbound WHERE id = %s FOR UPDATE", (turn.id,)
        ).fetchone()
        assert row is not None
        if row[0] is not None:
            return None
        if row[1] is not None:  # an earlier run of this turn did this
            return UUID(str(row[1]))
        session_id = sessions.open_or_join(c, turn.person_id, now)
        c.execute("UPDATE conv.inbound SET session_id = %s WHERE id = %s", (session_id, turn.id))
        _append(c, session_id, "PERSON", turn.person_id, turn.node_id, text, turn.id)
        return session_id


def _append(
    c: psycopg.Connection[Any],
    session_id: UUID,
    role: str,
    person_id: UUID,
    node_id: UUID | None,
    text: str,
    inbound_id: UUID,
) -> None:
    """Append a message; the caller holds the session row lock, so `seq` is in order."""
    c.execute(
        "INSERT INTO conv.message (session_id, seq, role, person_id, node_id, text, inbound_id)"
        " SELECT %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s, %s FROM conv.message"
        " WHERE session_id = %s",
        (session_id, role, person_id, node_id, text[:MESSAGE_MAX], inbound_id, session_id),
    )


# --- holds -----------------------------------------------------------------------------------

# Whether the person was sent `kinds` since their last "Claude back" (this outage).
_TOLD = (
    "SELECT 1 FROM comm.outbox WHERE person_id = %(p)s AND kind = ANY(%(k)s)"
    " AND created_at > coalesce((SELECT max(created_at) FROM comm.outbox"
    "   WHERE person_id = %(p)s AND kind = 'brain.back'), '-infinity') LIMIT 1"
)


def _tell_down(conn: psycopg.Connection[Any], turn: _Inbound, *, held: bool) -> bool:
    """CLAUDE_DOWN to the turn's person, once per outage: `brain.down.with_held` (TODAY) when
    their turn is on hold, superseding their open plain `brain.down`; else `brain.down`.
    True: the person has been sent the notice for this outage (now or earlier), so a held
    turn of theirs counts as told about its hold (R12-20)."""
    kind = "brain.down.with_held" if held else "brain.down"
    told = ["brain.down.with_held"] if held else ["brain.down", "brain.down.with_held"]
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"brain.down:{turn.person_id}",)
    )
    if conn.execute(_TOLD, {"p": turn.person_id, "k": told}).fetchone() is not None:
        return True
    context: rules.NotifyContext = {}
    if held:
        row = conn.execute(
            "SELECT id FROM comm.outbox WHERE person_id = %s AND kind = 'brain.down'"
            " AND state = ANY(%s) ORDER BY created_at DESC, id DESC LIMIT 1",
            (turn.person_id, ["PENDING", "DELIVERING", "DELIVERED"]),
        ).fetchone()
        if row is not None:
            context["supersedes_id"] = UUID(str(row[0]))
    event_id = comm.record_event(
        conn, kind, subject_type="inbound", subject_id=str(turn.id), person_id=turn.person_id,
        details={},
    )  # fmt: skip
    rules.notify(conn, kind, person_id=turn.person_id, event_id=event_id, context=context)
    return True


def _hold(
    conn: psycopg.Connection[Any],
    job: jobs.Job,
    reason: str,
    delay: timedelta,
    *,
    told: bool = False,
    **extra: str,
) -> None:
    """Re-enqueue the turn as a new job (it commits with this job's completion). `told`: the
    person has been sent the hold notice; it stays in the payload of every later hold."""
    payload: dict[str, Any] = {**job.payload, "hold": reason, "held": True, **extra}
    if told:
        payload["told_held"] = True
    jobs.enqueue(conn, "conv.turn", payload, delay=delay)


def _cap_reason(conn: psycopg.Connection[Any], db_now: datetime, ceiling: int) -> str:
    """ "monthly" when the month's cap refuses `ceiling`, else "daily" (today's ceiling)."""
    month, _ = budget.ist_month_day(db_now)
    row = conn.execute(
        "SELECT cap_inr, settled_inr, reserved_inr FROM ops.brain_budget WHERE month = %s",
        (month,),
    ).fetchone()
    if row is None or row[1] + row[2] + ceiling > row[0]:
        return "monthly"
    return "daily"


@health.on_back
def release_held(conn: psycopg.Connection[Any]) -> None:
    """The brain is back: every brain-held turn job is ready now, in the turns' arrival order
    (§10.2). Cap holds keep their own schedule."""
    released = conn.execute(
        "UPDATE ops.job j SET run_at = i.received_at FROM conv.inbound i"
        " WHERE j.kind = 'conv.turn' AND j.status = 'QUEUED' AND j.payload->>'hold' = 'brain'"
        " AND i.id = (j.payload->>'inbound_id')::uuid"
    )
    if released.rowcount:
        log.info("brain back: %d held turn(s) released", released.rowcount)


# --- the stream ------------------------------------------------------------------------------


class _Stream:
    """SPEECH chunks of one reply, each committed with its NOTIFY. One piece is kept back, so
    that the last one can be written as the final chunk when the call has been settled."""

    def __init__(self, c: psycopg.Connection[Any], session_id: UUID, in_reply_to: UUID) -> None:
        self.c = c
        self.session_id = session_id
        self.in_reply_to = in_reply_to  # the utterance this reply answers (R12-37)
        self.reply_id = uuid.uuid4()
        self.seq = 0
        self.pieces: list[str] = []
        self.acted = False  # a live action was issued in this reply
        self.queued = False  # Board work was queued in this reply
        self._pending: str | None = None
        self._space_next = False  # after Jarvis's own words, the brain's next piece is spaced

    def speech(self, text: str) -> None:
        if self._space_next and text and not text[0].isspace():
            text = " " + text
        self._space_next = False
        for start in range(0, len(text), CHUNK_MAX):
            piece = text[start : start + CHUNK_MAX]
            self.pieces.append(piece)
            if self._pending is not None:
                with self.c.transaction():
                    self._write(self._pending, final=False)
            self._pending = piece

    def flush(self) -> None:
        """The call failed: what was kept back goes out too, not final."""
        if self._pending is not None:
            with self.c.transaction():
                self._write(self._pending, final=False)
            self._pending = None

    def final(self) -> str:
        """Write the final chunk (inside the caller's transaction); return the whole reply."""
        reply = "".join(self.pieces)
        if not reply.strip():
            if self.queued:
                reply = BOARD_ACK_TEXT
            else:
                reply = ACTION_ONLY_TEXT if self.acted else NO_SPEECH_TEXT
            self._pending = reply if self._pending is None else self._pending
        assert self._pending is not None
        self._write(self._pending, final=True)
        self._pending = None
        return reply

    def say(self, text: str) -> None:
        """Jarvis's own words about a tool call, spoken as part of the reply."""
        if self.pieces and not self.pieces[-1][-1:].isspace():
            text = " " + text
        self.speech(text)
        self._space_next = True

    def action(self, *, target: UUID, person_id: UUID, kind: str, args: dict[str, Any],
               now: datetime) -> None:  # fmt: skip
        """A live ACTION chunk of this reply. The piece kept back keeps its place after it."""
        self.seq = actions.issue_live(
            self.c, reply_id=self.reply_id, session_id=self.session_id, target_node_id=target,
            person_id=person_id, action_kind=kind, args=args, now=now,
            in_reply_to=self.in_reply_to,
        )  # fmt: skip
        self.acted = True

    def _write(self, text: str, *, final: bool) -> None:
        self.seq += 1
        self.c.execute(
            "INSERT INTO conv.stream_chunk (reply_id, session_id, seq, type, text, final,"
            " in_reply_to) VALUES (%s, %s, %s, 'SPEECH', %s, %s, %s)",
            (self.reply_id, self.session_id, self.seq, text, final, self.in_reply_to),
        )
        self.c.execute("SELECT pg_notify('jarvis_stream', %s)", (str(self.reply_id),))


def _call(
    c: psycopg.Connection[Any],
    brain: Brain,
    req: BrainRequest,
    turn: _Inbound,
    stream: _Stream,
) -> CallPhase | None:
    """Run the call. None if it completed; else the phase it failed in (§10.1 step 4)."""
    received = False
    told: set[str] = set()  # what this reply has already said about refusals
    try:
        for event in brain.think(req):
            received = True
            if isinstance(event, Speech):
                if event.text:
                    stream.speech(event.text)
            else:
                _tool(c, turn, stream, event, prompt.memory_ids(req), told)
    except BrainCallFailed as failure:
        # Events from the provider prove a request was sent: never CONNECT after one.
        if failure.phase is CallPhase.CONNECT and not received:
            return CallPhase.CONNECT
        return CallPhase.SEND
    except Exception as exc:  # unplaceable: the send phase (fail closed)
        log.warning("turn %s: unexpected %s during the call", turn.id, type(exc).__name__)
        return CallPhase.SEND
    return None


def _tool(
    c: psycopg.Connection[Any],
    turn: _Inbound,
    stream: _Stream,
    call: ToolCall,
    memory_ids: list[str],
    told: set[str],
) -> None:
    """One tool call: map, decide (§9.2), then act or refuse (§9.1)."""
    kind = prompt.TOOL_ACTIONS.get(call.name)
    if kind is None:  # a wire name never reaches the policy as an action kind
        decision: policy.Decision = Refuse("unknown_action")
    else:
        decision = decide_tool_call(
            c, person_id=turn.person_id, session_id=stream.session_id, node_id=turn.node_id,
            node_kind=turn.node_kind, call=ToolCall(kind, call.args), memory_ids=memory_ids,
        )  # fmt: skip
    if isinstance(decision, Refuse):
        _refuse(c, turn, stream, told, decision.code, kind, REFUSED_TEXT)
        return
    assert kind is not None
    if isinstance(decision, NeedsApproval):
        _propose(c, turn, stream, call)
    elif decision.tier is Tier.REVERSIBLE:
        _remember(c, turn, stream, call)
    elif kind in _BOARD_KINDS:
        _board(c, turn, stream, kind, call, told)
    else:
        stream.action(
            target=turn.node_id, person_id=turn.person_id, kind=kind,
            args={"tool": call.name, "input": call.args}, now=_now(c),
        )  # fmt: skip


def _refuse(
    c: psycopg.Connection[Any],
    turn: _Inbound,
    stream: _Stream,
    told: set[str],
    code: str,
    kind: str | None,
    text: str,
) -> None:
    """A refused tool call: not executed, recorded (codes only), and `text` said once."""
    log.info("turn %s: tool call refused (%s)", turn.id, code)
    details = {"reason": code}
    if kind is not None:
        details["action_kind"] = kind
    with c.transaction():
        comm.record_event(
            c, "policy.refused", subject_type="inbound", subject_id=str(turn.id),
            person_id=turn.person_id, details=details,
        )  # fmt: skip
    if text not in told:
        told.add(text)
        stream.say(text)


_BOARD_KINDS = frozenset({"board.make", "board.set_state", "board.focus"})


def _board(
    c: psycopg.Connection[Any],
    turn: _Inbound,
    stream: _Stream,
    kind: str,
    call: ToolCall,
    told: set[str],
) -> None:
    """A Board tool call the policy allowed. Nothing in the call names a person: the panel is
    looked up among the authenticated person's own."""
    if kind == "board.make":
        request = call.args.get("request")
        if not isinstance(request, str) or not request.strip():
            _refuse(c, turn, stream, told, "bad_args", kind, BOARD_UNCLEAR_TEXT)
            return
        payload = {
            "session_id": str(stream.session_id),
            "person_id": str(turn.person_id),
            "node_id": str(turn.node_id),
            "inbound_id": str(turn.id),
            "request": request.strip()[: prompt.BOARD_REQUEST_MAX],
        }
        with c.transaction():  # one Board job per turn: the turn is the job's key
            queued = jobs.enqueue_once(c, "board.make", str(turn.id), payload)
        if queued is None:
            log.info("turn %s: Board work is already queued; a second request is left out", turn.id)
        stream.queued = True
        return
    try:
        panel_id = board.resolve(c, turn.person_id, call.args.get("panel"))  # type: ignore[arg-type]
    except board.UnknownPanel:
        _refuse(c, turn, stream, told, "unknown_panel", kind, NO_PANEL_TEXT)
        return
    args = {"panel_id": str(panel_id)}
    if kind == "board.focus":
        stream.action(
            target=turn.node_id, person_id=turn.person_id, kind=kind, args=args, now=_now(c)
        )
        return
    word = call.args.get("state")
    state = prompt.BOARD_STATES.get(word) if isinstance(word, str) else None
    if state is None:
        _refuse(c, turn, stream, told, "bad_args", kind, BOARD_UNCLEAR_TEXT)
        return
    with c.transaction():  # the record and the action to the desktop, together
        board.set_state(c, person_id=turn.person_id, panel_id=panel_id, state=state)
        stream.action(
            target=turn.node_id, person_id=turn.person_id, kind=kind,
            args={**args, "state": state}, now=_now(c),
        )  # fmt: skip


def _propose(c: psycopg.Connection[Any], turn: _Inbound, stream: _Stream, call: ToolCall) -> None:
    name = call.args.get("name")
    if not isinstance(name, str):
        stream.say(REFUSED_TEXT)
        return
    try:
        approval = approvals.propose_delete(
            c, session_id=stream.session_id, person_id=turn.person_id, node_id=turn.node_id,
            name=name, now=_now(c),
        )  # fmt: skip
    except approvals.ListingUnavailable:
        log.info("turn %s: no usable listing", turn.id)
        stream.say(NO_LISTING_TEXT)
        return
    if approval is None:
        stream.say(NOT_LISTED_TEXT.format(name=name if actions.valid_file_name(name) else "it"))
    else:
        stream.say(APPROVAL_TEXT)


def _remember(c: psycopg.Connection[Any], turn: _Inbound, stream: _Stream, call: ToolCall) -> None:
    """`memory.write`: by and about the authenticated person, never from the call (P2-S2)."""
    assert _memory is not None
    content = call.args.get("content")  # the only argument read: the tool takes no other
    if not isinstance(content, str) or not content.strip():
        stream.say(NOT_REMEMBERED_TEXT)
        return
    try:
        actions.memory_write(
            c, _memory, person_id=turn.person_id, inbound_id=turn.id, content=content.strip(),
            category=MEMORY_CATEGORY.value, topics=(),
        )  # fmt: skip
    except (MemoryEngineError, ValueError):
        log.info("turn %s: memory.write refused", turn.id)
        stream.say(NOT_REMEMBERED_TEXT)
        return
    stream.say(REMEMBERED_TEXT)


def _settle(
    c: psycopg.Connection[Any],
    reservation: UUID,
    *,
    turn: _Inbound,
    stream: _Stream,
    model: str,
    usage: tuple[int, int],
    told_held: bool,
    now: datetime,
) -> None:
    """Settle, the final chunk, the JARVIS message and ANSWERED, all or nothing. Raises
    `ReservationStateError` if the reservation is no longer ACTIVE (lapsed by the sweep)."""
    with c.transaction():
        c.execute("SELECT 1 FROM conv.inbound WHERE id = %s FOR UPDATE", (turn.id,))  # turn first
        budget.settle(
            c, reservation, actual_paise=prices.cost_paise(model, *usage), usage=usage, model=model
        )
        reply = stream.final()
        c.execute(
            "UPDATE conv.session SET last_activity_at = greatest(last_activity_at, %s)"
            " WHERE id = %s",
            (now, stream.session_id),
        )  # also the session row lock, for the message's seq
        _append(c, stream.session_id, "JARVIS", turn.person_id, None, reply, turn.id)
        c.execute(
            "UPDATE conv.inbound SET outcome = 'ANSWERED' WHERE id = %s AND outcome IS NULL",
            (turn.id,),
        )
        if told_held:  # R12-20: only for a person told about the hold
            event_id = comm.record_event(
                c, "turn.completed_after_hold", subject_type="inbound", subject_id=str(turn.id),
                person_id=turn.person_id, details={},
            )  # fmt: skip
            rules.notify(
                c, "turn.completed_after_hold", person_id=turn.person_id, event_id=event_id,
                context={"text": reply},
            )  # fmt: skip


# --- the job ---------------------------------------------------------------------------------


@dispatch.register("conv.turn")
def run(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    """Handler of `conv.turn`, payload `{inbound_id, [hold, held, told_held, cap_held_since]}`."""
    turn = _read(conn, UUID(job.payload["inbound_id"]))
    if turn is None:  # pruned (30 days): nothing to answer
        log.info("turn job %s: no turn %s", job.id, job.payload["inbound_id"])
        return
    if turn.outcome is not None:
        return
    if turn.kind != "utterance":
        raise ValueError(f"turn {turn.id} is a {turn.kind}, not an utterance")
    engine = _memory
    if engine is None:
        raise NoMemory("conv.turn ran with no memory engine")
    text, screen = _utterance(turn.payload)
    now = _now(conn)
    with budget.committing_connection(conn) as c:
        if not text.strip():
            _finish(c, turn, "REFUSED")
            return
        try:
            identity.memory_slug(engine, turn.person_id)
        except identity.UnknownPerson:
            log.warning("turn %s: person %s is not in memory", turn.id, turn.person_id)
            _finish(c, turn, "REFUSED")
            return
        session_id = _person_message(c, turn, text, now)
        if session_id is None:
            return
        if (
            job.payload.get("hold") == "brain"
            and now - turn.received_at >= DOWN_HOLD
            and _expire_unreached(c, turn, "turn.down_expired")  # R12-18: never sent to Claude
        ):
            return
        # A held turn that has already reached Claude goes on to `reserve`: the step-6 table.
        if health.is_down(conn):  # held before any reservation (§10.2)
            told = _tell_down(conn, turn, held=True)
            _hold(conn, job, "brain", BRAIN_RETRY, told=told)
            return
        if _brain is None:
            raise NoTurnBrain("conv.turn ran with no brain configured")
        brain, model = _brain()
        req = prompt.build(
            conn, engine, session_id=session_id, person_id=turn.person_id, utterance=text,
            screen_summary=screen, inbound_id=turn.id,
        )  # fmt: skip
        if not prompt.is_valid(req):
            _finish(c, turn, "REFUSED")
            return
        try:
            ceiling = prices.cost_paise(model, count_input_tokens(req), req.max_tokens)
        except prices.UnknownModelPrice:
            log.error("turn %s: the configured model has no price; not calling", turn.id)
            _finish(
                c, turn, "REFUSED", "brain.config_error", {"code": "unknown_model_price"},
                recipient=config.OWNER_PERSON_ID,  # R12-19
            )  # fmt: skip
            return
        db_now, lease_until = _lease(conn, job.id)
        owner, reservation = budget.reserve(
            c, job_id=job.id, turn_id=turn.id, reserve_paise=ceiling, lease_until=lease_until,
            now=db_now,
        )  # fmt: skip
        if owner is budget.Owner.NO_BUDGET:
            _cap_hold(conn, c, job, turn, now, db_now, ceiling)
            return
        if owner is not budget.Owner.RESERVED:  # the step-6 table: never a second call
            log.info("turn job %s: turn %s %s; no call", job.id, turn.id, owner)
            return
        assert reservation is not None

        stream = _Stream(c, session_id, turn.id)
        phase = _call(c, brain, req, turn, stream)
        usage = brain.usage
        if phase is None and usage is None:  # completed but unmetered: fail closed (carry-over c)
            phase = CallPhase.SEND
        if phase is CallPhase.CONNECT:  # nothing reached Claude: give it back, try again later
            try:
                budget.release(c, reservation)
            except budget.ReservationStateError:  # outlived its lease: the sweep lapsed it
                log.warning("turn %s: reservation no longer active at release", turn.id)
                health.record_failure(conn)
                return  # indeterminate already; never re-queued for another call
            health.record_failure(conn)
            # Told at once whenever the brain is DOWN, not only when this failure flipped it.
            told = health.is_down(conn) and _tell_down(conn, turn, held=True)
            _hold(conn, job, "brain", BRAIN_RETRY, told=told)
            return
        if phase is CallPhase.SEND:  # indeterminate: never called again (§10.1 step 6)
            budget.lapse(c, reservation)  # first: the indeterminate path, notified once (Task 9)
            stream.flush()
            if health.record_failure(conn):
                _tell_down(conn, turn, held=False)
            return
        assert usage is not None
        try:
            _settle(
                c, reservation, turn=turn, stream=stream, model=model, usage=usage,
                told_held=bool(job.payload.get("told_held")), now=now,
            )  # fmt: skip
        except budget.ReservationStateError:  # the call outlived its lease: already lapsed
            log.warning("turn %s: reservation no longer active at settle", turn.id)
            stream.flush()
        if health.record_success(conn):
            health.came_back(conn)


def _cap_hold(
    conn: psycopg.Connection[Any],
    c: psycopg.Connection[Any],
    job: jobs.Job,
    turn: _Inbound,
    now: datetime,
    db_now: datetime,
    ceiling: int,
) -> None:
    """NO_BUDGET: hold, retrying every `CAP_RETRY`; after `CAP_HOLD` expire the turn."""
    since_raw = job.payload.get("cap_held_since")
    since = datetime.fromisoformat(since_raw) if isinstance(since_raw, str) else now
    if now - since >= CAP_HOLD:
        reason = _cap_reason(conn, db_now, ceiling)
        # A duplicate job may have reserved since our NO_BUDGET: then it owns the turn.
        _expire_unreached(
            c, turn, "turn.cap_expired", {"reason": reason}, {"text": CAP_EXPIRED_TEXT[reason]}
        )
        return
    _hold(conn, job, "cap", CAP_RETRY, cap_held_since=since.isoformat())
