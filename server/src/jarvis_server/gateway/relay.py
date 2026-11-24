"""Delivery to connected nodes: the stream relay, outbox delivery, the re-send sweep, the
heartbeat and the `welcome` contents (spec §6.2, §8.2, §8.3; R8-1, R9-1, R12-8, R12-9).

**One `Relay` per gateway process.** It keeps the registry of live connections (one `NodeLink`
per node: a newer connection of the same node replaces and stops the older one, whose row
`gw.open_connection` has already closed as `superseded`), LISTENs on `jarvis_stream` and
`jarvis_outbox`, and runs a ticker for the heartbeat and the 60 s re-send sweep. Time comes from
the injected clock (`now()`), never from sleeps.

**Nothing shared waits on a socket (fix round 1, I-1).** Each link has its own queue and sender
task; the listener, fan-out, ticker and sweep only enqueue. Every send is bounded by the send
timeout; one that exceeds it closes that connection as `send_timeout`.

**Every frame follows a fenced database call that succeeded (R8-1).** An outbox item is written
only after `comm.record_sent` returned true in its own committed transaction; chunks, actions
and `outbox.cleared` only after `gw.current_node`. `JF001` stops the connection without writing;
any other database failure sends `error server_error` and closes it through
`gw.close_connection(conn_id, 'server_error')`.

**The stream.** Chunks are read from `conv.stream_chunk`, never from memory: a node gets the
chunks after `conv.stream_cursor.acked_seq` for `(reply, node)`. A reply's recipients are the
connected nodes of the person whose session it is (`stream_chunk.session_id` →
`conv.session.person_id`, R12-9). The in-memory "sent up to" mark only avoids re-sending on
every wake-up; correctness rests on the cursor. A SPEECH chunk is a `reply.chunk`; a live ACTION
chunk is a `node.action {authz}` to its `target_node_id` only, while `now() <= valid_until`, and
is never re-sent on (re)connect (§6.2 step 8).

**Reconnect (R9-1).** Candidate replies are those of the node's own person's sessions with a
chunk in the last 10 minutes, chosen from the chunks and sessions, never from cursor rows. Only
the first 64 `resume` entries are considered. An entry for a reply that isn't a candidate, or
whose `last_seq` is above the reply's highest stored `seq`, is ignored and goes to
`conv.record_resume_out_of_range`, which decides whether to record `node.resume_out_of_range`.
Otherwise a candidate restarts at `max(acked_seq, max(last_seq, 0)) + 1`.

**The outbox.** An open item goes to every connected node of its person that hasn't been sent it
(on its NOTIFY and on connect); an item with a `target_node_id` goes to that node only, and only
that node is re-sent it or sent its `outbox.cleared` (R12-23). A `keys.keyset` item is sent as
a `keyset` message carrying its unsigned `outbox_id` and the announcement from its signed
`authz` (R12-26); pending keysets go first, before any other item, chunk or action (Task 12
ruling 5). The sweep re-sends, with the same ID, items a connected node was sent more than 2
minutes ago and hasn't confirmed (`received`).
A terminal item's NOTIFY sends `outbox.cleared` with the state as reason to the connected nodes
that were sent it, other than a node that acknowledged it here.

Nothing here logs content.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, LiteralString, Protocol
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from jarvis_server.comm import OutboxState
from jarvis_server.contracts import MessageKind, OutboxRef, ReplyPosition, ResumePosition
from jarvis_server.core import summary
from jarvis_server.dbcall import FENCED_SQLSTATE, OutboxRefused, audited_call_async

log = logging.getLogger(__name__)

TICK_S = 1.0  # real seconds between ticker checks; the decisions use the injected clock
RESEND_SWEEP = timedelta(seconds=60)
RESEND_AFTER = timedelta(minutes=2)
HEARTBEAT = timedelta(seconds=30)
MISSED_HEARTBEATS = 3
RESUME_WINDOW = timedelta(minutes=10)
RESUME_LIMIT = 64
MAX_BODY = 4000
SERVER_ERROR = "server_error"
HEARTBEAT_REASON = "heartbeat"
SEND_TIMEOUT = "send_timeout"
SEND_TIMEOUT_S = 10.0  # real seconds one socket send may take (fix round 1, I-1)
KEYSET_KIND = "keys.keyset"  # core.keys.KEYSET_KIND: sent as a `keyset` message
KEY_ROTATED = "key_rotated"  # the close reason of a connection on a replaced gateway key

Pool = AsyncConnectionPool[psycopg.AsyncConnection[Any]]


# ------------------------------------------------------------------ typed boundary


def _outbox_result(text: Any) -> OutboxState:
    if text == "unknown_item":
        raise OutboxRefused("unknown_item")
    return OutboxState(text)


async def record_received(
    conn: psycopg.AsyncConnection[Any], conn_id: int, outbox_id: UUID
) -> OutboxState:
    """`comm.record_received`, committed; the state afterwards, or `OutboxRefused`."""
    (result,) = await audited_call_async(
        conn, "SELECT comm.record_received(%s, %s)", (conn_id, outbox_id)
    )
    return _outbox_result(result)


async def acknowledge(
    conn: psycopg.AsyncConnection[Any], conn_id: int, outbox_id: UUID
) -> OutboxState:
    """`comm.acknowledge`, committed; the state afterwards, or `OutboxRefused`."""
    (result,) = await audited_call_async(
        conn, "SELECT comm.acknowledge(%s, %s)", (conn_id, outbox_id)
    )
    return _outbox_result(result)


# ------------------------------------------------------------------ welcome


async def welcome(pool: Pool, person_id: UUID) -> dict[str, Any]:
    """`{session_id, summary}`: the person's open session, if any, and the reconnect summary
    (§8.3) as the message schema's groups."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM conv.session WHERE person_id = %s AND closed_at IS NULL", (person_id,)
        )
        row = await cur.fetchone()
        cur = await conn.execute(summary.QUERY, (person_id,))
        rows = await cur.fetchall()
    return {
        "session_id": None if row is None else str(row[0]),
        "summary": summary_groups(summary.build_summary(rows)),
    }


def summary_groups(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each reconnect-summary entry as a `SummaryGroup {priority, group_key, outbox_ids, line}`,
    in order (controller ruling G2): URGENT and DIGEST keep their line; a TODAY group of one
    keeps its line, a larger one gets the digest template on its oldest item's line."""
    groups = []
    for entry in entries:
        if entry["priority"] == "TODAY":
            lines = entry["lines"]
            line = (
                lines[0]
                if len(lines) == 1
                else summary.DIGEST_LINE.format(label=lines[0], count=len(lines))
            )
        else:
            line = entry["text"]
        groups.append(
            {
                "priority": entry["priority"],
                "group_key": entry["group_key"],
                "outbox_ids": [str(i) for i in entry["outbox_ids"]],
                "line": line,
            }
        )
    return groups


# ------------------------------------------------------------------ keyset


def keyset_payload(outbox_id: UUID, authz: dict[str, Any]) -> dict[str, Any]:
    """The `keyset` message for a `keys.keyset` item: its unsigned `outbox_id`, and the role,
    key_id and public key copied from the signed `authz.object.args`, then the authz itself
    (R12-26). The node accepts it only if the copies equal the signed args."""
    args = authz["object"]["args"]
    return {
        "outbox_id": str(outbox_id),
        "role": args["role"],
        "key_id": args["key_id"],
        "public_key": args["public_key"],
        "authz": authz,
    }


# ------------------------------------------------------------------ connections


class Endpoint(Protocol):
    """What the relay needs from a connection (`gateway.session.Session`)."""

    async def send(self, kind: MessageKind, payload: dict[str, Any]) -> None: ...

    def stop(self, db_reason: str | None, socket_reason: str = "") -> None: ...


class SendTimeout(Exception):
    """A socket send didn't finish within the send timeout: the peer stopped reading."""


Work = Callable[[], Awaitable[None]]


class _Stopped(Exception):
    """The connection was stopped while delivering; the delivery ends here."""


def reap(task: asyncio.Task[Any]) -> None:
    """Cancel `task` if it is still running, and retrieve its outcome when it ends, so no task
    is left pending at shutdown and no exception goes unretrieved."""
    task.cancel()
    task.add_done_callback(_retrieve)


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _fenced(e: psycopg.Error) -> bool:
    return e.sqlstate == FENCED_SQLSTATE


# Open items never sent to this node (on connect, and on an item's NOTIFY).
_UNSENT: LiteralString = """
SELECT o.id, o.priority, o.kind, o.body, o.expires_at, o.authz FROM comm.outbox o
LEFT JOIN comm.delivery d ON d.outbox_id = o.id AND d.node_id = %(node)s
WHERE o.person_id = %(person)s AND o.state IN ('PENDING', 'DELIVERING', 'DELIVERED')
  AND (o.target_node_id IS NULL OR o.target_node_id = %(node)s)
  AND coalesce(d.attempts, 0) = 0
  AND (%(item)s::uuid IS NULL OR o.id = %(item)s::uuid)
ORDER BY (o.kind = 'keys.keyset') DESC, o.created_at, o.id
"""
# The sweep: those, and items sent to this node before %(before)s that it hasn't confirmed.
_DUE: LiteralString = """
SELECT o.id, o.priority, o.kind, o.body, o.expires_at, o.authz FROM comm.outbox o
LEFT JOIN comm.delivery d ON d.outbox_id = o.id AND d.node_id = %(node)s
WHERE o.person_id = %(person)s AND o.state IN ('PENDING', 'DELIVERING', 'DELIVERED')
  AND (o.target_node_id IS NULL OR o.target_node_id = %(node)s)
  AND (coalesce(d.attempts, 0) = 0
       OR (d.received_at IS NULL AND d.last_sent_at < %(before)s))
ORDER BY (o.kind = 'keys.keyset') DESC, o.created_at, o.id
"""
_CHUNKS: LiteralString = """
SELECT seq, type, text, authz, final, target_node_id, valid_until FROM conv.stream_chunk
WHERE reply_id = %s AND seq >= %s ORDER BY seq
"""


class NodeLink:
    """The relay's side of one established connection: its own outbound queue and sender task,
    its delivery lock (one delivery at a time, in order), the in-memory stream mark and the
    heartbeat.

    **Nothing shared waits on this socket (fix round 1, I-1).** The listener, the fan-out, the
    ticker and the sweep only enqueue work; this link's sender task runs it, each fenced call
    immediately before its frame. A send that exceeds the send timeout stops the link, which
    closes the connection as `send_timeout`, and never holds up another node."""

    def __init__(
        self,
        relay: Relay,
        endpoint: Endpoint,
        *,
        node_id: UUID,
        person_id: UUID,
        conn_id: int,
        connected_at: datetime,
        gw_key_id: str = "",
    ) -> None:
        self.relay = relay
        self.gw_key_id = gw_key_id  # the gateway key this connection signs under (R12-6)
        self.endpoint = endpoint
        self.node_id = node_id
        self.person_id = person_id
        self.conn_id = conn_id
        self._lock = asyncio.Lock()
        self._sent_upto: dict[UUID, int] = {}  # an optimisation only (R9-1: the cursor decides)
        self._acked_here: set[UUID] = set()
        self._next_ping = connected_at + HEARTBEAT
        self._unanswered = 0
        self.stopped = False
        self._queue: asyncio.Queue[Work] = asyncio.Queue()
        self._sender = asyncio.create_task(self._send_loop())

    async def _send_loop(self) -> None:
        while True:
            work = await self._queue.get()
            try:
                await self._guarded(work)
            finally:
                self._queue.task_done()

    def _enqueue(self, work: Work) -> None:
        if not self.stopped:
            self._queue.put_nowait(work)

    async def idle(self) -> None:
        """Until everything queued so far has run (tests, and `Relay.sweep(wait=True)`)."""
        await self._queue.join()

    async def close(self) -> None:
        """Stop the sender task and drop the queue; works while a send is blocked."""
        self.stopped = True
        reap(self._sender)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        with contextlib.suppress(asyncio.CancelledError):
            await self._sender

    # ---------------------------------------------------------------- failures

    async def _guarded(self, work: Work) -> None:
        """Run one relay-initiated delivery; a fence or database failure stops the link."""
        if self.stopped:
            return
        try:
            async with self._lock:
                if not self.stopped:
                    await work()
        except _Stopped:
            pass
        except SendTimeout:
            log.info("connection %s: send timed out", self.conn_id)
            self._stop(SEND_TIMEOUT)
        except psycopg.Error as e:
            if _fenced(e):
                log.info("connection %s fenced during delivery", self.conn_id)
                self._stop(None)
            else:
                log.warning(
                    "database error delivering to connection %s (%s)", self.conn_id, e.sqlstate
                )
                with contextlib.suppress(Exception):
                    await self.endpoint.send(
                        MessageKind.ERROR,
                        {"code": SERVER_ERROR, "detail": "the server could not deliver"},
                    )
                self._stop(SERVER_ERROR)
        except Exception:
            # The socket went away mid-send: the session's own receive loop sees the
            # disconnect and closes the connection row (`node_closed`).
            log.info("delivery to connection %s ended: the socket is gone", self.conn_id)

    def _stop(self, db_reason: str | None, socket_reason: str = "") -> None:
        self.stopped = True
        self.endpoint.stop(db_reason, socket_reason)

    async def _fence(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """`gw.current_node` before a frame that has no record of its own."""
        await conn.execute("SELECT gw.current_node(%s)", (self.conn_id,))

    # ---------------------------------------------------------------- outbox

    def _offer_work(self, outbox_id: UUID | None) -> Work:
        """The open items this node hasn't been sent (one item, or all of them)."""

        async def work() -> None:
            rows = await self._query(
                _UNSENT, {"node": self.node_id, "person": self.person_id, "item": outbox_id}
            )
            await self._deliver_items(rows)

        return work

    def offer(self, outbox_id: UUID | None = None) -> None:
        self._enqueue(self._offer_work(outbox_id))

    def resend_due(self, now: datetime) -> None:
        async def work() -> None:
            rows = await self._query(
                _DUE,
                {"node": self.node_id, "person": self.person_id, "before": now - RESEND_AFTER},
            )
            await self._deliver_items(rows)

        self._enqueue(work)

    async def _query(self, sql: LiteralString, params: dict[str, Any]) -> list[Any]:
        async with self.relay.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _deliver_items(self, rows: Sequence[Sequence[Any]]) -> None:
        for outbox_id, priority, kind, body, expires_at, authz in rows:
            if expires_at is not None and self.relay.now() > expires_at:
                continue  # the gateway enforces expiry too; core's job expires it
            async with self.relay.pool.connection() as conn:
                (sent,) = await audited_call_async(
                    conn, "SELECT comm.record_sent(%s, %s)", (self.conn_id, outbox_id)
                )
            if not sent:  # terminal by now, or refused: "don't send"
                continue
            if kind == KEYSET_KIND:
                await self.endpoint.send(MessageKind.KEYSET, keyset_payload(outbox_id, authz))
                continue
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text:
                text = summary.LINE.get(kind, summary.GENERIC_LINE)
            await self.endpoint.send(
                MessageKind.OUTBOX_ITEM,
                {
                    "outbox_id": str(outbox_id),
                    "priority": priority,
                    "kind": kind,
                    "body": text[:MAX_BODY],
                    "expires_at": None if expires_at is None else expires_at.isoformat(),
                    "authz": authz,
                },
            )

    def clear(self, outbox_id: UUID, reason: str) -> None:
        """`outbox.cleared` if this node was sent the item and didn't acknowledge it here."""

        async def work() -> None:
            if outbox_id in self._acked_here:
                return
            async with self.relay.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM comm.delivery WHERE outbox_id = %s AND node_id = %s"
                    " AND attempts > 0",
                    (outbox_id, self.node_id),
                )
                if await cur.fetchone() is None:
                    return
                await self._fence(conn)
            await self.endpoint.send(
                MessageKind.OUTBOX_CLEARED, {"outbox_id": str(outbox_id), "reason": reason}
            )

        self._enqueue(work)

    # ---------------------------------------------------------------- stream

    def relay_reply(self, reply_id: UUID) -> None:
        """A live wake-up: the chunks after the cursor (and after what this link sent)."""

        async def work() -> None:
            await self._send_reply(reply_id, self._sent_upto.get(reply_id, 0), live=True)

        self._enqueue(work)

    async def _send_reply(self, reply_id: UUID, after: int, *, live: bool) -> None:
        """Chunks with `seq > max(acked_seq, after)`; ACTION chunks only when `live`."""
        now = self.relay.now()
        async with self.relay.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT acked_seq FROM conv.stream_cursor WHERE reply_id = %s AND node_id = %s",
                (reply_id, self.node_id),
            )
            row = await cur.fetchone()
            start = max(0 if row is None else int(row[0]), after) + 1
            cur = await conn.execute(_CHUNKS, (reply_id, start))
            chunks = await cur.fetchall()
            frames: list[tuple[MessageKind, dict[str, Any]]] = []
            for seq, kind, text, authz, final, target, valid_until in chunks:
                if kind == "SPEECH":
                    payload = {
                        "reply_id": str(reply_id),
                        "seq": seq,
                        "type": "SPEECH",
                        "text": text,
                        "authz": None,
                        "final": final,
                    }
                    frames.append((MessageKind.REPLY_CHUNK, payload))
                elif live and target == self.node_id and now <= valid_until:
                    frames.append((MessageKind.NODE_ACTION, {"authz": authz}))
            if frames:
                await self._fence(conn)
        if chunks:
            self._sent_upto[reply_id] = max(self._sent_upto.get(reply_id, 0), chunks[-1][0])
        for kind, payload in frames:
            await self.endpoint.send(kind, payload)

    async def on_connect(self, resume: Sequence[ResumePosition]) -> None:
        """After `welcome`, in the connection's own task: the items not yet sent to this node,
        then each candidate reply's unacknowledged chunks (R9-1, R12-8)."""
        await self._guarded(self._offer_work(None))
        await self._guarded(self._resume_work(resume))

    def resync(self) -> None:
        """The same, queued: after the listener re-LISTENs (anything missed meanwhile)."""
        self._enqueue(self._offer_work(None))
        self._enqueue(self._resume_work(()))

    def _resume_work(self, resume: Sequence[ResumePosition]) -> Work:
        async def work() -> None:
            async with self.relay.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT k.reply_id, max(k.seq) FROM conv.stream_chunk k"
                    " JOIN conv.session s ON s.id = k.session_id"
                    " WHERE s.person_id = %s GROUP BY k.reply_id"
                    " HAVING max(k.created_at) > %s ORDER BY min(k.created_at), k.reply_id",
                    (self.person_id, self.relay.now() - RESUME_WINDOW),
                )
                candidates = {UUID(str(r)): int(m) for r, m in await cur.fetchall()}
            positions: dict[UUID, int] = {}
            for entry in resume[:RESUME_LIMIT]:
                if entry.reply_id in positions:
                    continue
                highest = candidates.get(entry.reply_id)
                if highest is None or entry.last_seq > highest:
                    async with self.relay.pool.connection() as conn:
                        await audited_call_async(
                            conn,
                            "SELECT conv.record_resume_out_of_range(%s, %s, %s::numeric)",
                            (self.conn_id, entry.reply_id, entry.last_seq),
                        )
                    continue  # ignored: the cursor alone decides
                positions[entry.reply_id] = max(entry.last_seq, 0)
            for reply_id in candidates:
                await self._send_reply(reply_id, positions.get(reply_id, 0), live=False)

        return work

    # ---------------------------------------------------------------- from the node

    async def on_confirm(self, kind: MessageKind, ref: OutboxRef | ReplyPosition) -> str | None:
        """A node's `received` or `ack`; returns a refusal code, if any. Database errors,
        including JF001, propagate to the session, which closes the connection."""
        async with self.relay.pool.connection() as conn:
            if isinstance(ref, OutboxRef):
                if kind is MessageKind.ACK:
                    self._acked_here.add(ref.outbox_id)  # before its NOTIFY can arrive
                try:
                    if kind is MessageKind.ACK:
                        await acknowledge(conn, self.conn_id, ref.outbox_id)
                    else:
                        await record_received(conn, self.conn_id, ref.outbox_id)
                except OutboxRefused as r:
                    return r.reason
                return None
            _, refusal = await audited_call_async(
                conn,
                "SELECT recorded, refusal FROM conv.record_chunk_received(%s, %s, %s)",
                (self.conn_id, ref.reply_id, ref.seq),
            )
            return None if refusal is None else str(refusal)

    def on_pong(self) -> None:
        self._unanswered = 0

    def heartbeat(self, now: datetime) -> None:
        """A `ping` every 30 s (queued); three left unanswered close the connection
        (`heartbeat`). The decision never waits on the socket."""
        if self.stopped or now < self._next_ping:
            return
        if self._unanswered >= MISSED_HEARTBEATS:
            log.info("connection %s missed %d heartbeats", self.conn_id, self._unanswered)
            self._stop(HEARTBEAT_REASON)
            return
        self._unanswered += 1
        self._next_ping = now + HEARTBEAT

        async def ping() -> None:
            await self.endpoint.send(MessageKind.PING, {})

        self._enqueue(ping)


# ------------------------------------------------------------------ the process-wide relay


class Relay:
    def __init__(
        self,
        *,
        pool: Pool,
        dsn: str,
        clock: Callable[[], datetime],
        tick_interval: float = TICK_S,
    ) -> None:
        self.pool = pool
        self.now = clock
        self._dsn = dsn
        self._tick_interval = tick_interval
        self._links: dict[UUID, NodeLink] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._listening = asyncio.Event()
        self._next_sweep: datetime | None = None

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._tick())]
        await asyncio.wait_for(self._listening.wait(), 30)

    async def stop(self) -> None:
        """Shutdown: the listener, the ticker and every link's sender task, all awaited."""
        for task in self._tasks:
            reap(task)
        for link in list(self._links.values()):
            await link.close()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---------------------------------------------------------------- registry

    def register(self, link: NodeLink) -> None:
        """The node's newest connection; an older one is stopped (`superseded`). Its row was
        closed by gw.open_connection, so nothing more is written for it."""
        old = self._links.get(link.node_id)
        self._links[link.node_id] = link
        if old is not None and old is not link:
            old._stop(None, "superseded")

    def unregister(self, link: NodeLink) -> None:
        if self._links.get(link.node_id) is link:
            del self._links[link.node_id]

    def close_rotated(self, active_gw_key_id: str) -> int:
        """Stop every connection that signs under a gateway key other than the ACTIVE one
        (`key_rotated`), so it reconnects under a challenge with the new key (Task 12 fix
        round 1, I-1). Returns how many were stopped."""
        stale = [k for k in self._links.values() if k.gw_key_id != active_gw_key_id]
        for link in stale:
            link._stop(KEY_ROTATED, KEY_ROTATED)
        return len(stale)

    def _of(self, person_id: UUID) -> list[NodeLink]:
        return [k for k in self._links.values() if k.person_id == person_id]

    # ---------------------------------------------------------------- notifications

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True
                ) as conn:
                    await conn.execute("LISTEN jarvis_stream")
                    await conn.execute("LISTEN jarvis_outbox")
                    if self._listening.is_set():  # a re-LISTEN: catch up on what was missed
                        await self._catch_up()
                    self._listening.set()
                    async for note in conn.notifies():
                        await self._on_notify(note.channel, note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("notification listener failed; reconnecting", exc_info=True)
                await asyncio.sleep(1.0)

    async def _catch_up(self) -> None:
        for link in list(self._links.values()):
            link.resync()

    async def _on_notify(self, channel: str, payload: str) -> None:
        try:
            key = UUID(payload)
        except ValueError:
            return
        try:
            if channel == "jarvis_stream":
                await self._on_stream(key)
            elif channel == "jarvis_outbox":
                await self._on_outbox(key)
        except psycopg.Error as e:
            log.warning("could not handle a %s notification (%s)", channel, e.sqlstate)

    async def _on_stream(self, reply_id: UUID) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT s.person_id FROM conv.stream_chunk k"
                " JOIN conv.session s ON s.id = k.session_id WHERE k.reply_id = %s LIMIT 1",
                (reply_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return
        for link in self._of(UUID(str(row[0]))):
            link.relay_reply(reply_id)

    async def _on_outbox(self, outbox_id: UUID) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT person_id, state, target_node_id FROM comm.outbox WHERE id = %s",
                (outbox_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return
        person_id, state, target = UUID(str(row[0])), str(row[1]), row[2]
        for link in self._of(person_id):
            if target is not None and link.node_id != target:
                continue  # an item for one node only (R12-23)
            if state in ("PENDING", "DELIVERING", "DELIVERED"):
                link.offer(outbox_id)
            else:
                link.clear(outbox_id, state)

    # ---------------------------------------------------------------- the ticker

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            try:
                now = self.now()
                for link in list(self._links.values()):
                    link.heartbeat(now)
                if self._next_sweep is None:
                    self._next_sweep = now + RESEND_SWEEP
                elif now >= self._next_sweep:
                    self._next_sweep = now + RESEND_SWEEP
                    await self.sweep()
            except Exception:
                log.warning("gateway ticker failed", exc_info=True)

    async def sweep(self, *, wait: bool = False) -> None:
        """Re-send, with the same ID, what a connected node hasn't confirmed (§8.2): queued on
        each link. `wait` (tests) returns once every link has run its queue."""
        now = self.now()
        links = list(self._links.values())
        for link in links:
            link.resend_due(now)
        if wait:
            await asyncio.gather(*(link.idle() for link in links))
