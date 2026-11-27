"""One node connection: handshake, inbound recording, delivery and the revocation re-check
(§6.1, §6.2, §7.1, §8.2).

**Handshake.** After `whois` (an unknown peer is refused `unknown_peer`), the gateway sends a
signed `challenge` before reading anything, then checks the node's single reply: first message
with `seq` 1, this connection's nonce (constant time), before `expires_at`. Then one of two paths:

- **A, `hello`:** a registered node, its registered key, not revoked, and the connection's
  `whois` identity equal to the node row's. Then `gw.open_connection`.
- **B, `enrol` (nil node ID):** signed by the key it carries, then `gw.enrol_node` with this
  connection's `whois` identity (never the message's), then `gw.open_connection`, and `welcome`
  carries the `enrolment` block.

A STALE node (`gw.node.key_state`, Task 12) whose `hello` passes path A is refused
`reenrol_required` and closed with 4409 instead of `welcome`: after the challenge it is sent
nothing (R6-6). Every other refusal closes with 4401. Each is audited once: through
`gw.refuse_handshake` for the gateway's own codes, or by `gw.enrol_node` / `gw.open_connection`
for theirs (R12-1). The refusal commits before the socket closes. The nonce lives only in this
object and is consumed by the first reply, whatever its outcome.

**After the handshake.** A frame over 65,536 bytes gets `error too_large`, one that isn't an
envelope gets `error malformed`; neither carries a `seq` the gateway counts. An envelope from
another node, with a bad `node-msg` signature or a `seq` that isn't the previous + 1 closes the
connection through `gw.close_connection(conn_id, code)`, which writes its DISCONNECT row.
`utterance`, `approval.decision`, `action.receipt` and `board.state` go to
`conv.record_inbound`, with the
node's complete signed envelope, which core re-verifies before an irreversible step (R12-34);
`ping` to
`gw.touch_connection` and `pong`; `received` and `ack` to `comm.record_received`,
`comm.acknowledge` or `conv.record_chunk_received` (a refusal is sent back as `error`); `pong`
answers the gateway's heartbeat. Any other database error on an established connection sends
`error server_error` and closes through `gw.close_connection(conn_id, 'server_error')`.

**Delivery** (`relay`): `welcome` carries the open session and the reconnect summary. Then the
connection registers with the process's `Relay`, which stops an older connection of the same
node (`superseded`), and gets its pending items and unacknowledged chunks. The relay can stop
the connection (`stop`): fenced, superseded, a database failure or three missed heartbeats.

**Keys.** A connection keeps the gateway keys it started with (`ctx.keys` at the time), so
every envelope is signed under the key its challenge announced (R12-6). When a re-read finds a
new ACTIVE gateway key, the relay closes the connections still on the old key (`key_rotated`,
through `gw.close_connection`), and the node reconnects under a challenge with the new key
(fix round 1, I-1).

**Sends are bounded** by `send_timeout` (10 s in service): a send that doesn't finish in time
closes the connection through `gw.close_connection(conn_id, 'send_timeout')`.

**Fencing.** Every connection-scoped database call relies on the database fence: SQLSTATE
`JF001` closes the socket. A re-check every `recheck_interval` seconds (60 in service) calls
`gw.current_node`, so a revoked or superseded connection is closed within one interval.

Nothing here logs message content.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from jarvis_server import signing
from jarvis_server.contracts import (
    MESSAGE_MODELS,
    NIL_UUID,
    Ack,
    Enrol,
    Hello,
    MessageKind,
    NodeEnvelope,
    Received,
    ResumePosition,
)
from jarvis_server.dbcall import FENCED_SQLSTATE, audited_call_async
from jarvis_server.gateway import relay
from jarvis_server.gateway.keys import GatewayKeys, node_key_id
from jarvis_server.gateway.relay import SEND_TIMEOUT_S
from jarvis_server.gateway.tailscale import WhoIs, is_tailnet_address

log = logging.getLogger(__name__)

MAX_FRAME = 65_536
CHALLENGE_TTL = timedelta(seconds=30)
HANDSHAKE_TIMEOUT_S = 30.0  # real time: a peer that never replies is closed
SKEW_TOLERANCE_MS = 30_000
SKEW_CLAMP_MS = 10**12
TOKEN_LENGTH = 26  # the 128-bit token, base32 without padding, as gw.enrol_node requires

# The gw.close_connection reason for a database failure other than the fence (JF001).
SERVER_ERROR = "server_error"

CLOSE_REFUSED = 4401
CLOSE_REENROL = 4409

INBOUND_KINDS = frozenset(
    {
        MessageKind.UTTERANCE,
        MessageKind.APPROVAL_DECISION,
        MessageKind.ACTION_RECEIPT,
        MessageKind.BOARD_STATE,  # the person changed a panel on the Board by hand
    }
)
NODE_KINDS = INBOUND_KINDS | {
    MessageKind.RECEIVED,
    MessageKind.ACK,
    MessageKind.PING,
    MessageKind.PONG,
}


@dataclass
class GatewayContext:
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]]
    whois: WhoIs
    keys: GatewayKeys
    clock: Callable[[], datetime]
    recheck_interval: float
    bind_check: bool
    relay: relay.Relay
    send_timeout: float = SEND_TIMEOUT_S


class HandshakeRefused(Exception):
    """The handshake failed with `code`. `audited` when the SQL function that decided it has
    already written the audit row (gw.enrol_node, gw.open_connection)."""

    def __init__(self, code: str, node_id: UUID | None = None, *, audited: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.node_id = node_id
        self.audited = audited


class CloseConnection(Exception):
    """A protocol error on an established connection; `reason` goes to gw.close_connection."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Fenced(Exception):
    """The database fenced this connection (JF001): revoked, superseded or closed."""


class _Malformed(Exception):
    pass


def _fenced(e: psycopg.Error) -> bool:
    return e.sqlstate == FENCED_SQLSTATE


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _Malformed("duplicate key")
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise _Malformed(f"non-finite number {name}")


def parse_envelope(text: str) -> tuple[dict[str, Any], NodeEnvelope]:
    """The raw envelope (what the signature covers) and its validated model, or `_Malformed`.

    Duplicate keys and non-finite numbers are refused, so the signed object and the validated
    one can never differ. The envelope is validated strictly (no coercion: `sent_at` must be an
    RFC 3339 string with an offset).
    """
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
        if not isinstance(raw, dict):
            raise _Malformed("not an object")
        env = NodeEnvelope.model_validate_json(text, strict=True)
    except (ValueError, ValidationError) as e:
        raise _Malformed(str(type(e).__name__)) from None
    return raw, env


def _payload(model: type[BaseModel], payload: dict[str, Any]) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise _Malformed("payload") from None


class Session:
    def __init__(self, ws: WebSocket, ctx: GatewayContext) -> None:
        self.ws = ws
        self.ctx = ctx
        self.identity: str | None = None
        self.node_id: UUID | None = None
        self.conn_id: int | None = None
        self.person_id: UUID | None = None
        self.link: relay.NodeLink | None = None
        self._resume: tuple[ResumePosition, ...] = ()
        self._stopped = asyncio.Event()
        self._stop_reason: str | None = None
        self._socket_reason = ""
        self._node_public: bytes = b""
        self._node_key_id = ""
        self._out_seq = 0
        self._in_seq = 0
        self._send_lock = asyncio.Lock()
        self._close_recorded = False  # gw.close_connection has closed (or found closed) the row
        self._closing: asyncio.Future[None] | None = None  # the close in flight, if any
        self.keys = ctx.keys  # this connection's keys, for its whole life (R12-6)

    # ------------------------------------------------------------------ running

    async def run(self) -> None:
        await self.ws.accept()
        try:
            await self._run()
        except BaseException as e:
            if self.conn_id is None:
                raise  # no row was opened: nothing to record (the handshake's own refusals)
            # The row is open and the connection is over, whatever ended it: a send to a peer
            # that has gone, a send timeout, a database failure or a fault here. Its
            # DISCONNECT is recorded with a reason (final review I-2), and only the exception's
            # class is logged, never its text.
            await self._record_close(self._stop_reason or _close_reason(e))
            if not isinstance(e, Exception):
                raise  # cancelled (shutdown): recorded, then let it go on
            log.warning("connection %s ended by %s", self.conn_id, type(e).__name__)
        finally:
            if self.conn_id is not None:
                # Every other way out (the receive loop returning, the relay stopping it)
                # recorded it already; this is a no-op then.
                await self._record_close(self._stop_reason or "node_closed")
            await self._close_socket(CLOSE_REFUSED, self._socket_reason)

    async def _record_close(self, reason: str) -> None:
        """Record the close unless it is recorded already, or in flight (then wait for it)."""
        if self._closing is not None and not self._closing.done():
            await asyncio.shield(self._closing)
        if not self._close_recorded:
            await self._db_close(reason)

    async def _run(self) -> None:
        if not await self._handshake():
            return
        assert self.node_id is not None and self.person_id is not None
        assert self.conn_id is not None
        self.link = relay.NodeLink(
            self.ctx.relay,
            self,
            node_id=self.node_id,
            person_id=self.person_id,
            conn_id=self.conn_id,
            connected_at=self.ctx.clock(),
            gw_key_id=self.keys.gw_key_id,
        )
        self.ctx.relay.register(self.link)
        if self.keys.gw_key_id != self.ctx.keys.gw_key_id:  # rotated during the handshake
            # Stops the link too, so on_connect delivers nothing under the replaced key.
            self.link._stop(relay.KEY_ROTATED, relay.KEY_ROTATED)
        try:
            await self.link.on_connect(self._resume)
            await self._established()
        finally:
            self.ctx.relay.unregister(self.link)
            await self.link.close()

    async def _established(self) -> None:
        tasks = [
            asyncio.create_task(self._serve()),
            asyncio.create_task(self._recheck()),
            asyncio.create_task(self._stopped.wait()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also when this connection itself is cancelled (shutdown): no task is left
            # pending, and no task's exception goes unretrieved.
            for task in tasks:
                relay.reap(task)
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()
        if self._stopped.is_set() and self._stop_reason is not None:
            await self._db_close(self._stop_reason)

    def stop(self, db_reason: str | None, socket_reason: str = "") -> None:
        """End this connection (called by the relay): `db_reason` goes to gw.close_connection,
        None when the database already closed or fenced it."""
        if not self._stopped.is_set():
            self._stop_reason = db_reason
            self._socket_reason = socket_reason
            self._stopped.set()

    async def _close_socket(self, code: int, reason: str = "") -> None:
        if (
            self.ws.application_state == WebSocketState.CONNECTED
            and self.ws.client_state == WebSocketState.CONNECTED
        ):
            # Bounded like every send: a peer that stopped reading can't hold the close.
            with contextlib.suppress(RuntimeError, TimeoutError):
                await asyncio.wait_for(self.ws.close(code, reason or None), self.ctx.send_timeout)

    async def send(self, kind: MessageKind, payload: dict[str, Any]) -> None:
        """Sign and send one envelope with the next gateway `seq` (R6-8). Gateway messages
        carry the recipient's node ID, the nil UUID only before the node is known (§6.1)."""
        async with self._send_lock:
            self._out_seq += 1
            env = {
                "version": 1,
                "message_id": str(uuid.uuid4()),
                "node_id": str(self.node_id or NIL_UUID),
                "seq": self._out_seq,
                "sent_at": self.ctx.clock().isoformat(),
                "kind": kind.value,
                "payload": payload,
            }
            signed = signing.sign_envelope(
                signing.Purpose.GW_TRANSPORT, self.keys.gw_key, self.keys.gw_key_id, env
            )
            # Bounded: a peer that stops reading must not hold this connection (or the
            # relay's sender for it) for minutes (fix round 1, I-1).
            try:
                await asyncio.wait_for(
                    self.ws.send_text(json.dumps(signed, ensure_ascii=False)),
                    self.ctx.send_timeout,
                )
            except TimeoutError:
                raise relay.SendTimeout from None

    async def _error(self, code: str, detail: str) -> None:
        await self.send(MessageKind.ERROR, {"code": code, "detail": detail})

    # ------------------------------------------------------------------ handshake

    async def _handshake(self) -> bool:
        """True with an open connection row and `welcome` sent; False after a refusal."""
        try:
            await self._authenticate()
        except HandshakeRefused as r:
            if not r.audited:
                await self._refuse(r.code, r.node_id)
            log.info("handshake refused: %s", r.code)
            await self._close_socket(
                CLOSE_REENROL if r.code == "reenrol_required" else CLOSE_REFUSED
            )
            return False
        except _PeerLeft:
            return False
        return True

    async def _refuse(self, code: str, node_id: UUID | None) -> None:
        """`gw.refuse_handshake`, committed before the socket closes (R12-1)."""
        async with self.ctx.pool.connection() as conn:
            await audited_call_async(
                conn, "SELECT gw.refuse_handshake(%s, %s, %s)", (code, self.identity, node_id)
            )

    async def _authenticate(self) -> None:
        server = self.ws.scope.get("server")
        if self.ctx.bind_check and not is_tailnet_address(server[0] if server else None):
            raise HandshakeRefused("unknown_peer")
        peer = self.ws.client.host if self.ws.client else None
        if peer is not None:
            try:
                self.identity = await self.ctx.whois.identity(peer)
            except Exception:
                log.warning("whois failed", exc_info=True)
        if self.identity is None:
            raise HandshakeRefused("unknown_peer")

        nonce = signing.new_nonce()
        issued_at = self.ctx.clock()
        expires_at = issued_at + CHALLENGE_TTL
        await self.send(
            MessageKind.CHALLENGE,
            {
                "nonce": nonce,
                "issued_at": issued_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "key_id": self.keys.gw_key_id,
                "public_key": _b64(self.keys.gw_public),
            },
        )
        text = await self._first_frame()
        received_at = self.ctx.clock()
        # The nonce is consumed by this first reply, whatever its outcome: there is no second.
        raw, env, reply = self._check_common(text, nonce, expires_at, received_at)
        skew_ms = (env.sent_at - received_at) // timedelta(milliseconds=1)
        skew_ms = max(-SKEW_CLAMP_MS, min(SKEW_CLAMP_MS, skew_ms))
        skew_arg = skew_ms if abs(skew_ms) > SKEW_TOLERANCE_MS else None
        if isinstance(reply, Hello):
            self._resume = reply.resume
            await self._path_a(raw, env, skew_arg)
        else:
            await self._path_b(raw, reply, skew_arg)

    async def _first_frame(self) -> str | bytes:
        try:
            message = await asyncio.wait_for(self.ws.receive(), HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            raise HandshakeRefused("challenge_expired") from None
        if message["type"] == "websocket.disconnect":
            raise _PeerLeft
        text = message.get("text")
        return text if text is not None else message.get("bytes") or b""

    def _check_common(
        self, frame: str | bytes, nonce: str, expires_at: datetime, received_at: datetime
    ) -> tuple[dict[str, Any], NodeEnvelope, Hello | Enrol]:
        """Checks common to both paths (§6.1 step 6), in order."""
        if not isinstance(frame, str) or len(frame.encode("utf-8")) > MAX_FRAME:
            raise HandshakeRefused("malformed")
        try:
            raw, env = parse_envelope(frame)
        except _Malformed:
            raise HandshakeRefused("malformed") from None
        if env.kind not in (MessageKind.HELLO, MessageKind.ENROL):
            raise HandshakeRefused("not_first")
        if env.seq != 1:
            raise HandshakeRefused("bad_seq")
        try:
            reply: Hello | Enrol = _payload(
                Hello if env.kind is MessageKind.HELLO else Enrol, raw["payload"]
            )
        except _Malformed:
            raise HandshakeRefused("malformed") from None
        if isinstance(reply, Enrol) and (
            env.node_id != NIL_UUID or len(reply.token) != TOKEN_LENGTH
        ):
            raise HandshakeRefused("malformed")  # never a malformed gw.enrol_node call
        if not hmac.compare_digest(reply.nonce.encode("ascii"), nonce.encode("ascii")):
            raise HandshakeRefused("bad_nonce")
        if received_at > expires_at:
            raise HandshakeRefused("challenge_expired")
        self._in_seq = 1
        return raw, env, reply

    async def _path_a(self, raw: dict[str, Any], env: NodeEnvelope, skew: int | None) -> None:
        """An existing node (`hello`), unchanged by enrolment."""
        node_id = env.node_id
        if node_id == NIL_UUID:
            raise HandshakeRefused("malformed")
        async with self.ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT public_key, tailscale_identity, revoked_at, person_id, key_state"
                " FROM gw.node WHERE id = %s",
                (node_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise HandshakeRefused("unknown_node")  # a claimed ID is never stored
        public_key, identity, revoked_at = bytes(row[0]), row[1], row[2]
        self.person_id = UUID(str(row[3]))
        if not signing.verify_envelope(
            signing.Purpose.NODE_MSG, public_key, node_key_id(public_key), raw
        ):
            raise HandshakeRefused("bad_signature", node_id)
        if revoked_at is not None:
            raise HandshakeRefused("revoked", node_id)
        if identity != self.identity:
            raise HandshakeRefused("identity_mismatch", node_id)
        self._check_key_state(node_id, str(row[4]))
        await self._open(node_id, public_key, skew)
        await self.send(
            MessageKind.WELCOME, await relay.welcome(self.ctx.pool, self.person_id, node_id)
        )

    def _check_key_state(self, node_id: UUID, key_state: str) -> None:
        """The node's `key_state` decision point (§6.1 step 6, R6-6): CURRENT goes on to
        `welcome`; STALE is refused `reenrol_required`, closed 4409 with no welcome and no
        post-handshake content, and no connection row is opened."""
        if key_state != "CURRENT":
            raise HandshakeRefused("reenrol_required", node_id)

    async def _path_b(self, raw: dict[str, Any], enrol: Enrol, skew: int | None) -> None:
        """A new enrolment (`enrol`, nil node ID): proof of the carried key, then gw.enrol_node
        with this connection's `whois` identity."""
        public_key = signing.b64url_decode(enrol.public_key, 32)
        if public_key is None:
            raise HandshakeRefused("malformed")
        if not signing.verify_envelope(
            signing.Purpose.NODE_MSG, public_key, node_key_id(public_key), raw
        ):
            raise HandshakeRefused("bad_signature")
        async with self.ctx.pool.connection() as conn:
            node_id, refusal = await audited_call_async(
                conn,
                "SELECT node_id, refusal FROM gw.enrol_node(%s, %s, %s)",
                (enrol.token, public_key, self.identity),
            )
        if refusal is not None:
            raise HandshakeRefused(str(refusal), audited=True)
        node_id = UUID(str(node_id))
        async with self.ctx.pool.connection() as conn:
            cur = await conn.execute("SELECT person_id FROM gw.node WHERE id = %s", (node_id,))
            row = await cur.fetchone()
        assert row is not None  # nodes are never deleted
        self.person_id = UUID(str(row[0]))
        await self._open(node_id, public_key, skew)
        core_key_id, core_public = self.keys.current_core_key
        await self.send(
            MessageKind.WELCOME,
            {
                **await relay.welcome(self.ctx.pool, self.person_id, node_id),
                "enrolment": {
                    "node_id": str(node_id),
                    "core_key_id": core_key_id,
                    "core_public_key": _b64(core_public),
                },
            },
        )

    async def _open(self, node_id: UUID, public_key: bytes, skew: int | None) -> None:
        async with self.ctx.pool.connection() as conn:
            conn_id, refusal = await audited_call_async(
                conn,
                "SELECT conn_id, refusal FROM gw.open_connection(%s, %s)",
                (node_id, skew),
            )
        if refusal is not None:  # a revocation racing the handshake; audited by the function
            raise HandshakeRefused(str(refusal), audited=True)
        self.node_id = node_id
        self.conn_id = int(conn_id)
        self._node_public = public_key
        self._node_key_id = node_key_id(public_key)

    # ------------------------------------------------------------------ established

    async def _serve(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                await self._db_close("node_closed")
                return
            try:
                await self._handle(message.get("text"), message.get("bytes"))
            except CloseConnection as c:
                log.info("closing connection %s: %s", self.conn_id, c.reason)
                await self._db_close(c.reason)
                return
            except Fenced:
                log.info("connection %s fenced", self.conn_id)
                return
            except relay.SendTimeout:
                log.info("connection %s: send timed out", self.conn_id)
                await self._db_close(relay.SEND_TIMEOUT)
                return
            except WebSocketDisconnect:  # a reply to a peer that has gone (I-2)
                log.info("connection %s: the node has gone", self.conn_id)
                await self._db_close("node_closed")
                return
            except psycopg.Error as e:
                # Any other database failure: its audited transaction rolled back, so nothing
                # partial remains. Tell the node if it can still be told (the reply may fail or
                # time out: I-2), then close through gw.close_connection.
                log.warning("database error on connection %s (%s)", self.conn_id, e.sqlstate)
                with contextlib.suppress(Exception):
                    await self._error(SERVER_ERROR, "the server could not process the message")
                await self._db_close(SERVER_ERROR)
                return

    async def _handle(self, text: str | None, data: bytes | None) -> None:
        size = len(text.encode("utf-8")) if text is not None else len(data or b"")
        if size > MAX_FRAME:
            await self._error("too_large", f"messages are limited to {MAX_FRAME} bytes")
            return
        if text is None:
            await self._error("malformed", "messages are JSON text frames")
            return
        try:
            raw, env = parse_envelope(text)
        except _Malformed:
            await self._error("malformed", "not a valid envelope")
            return
        if env.node_id != self.node_id:
            raise CloseConnection("wrong_node")
        if not signing.verify_envelope(
            signing.Purpose.NODE_MSG, self._node_public, self._node_key_id, raw
        ):
            raise CloseConnection("bad_signature")
        if env.seq != self._in_seq + 1:
            raise CloseConnection("bad_seq")
        self._in_seq = env.seq
        await self._dispatch(env, raw)

    async def _dispatch(self, env: NodeEnvelope, raw: dict[str, Any]) -> None:
        payload: dict[str, Any] = raw["payload"]
        kind = env.kind
        if kind in (MessageKind.HELLO, MessageKind.ENROL):
            raise CloseConnection("bad_nonce")  # the nonce was consumed by the first reply
        if kind not in NODE_KINDS:
            await self._error("unexpected_kind", "this kind is sent by the gateway only")
            return
        try:
            _payload(MESSAGE_MODELS[kind.value], payload)
        except _Malformed:
            await self._error("malformed", "the payload does not match its schema")
            return
        if kind in INBOUND_KINDS:
            await self._record_inbound(env.message_id, kind, payload, raw)
        elif kind is MessageKind.PING:
            await self._db("SELECT gw.touch_connection(%s)", (self.conn_id,))
            await self.send(MessageKind.PONG, {})
        elif kind is MessageKind.PONG:
            assert self.link is not None
            self.link.on_pong()
        else:  # received / ack
            await self._confirm(kind, payload)

    async def _confirm(self, kind: MessageKind, payload: dict[str, Any]) -> None:
        assert self.link is not None
        model = Ack if kind is MessageKind.ACK else Received
        ref = _payload(model, payload).root
        try:
            refusal = await self.link.on_confirm(kind, ref)
        except psycopg.Error as e:
            if _fenced(e):
                raise Fenced from None
            raise
        if refusal is not None:
            await self._error(refusal, "the message was refused")

    async def _record_inbound(
        self, message_id: UUID, kind: MessageKind, payload: Any, envelope: dict[str, Any]
    ) -> None:
        """`conv.record_inbound`, with the node's complete signed envelope for core to
        re-verify before an irreversible step (R12-34)."""
        try:
            async with self.ctx.pool.connection() as conn:
                _, refusal = await audited_call_async(
                    conn,
                    "SELECT recorded, refusal FROM conv.record_inbound(%s, %s, %s, %s, %s)",
                    (self.conn_id, message_id, kind.value, Jsonb(payload), Jsonb(envelope)),
                )
        except psycopg.Error as e:
            if _fenced(e):
                raise Fenced from None
            raise
        if refusal is not None:
            await self._error(str(refusal), "the message was refused")

    async def _db(self, sql: LiteralString, params: tuple[Any, ...]) -> None:
        """One connection-scoped call outside a transaction; JF001 → `Fenced`."""
        try:
            async with self.ctx.pool.connection() as conn:
                await conn.execute(sql, params)
        except psycopg.Error as e:
            if _fenced(e):
                raise Fenced from None
            raise

    async def _db_close(self, reason: str) -> None:
        """gw.close_connection: its DISCONNECT row records why (an already closed connection is
        a no-op there). Shielded: once started, it completes even if this connection's tasks
        are cancelled (a shutdown), so the disconnect is still recorded."""
        self._closing = asyncio.ensure_future(self._db_close_now(reason))
        await asyncio.shield(self._closing)

    async def _db_close_now(self, reason: str) -> None:
        try:
            async with self.ctx.pool.connection() as conn:
                await audited_call_async(
                    conn, "SELECT gw.close_connection(%s, %s)", (self.conn_id, reason)
                )
            self._close_recorded = True
        except psycopg.Error as e:
            if _fenced(e):
                self._close_recorded = True  # closed already (superseded or revoked)
            else:  # the database is unreachable: open_connection supersedes later
                log.warning("could not close connection %s (%s)", self.conn_id, e.sqlstate)

    async def _recheck(self) -> None:
        """Every `recheck_interval` seconds: `gw.current_node`, returning once fenced (P2-E10)."""
        while True:
            await asyncio.sleep(self.ctx.recheck_interval)
            try:
                await self._db("SELECT gw.current_node(%s)", (self.conn_id,))
            except Fenced:
                log.info("connection %s fenced at re-check", self.conn_id)
                return
            except psycopg.Error:  # the database is unreachable: try again next interval
                log.warning("revocation re-check failed for connection %s", self.conn_id)


def _close_reason(e: BaseException) -> str:
    """The gw.close_connection reason for an exception that ended an open connection."""
    if isinstance(e, relay.SendTimeout):
        return relay.SEND_TIMEOUT
    if isinstance(e, WebSocketDisconnect | OSError | asyncio.CancelledError):
        return "node_closed"  # the peer has gone (or the gateway is shutting down)
    return SERVER_ERROR  # a database failure, or a fault here


class _PeerLeft(Exception):
    """The peer disconnected before replying to the challenge: nothing to refuse."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
