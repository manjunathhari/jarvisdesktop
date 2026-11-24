"""A fake node for gateway tests: a WebSocket client that follows the node-side rules.

It signs every envelope it sends as ``node-msg`` under ``fingerprint(its public key)`` (R12-6),
with its own per-connection ``seq``. It checks everything the gateway sends: the gateway's
``seq`` (``SeqChecker``), the ``gw-transport`` signature under the key the connection's
challenge announced, and the node rules in ``node_rules`` (challenge, enrolment welcome,
``node.action``, ``keyset``). It never accepts a key from the gateway alone: the gateway key is
pinned only through the enrolment code's gateway fingerprint, and the core key only through
``welcome.enrolment`` matching the code's core fingerprint (§6.1, §7.2). Until then every
core-authorised item (``node.action``, an approval card, ``keyset``, anything shown) is
refused and recorded in ``rejected``.

Its pins, node ID and seen-list live in a JSON file (``state_path``), reloaded by a new
``FakeNode`` on the same path. A frame the gateway can't parse (too large, not JSON, not an
envelope) carries no ``seq`` the gateway could count, so building one with ``envelope()`` and
sending it with ``send_raw()`` doesn't move the node's counter.

An ``outbox.item`` whose ``authz`` is a ``node.action`` (an approved action, Task 11) goes
through ``check_node_action`` like a ``node.action`` frame, and needs an ``approval_id`` and an
unused ``execution_key`` (§7.1 rule 5); a rejected one is not shown.

Delivery (Task 7): it sends ``received`` automatically when an outbox item arrives
(``auto_received``) and for every reply chunk (``ack_chunks``), answers the gateway's ``ping``
with ``pong`` (``answer_pings``), shows a ``(reply_id, seq)`` at most once, and never shows an
item it has seen or that ``outbox.cleared`` removed; the cleared list lives in the state file
too. ``log`` records each display and each clear in order, for the P2-E13 invariants.
``sync()`` is a barrier: a ``ping`` whose ``pong`` follows everything the gateway sent first.

Keys (Task 12): a ``keyset`` (carrying its unsigned ``outbox_id``, R12-26) is accepted only if
``check_keyset`` passes with a pinned CORE key and its signed ``target_node_id`` is null or this
node (R12-27). Only then does the node send ``received`` and ``ack`` for it. The new key is
added to the CORE or GATEWAY pins by role, and the role's existing keys stay trusted until
24 h after the keyset's ``issued_at`` (``key_expiry``). A key still waiting out an earlier
overlap is dropped when a later keyset arrives. On close code 4409, an unpinned gateway key in
a challenge, a ``gw-transport`` envelope on a connection whose challenge key is no longer
trusted (its overlap ended), or a core signature under an unknown ``key_id``, the node runs
nothing, keeps its
journal (the state file), closes and sets ``prompt = "reenrol"`` (R5-1). Any other close
means reconnect (``may_reconnect``, ``reconnect()``; R12-29). Every ``gw-transport`` envelope
is verified under the challenge's key only; one that fails while that key is trusted is a bad
signature: the node closes and reconnects, with no re-enrol prompt.

Also here: ``FakeWhoIs`` (dict-backed ``whois``) and ``PeerFromHeader``, a test-only ASGI
wrapper that sets the connection's peer address from a request header, because Starlette's
TestClient has one fixed client address. The gateway itself never reads that header.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from jarvis_server.contracts import NIL_UUID, Keyset
from jarvis_server.nodes import parse_enrolment_code
from jarvis_server.signing import (
    AuthzInvalid,
    Purpose,
    b64url_decode,
    check_authz,
    fingerprint,
    public_bytes,
    sign_envelope,
    verify_envelope,
)

from ..schema_registry import CONTRACTS, validator
from .node_rules import (
    KEYSET_FIELDS,
    Rejected,
    SeqChecker,
    apply_keyset,
    check_challenge,
    check_enrolment_welcome,
    check_keyset,
    check_node_action,
)

PEER_HEADER = "x-test-peer-ip"
# Refused until the core key is pinned (§6.1 step 7): anything core-authorised or shown.
CORE_CONTENT = frozenset({"node.action", "keyset", "outbox.item", "reply.chunk"})
ROUTE = "/v1/node"
REENROL_CLOSE = 4409


class FakeWhoIs:
    """``whois`` from a dict: peer IP → Tailscale identity. Unknown peers give None."""

    def __init__(self, table: dict[str, str] | None = None) -> None:
        self.table: dict[str, str] = dict(table or {})

    async def identity(self, peer_ip: str) -> str | None:
        return self.table.get(peer_ip)


class PeerFromHeader:
    """Test-only ASGI wrapper: a WebSocket's ``client`` address comes from ``x-test-peer-ip``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            headers = [(k, v) for k, v in scope["headers"] if k != PEER_HEADER.encode()]
            peer = dict(scope["headers"]).get(PEER_HEADER.encode())
            scope = {**scope, "headers": headers}
            if peer is not None:
                scope["client"] = (peer.decode(), 50000)
        await self.app(scope, receive, send)


def _conform(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Check a payload the fake desktop sends against its shared contract (R12-25)."""
    schema = json.loads((CONTRACTS / "messages" / f"{name}.schema.json").read_text())
    validator(schema).validate(payload)
    return payload


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _iso(t: datetime) -> str:
    return t.isoformat()


class FakeNode:
    def __init__(
        self,
        client: TestClient,
        *,
        key: Ed25519PrivateKey,
        node_id: UUID | None,
        peer_ip: str,
        state_path: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.key = key
        self.public = public_bytes(key)
        self.key_id = fingerprint(self.public)
        self.peer_ip = peer_ip
        self.state_path = state_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.pinned_gateway: dict[str, bytes] = {}
        self.trusted_core: dict[str, bytes] = {}
        self.key_expiry: dict[str, datetime] = {}  # key_id -> no longer trusted after this
        self.seen_outbox: set[str] = set()
        self.cleared: set[str] = set()
        self.last_shown: dict[str, int] = {}
        self.auto_received = True
        self.ack_chunks = True
        self.answer_pings = True
        self.log: list[tuple[str, str]] = []
        self.node_id = node_id
        self._load()
        if node_id is not None:
            self.node_id = node_id
        self.rejected: list[tuple[str, str]] = []
        self.displays: Counter[str] = Counter()
        self.shown_chunks: dict[str, list[int]] = defaultdict(list)
        self.actions: list[dict[str, Any]] = []
        self.used_keys: set[str] = set()  # execution keys run (the desktop's journal, §9.4)
        self.received_envelopes: list[dict[str, Any]] = []
        self.discarded = False
        self.prompt: str | None = None  # "reenrol": Jarvis's keys have changed (R5-1)
        self.last_close: int | None = None  # the gateway's last close code
        self.sent: list[str] = []  # the kinds this FakeNode sent, in order
        self.challenge: dict[str, Any] | None = None
        self.last_sent: str | None = None
        self._ctx: Any = None
        self.ws: Any = None
        self._seq = 0
        self._peer_seq = SeqChecker()
        self._gw_key_id: str | None = None
        self._gw_public: bytes | None = None
        self._gw_from_code = False
        self._authorised = False

    # ------------------------------------------------------------------ state file

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text())
        if state.get("node_id"):
            self.node_id = UUID(state["node_id"])
        self.pinned_gateway = {k: bytes.fromhex(v) for k, v in state["gateway_keys"].items()}
        self.trusted_core = {k: bytes.fromhex(v) for k, v in state["core_keys"].items()}
        self.key_expiry = {
            k: datetime.fromisoformat(v) for k, v in state.get("key_expiry", {}).items()
        }
        self.seen_outbox = set(state["seen_outbox"])
        self.cleared = set(state.get("cleared", []))
        self.last_shown = dict(state["shown"])

    def _save(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "node_id": str(self.node_id) if self.node_id else None,
                    "gateway_keys": {k: v.hex() for k, v in self.pinned_gateway.items()},
                    "core_keys": {k: v.hex() for k, v in self.trusted_core.items()},
                    "key_expiry": {k: v.isoformat() for k, v in self.key_expiry.items()},
                    "seen_outbox": sorted(self.seen_outbox),
                    "cleared": sorted(self.cleared),
                    "shown": self.last_shown,
                }
            )
        )

    # ------------------------------------------------------------------ connection

    @property
    def connected(self) -> bool:
        return self.ws is not None

    @property
    def next_gateway_seq(self) -> int:
        """The ``seq`` the next gateway message must carry (for tests that inject frames)."""
        return self._peer_seq._next

    def connect(self, *, enrol_gateway_fingerprint: str | None = None) -> dict[str, Any]:
        """Open a connection and check the challenge (§6.1 step 3); return its payload.

        An enrolling device passes the gateway fingerprint from its code and has no pinned key;
        an enrolled one checks against its pinned gateway keys. On a bad challenge the node
        closes and sends nothing.
        """
        self._ctx = self.client.websocket_connect(ROUTE, headers={PEER_HEADER: self.peer_ip})
        self.ws = self._ctx.__enter__()
        self._seq = 0
        self._peer_seq = SeqChecker()
        self._authorised = False
        self._gw_from_code = enrol_gateway_fingerprint is not None  # not pinned yet (§7.2)
        self._prune()
        env = self._receive(2.0)
        try:
            payload = check_challenge(
                env,
                pinned_gateway=None if enrol_gateway_fingerprint else self.pinned_gateway,
                enrol_fingerprint=enrol_gateway_fingerprint,
            )
            self._peer_seq.expect(env["seq"])
        except Rejected as e:
            if e.reason == "unpinned_key":
                self.prompt = "reenrol"  # a gateway key it never pinned (R5-1)
            self._refuse("challenge", e.reason)
        self._gw_key_id = payload["key_id"]
        self._gw_public = b64url_decode(payload["public_key"], 32)
        self.challenge = payload
        return payload

    def close(self) -> None:
        if self._ctx is not None:
            ctx, self._ctx, self.ws = self._ctx, None, None
            ctx.__exit__(None, None, None)

    def _prune(self) -> None:
        """Drop the keys whose overlap has ended."""
        now = self.clock()
        for key_id, until in list(self.key_expiry.items()):
            if now > until:
                self.trusted_core.pop(key_id, None)
                self.pinned_gateway.pop(key_id, None)
                del self.key_expiry[key_id]
                self._save()

    @property
    def may_reconnect(self) -> bool:
        """R12-29: reconnect after any close except 4409 (4401 with any reason, such as
        `key_rotated`, `superseded`, `heartbeat` or `send_timeout`), unless a signature under a
        key it never pinned or no longer trusts has asked for re-enrolment."""
        return self.prompt is None and self.last_close != REENROL_CLOSE

    def reconnect(self) -> dict[str, Any]:
        """Connect again and say ``hello``; returns the ``welcome`` payload."""
        if not self.may_reconnect:
            raise RuntimeError("this device must be re-enrolled")
        self.close()
        self.connect()
        return self.hello()

    def sent_kinds(self) -> list[str]:
        return list(self.sent)

    def _refuse(self, what: str, reason: str) -> Any:
        """Record a rejection, close the connection without replying, and raise."""
        self.rejected.append((what, reason))
        self.close()
        raise Rejected(reason)

    # ------------------------------------------------------------------ handshake

    def hello(
        self,
        *,
        nonce: str | None = None,
        clock_skew_ms: int | None = None,
        sent_at: datetime | None = None,
        node_id: UUID | None = None,
        resume: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """``hello`` (path A); returns the ``welcome`` payload. ``resume`` defaults to the
        last shown ``seq`` of every reply in the state file."""
        if not self.connected:
            self.connect()
        assert self.challenge is not None
        if resume is None:
            resume = [{"reply_id": r, "last_seq": s} for r, s in sorted(self.last_shown.items())]
        payload: dict[str, Any] = {"nonce": nonce or self.challenge["nonce"], "resume": resume}
        if clock_skew_ms is not None:
            payload["clock_skew_ms"] = clock_skew_ms
        self.send("hello", payload, sent_at=sent_at, node_id=node_id)
        env = self.recv()
        if env["kind"] != "welcome":
            raise AssertionError(f"expected welcome, got {env['kind']}")
        self._authorised = bool(self.trusted_core)
        return dict(env["payload"])

    def enrol(self, code: str, *, sent_at: datetime | None = None) -> UUID:
        """Enrol with a code (§7.2): pin the gateway key by the code's gateway fingerprint,
        send ``enrol``, then pin the core key only if ``welcome.enrolment`` matches the code's
        core fingerprint. On a mismatch, discard the keys, store nothing and close."""
        token, core_fp, gw_fp = parse_enrolment_code(code)
        if not self.connected:
            self.connect(enrol_gateway_fingerprint=gw_fp)
        assert self.challenge is not None
        self.send(
            "enrol",
            {"token": token, "public_key": b64(self.public), "nonce": self.challenge["nonce"]},
            sent_at=sent_at,
            node_id=NIL_UUID,
        )
        env = self.recv()
        while env["kind"] != "welcome":
            env = self.recv()
        try:
            node_id, core_key_id, core_public = check_enrolment_welcome(
                env["payload"], core_fingerprint=core_fp
            )
            if UUID(env["node_id"]) != node_id:
                raise Rejected("node_id_mismatch")
        except Rejected as e:
            self.discarded = True
            self.pinned_gateway, self.trusted_core, self.node_id = {}, {}, None
            self._refuse("welcome", e.reason)
        assert self._gw_key_id is not None and self._gw_public is not None
        self.pinned_gateway = {self._gw_key_id: self._gw_public}
        self._gw_from_code = False
        self.trusted_core = {core_key_id: core_public}
        self.node_id = node_id
        self._authorised = True
        self._save()
        return node_id

    # ------------------------------------------------------------------ sending

    def envelope(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        seq: int | None = None,
        sent_at: datetime | None = None,
        node_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> dict[str, Any]:
        """A signed envelope with the next ``seq``, without consuming it."""
        env = {
            "version": 1,
            "message_id": str(message_id or uuid.uuid4()),
            "node_id": str(node_id or self.node_id or NIL_UUID),
            "seq": self._seq + 1 if seq is None else seq,
            "sent_at": _iso(sent_at or self.clock()),
            "kind": kind,
            "payload": payload,
        }
        return sign_envelope(Purpose.NODE_MSG, self.key, self.key_id, env)

    def send(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        seq: int | None = None,
        sent_at: datetime | None = None,
        node_id: UUID | None = None,
        message_id: UUID | None = None,
        mutate: Callable[[dict[str, Any]], None] | None = None,
    ) -> UUID:
        """Sign and send one message; ``mutate`` changes the envelope after signing."""
        if self.discarded:
            raise RuntimeError("this device discarded its keys")
        env = self.envelope(
            kind, payload, seq=seq, sent_at=sent_at, node_id=node_id, message_id=message_id
        )
        if seq is None:
            self._seq += 1
        if mutate is not None:
            mutate(env)
        self.send_raw(json.dumps(env))
        self.sent.append(kind)
        return UUID(env["message_id"])

    def send_raw(self, text: str) -> None:
        self.last_sent = text
        self.ws.send_text(text)

    def listing_receipt(
        self, reply_id: str, seq: int, files: list[dict[str, Any]], result: str = "OK"
    ) -> UUID:
        """The desktop's answer to a live ``pc.list_test_folder``, in the contract's shape."""
        return self.send("action.receipt", _conform("pc.list_test_folder.receipt", {
            "reply_id": reply_id, "seq": seq, "result": result, "evidence": {"files": files},
        }))  # fmt: skip

    def delete_receipt(self, execution_key: str, result: str, evidence: dict[str, Any]) -> UUID:
        """The desktop's receipt for an approved ``pc.delete_test_file``, in the contract's
        shape."""
        return self.send("action.receipt", _conform("pc.delete_test_file.receipt", {
            "execution_key": execution_key, "result": result, "evidence": evidence,
        }))  # fmt: skip

    def received(self, outbox_id: UUID) -> UUID:
        return self.send("received", {"outbox_id": str(outbox_id)})

    def ack(self, outbox_id: UUID) -> UUID:
        return self.send("ack", {"outbox_id": str(outbox_id)})

    # ------------------------------------------------------------------ receiving

    def _receive(self, timeout: float) -> dict[str, Any]:
        """The next frame as JSON; WebSocketDisconnect if the gateway closed; TimeoutError."""
        ws = self.ws
        if ws is None:
            raise RuntimeError("not connected")
        next_message = getattr(ws, "next_message", None)  # a live socket (live_node.py)
        if next_message is not None:
            message = next_message(timeout)
        else:

            async def _next() -> Any:
                with anyio.fail_after(timeout):
                    return await ws._send_rx.receive()

            message = ws.portal.call(_next)
        if message["type"] == "websocket.close":
            self.close()
            self.last_close = message.get("code")
            if message.get("code") == REENROL_CLOSE:
                self.prompt = "reenrol"  # runs nothing; the state file (journal) is kept
            raise WebSocketDisconnect(code=message.get("code", 1000))
        return dict(json.loads(message["text"]))

    def recv(self, timeout: float = 2.0) -> dict[str, Any]:
        """The next gateway message, after every node-side check. A gateway ``ping`` is
        answered with ``pong`` when ``answer_pings`` is set."""
        env = self._receive(timeout)
        self.inject(env)
        if env["kind"] == "ping" and self.answer_pings:
            self.send("pong", {})
        return env

    def drain(self, timeout: float = 0.005) -> list[dict[str, Any]]:
        """Every message already sent, until none arrives within ``timeout``."""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self.recv(timeout))
            except TimeoutError:
                return out

    def sync(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Send ``ping`` and return what the gateway sent before its ``pong``. The gateway
        handles one connection's messages in order, so the ``pong`` follows everything that
        connection's earlier messages (and its connect-time delivery) caused."""
        self.send("ping", {})
        out: list[dict[str, Any]] = []
        while True:
            env = self.recv(timeout)
            if env["kind"] == "pong":
                return out
            out.append(env)

    def inject(self, env: dict[str, Any]) -> None:
        """Process one gateway envelope exactly as if it had arrived on the connection."""
        try:
            self._peer_seq.expect(env.get("seq"))  # type: ignore[arg-type]
        except Rejected as e:
            self._refuse(str(env.get("kind")), e.reason)
        assert self._gw_key_id is not None and self._gw_public is not None
        self._prune()
        what = str(env.get("kind"))
        if not self._gw_from_code and self.pinned_gateway.get(self._gw_key_id) != self._gw_public:
            # The connection's gateway key is no longer pinned (its overlap ended).
            self.prompt = "reenrol"
            self._refuse(what, "unpinned_key")
        # Under the challenge's key only (R12-6). The key is still trusted, so a failure is a
        # bad signature: close and reconnect, never re-enrol (spec §7.1, R12-29).
        if not verify_envelope(Purpose.GW_TRANSPORT, self._gw_public, self._gw_key_id, env):
            self._refuse(what, "bad_signature")
        self.received_envelopes.append(env)
        kind, payload = env["kind"], env["payload"]
        if kind in CORE_CONTENT and not self._authorised:
            self.rejected.append((kind, "unauthorised"))
            return
        try:
            if kind == "node.action":
                assert self.node_id is not None
                check_node_action(
                    payload["authz"], trusted=self.trusted_core, me=self.node_id, now=self.clock()
                )
                self.actions.append(dict(payload["authz"]["object"]))
            elif kind == "keyset":
                self._keyset(payload)
            elif kind == "outbox.item":
                authz = payload.get("authz")
                if authz is not None:
                    try:
                        check_authz(authz, self.trusted_core)
                    except AuthzInvalid:
                        raise Rejected("bad_signature") from None
                    fresh = payload["outbox_id"] not in self.seen_outbox
                    if authz["object"].get("kind") == "node.action" and fresh:
                        self._approved_action(authz)  # a re-send of a seen item never re-runs
                outbox_id = payload["outbox_id"]
                if outbox_id not in self.seen_outbox and outbox_id not in self.cleared:
                    self.seen_outbox.add(outbox_id)
                    self.displays[outbox_id] += 1
                    self.log.append(("display", outbox_id))
                    self._save()
                if self.auto_received and self.ws is not None:
                    self.send("received", {"outbox_id": outbox_id})
            elif kind == "outbox.cleared":
                self.cleared.add(payload["outbox_id"])
                self.log.append(("cleared", payload["outbox_id"]))
                self._save()
            elif kind == "reply.chunk":
                self._chunk(payload)
        except Rejected as e:
            self.rejected.append((kind, e.reason))
            if e.reason == "untrusted_key_id":  # a core key_id it never pinned (R5-1)
                self.prompt = "reenrol"
                self.close()

    def _keyset(self, payload: dict[str, Any]) -> None:
        """Accept a ``keyset`` (R6-2, R12-26, R12-27), then ``received`` and ``ack`` its item."""
        try:
            keyset = Keyset.model_validate(payload)
        except ValidationError:
            raise Rejected("malformed") from None
        signed = {f: payload[f] for f in KEYSET_FIELDS}  # without outbox_id and authz
        assert self.node_id is not None
        obj = check_keyset(
            signed, payload["authz"], trusted=self.trusted_core, me=self.node_id, now=self.clock()
        )
        before = (dict(self.trusted_core), dict(self.pinned_gateway), dict(self.key_expiry))
        apply_keyset(
            obj,
            trusted_core=self.trusted_core,
            pinned_gateway=self.pinned_gateway,
            key_expiry=self.key_expiry,
        )
        if (self.trusted_core, self.pinned_gateway, self.key_expiry) != before:
            self._save()
        if self.ws is not None:
            outbox_id = str(keyset.outbox_id)
            self.send("received", {"outbox_id": outbox_id})
            self.send("ack", {"outbox_id": outbox_id})

    def _approved_action(self, authz: dict[str, Any]) -> None:
        """§7.1 rules 2 to 4 (``check_node_action``), then rule 5: an IRREVERSIBLE action needs
        an ``approval_id`` and an ``execution_key`` this node hasn't used."""
        assert self.node_id is not None
        check_node_action(authz, trusted=self.trusted_core, me=self.node_id, now=self.clock())
        obj = authz["object"]
        key = obj.get("execution_key")
        if obj.get("approval_id") is None or key is None:
            raise Rejected("no_approval")
        if key in self.used_keys:
            raise Rejected("used_execution_key")
        self.used_keys.add(key)
        self.actions.append(dict(obj))

    def _chunk(self, payload: dict[str, Any]) -> None:
        reply_id, seq = payload["reply_id"], payload["seq"]
        if payload["type"] == "ACTION":
            assert self.node_id is not None
            check_node_action(
                payload["authz"], trusted=self.trusted_core, me=self.node_id, now=self.clock()
            )
            self.actions.append(dict(payload["authz"]["object"]))
        if seq > self.last_shown.get(reply_id, 0):
            self.last_shown[reply_id] = seq
            self.shown_chunks[reply_id].append(seq)
            self._save()
        if self.ack_chunks and self.ws is not None:
            self.send("received", {"reply_id": reply_id, "seq": seq})
