"""Gateway, part 1: handshake, enrolment, inbound recording, size and shape checks, the
revocation re-check, and the fake node (spec §4.1, §6.1, §7.1, §7.2; P2-S3, P2-S8; R12-1, R12-4,
R12-6).

Runs in its own database: node rows block the 0008 downgrade (R7-6). The gateway connects as
jarvis_gateway; tests read and act as jarvis_app (and the owner for ops.job), each check of an
audit row on a separate, new connection after the socket closed (R7-1). Keys and identities
are random test values; the only person is the seeded fictional household member.

STALE nodes (4409, `reenrol_required`) arrive with `gw.node.key_state` in Task 12, so
`test_stale_node_refusal_recorded` moves there (controller ruling).
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis_server import nodes
from jarvis_server.contracts import NIL_UUID
from jarvis_server.gateway import app as gateway_app
from jarvis_server.gateway.app import create_app
from jarvis_server.signing import (
    Purpose,
    b64url_decode,
    fingerprint,
    make_authz,
    public_bytes,
    sign_envelope,
    verify_envelope,
)

from .conftest import Dsns, fresh_database
from .fakes.fake_node import FakeNode, FakeWhoIs, PeerFromHeader, b64
from .fakes.node_rules import Rejected

Conn = psycopg.Connection[Any]
MANJ = uuid.UUID("00000000-0000-4000-8000-000000000002")
REFUSED = 4401


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def gdb() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture
def app_conn(gdb: Dsns) -> Iterator[Conn]:
    with psycopg.connect(gdb.app, autocommit=True) as conn:
        yield conn


@pytest.fixture(autouse=True)
def _empty_refusal_tally(gdb: Dsns) -> Iterator[None]:
    """A fresh refusal tally and window per test, so the global ceiling of 30 audit rows per
    window (R12-3) never decides another test's outcome."""
    with psycopg.connect(gdb.owner, autocommit=True) as conn:
        conn.execute("DELETE FROM gw.refusal_tally")
        conn.execute(
            "UPDATE gw.refusal_window SET window_start = '2000-01-01 00:00+00', audited = 0"
        )
    yield


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 23, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Gw:
    """One running gateway app under a TestClient, with its keys, clock and whois table."""

    dsns: Dsns
    client: TestClient
    clock: FakeClock
    whois: FakeWhoIs
    gw_key: Ed25519PrivateKey
    core_key: Ed25519PrivateKey
    tmp: Path
    nodes_made: list[FakeNode] = field(default_factory=list)
    app: Any = None

    @property
    def gw_key_id(self) -> str:
        return fingerprint(public_bytes(self.gw_key))

    @property
    def core_key_id(self) -> str:
        return fingerprint(public_bytes(self.core_key))

    def peer(self, identity: str | None = None) -> str:
        """A new tailnet peer address mapped to a new (or the given) identity."""
        ip = f"100.{secrets.randbelow(64) + 64}.{secrets.randbelow(256)}.{secrets.randbelow(256)}"
        self.whois.table[ip] = identity or f"ts:pc-{secrets.token_hex(6)}"
        return ip

    def node(
        self,
        *,
        key: Ed25519PrivateKey | None = None,
        node_id: uuid.UUID | None = None,
        peer_ip: str | None = None,
        state: Path | None = None,
    ) -> FakeNode:
        fake = FakeNode(
            self.client,
            key=key or Ed25519PrivateKey.generate(),
            node_id=node_id,
            peer_ip=peer_ip or self.peer(),
            state_path=state or self.tmp / f"node-{secrets.token_hex(4)}.json",
            clock=self.clock,
        )
        self.nodes_made.append(fake)
        return fake

    def code(self, *, core_fp: str | None = None, gw_fp: str | None = None) -> str:
        with psycopg.connect(self.dsns.app, autocommit=True) as conn:
            return nodes.create_enrolment(
                conn,
                person_id=MANJ,
                kind="DESKTOP",
                name="study pc",
                core_fp=core_fp or fingerprint(public_bytes(self.core_key)),
                gw_fp=gw_fp or fingerprint(public_bytes(self.gw_key)),
            )

    def enrolled(self, **kw: Any) -> FakeNode:
        fake = self.node(**kw)
        fake.enrol(self.code())
        return fake

    def identity_of(self, fake: FakeNode) -> str:
        return self.whois.table[fake.peer_ip]

    def gw_envelope(self, fake: FakeNode, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A gateway-signed envelope with the node's next expected gateway seq."""
        env = {
            "version": 1,
            "message_id": str(uuid.uuid4()),
            "node_id": str(fake.node_id or uuid.uuid4()),
            "seq": fake.next_gateway_seq,
            "sent_at": self.clock().isoformat(),
            "kind": kind,
            "payload": payload,
        }
        return sign_envelope(Purpose.GW_TRANSPORT, self.gw_key, self.gw_key_id, env)

    def authz(self, target: uuid.UUID, *, kind: str = "node.action") -> dict[str, Any]:
        now = self.clock()
        return make_authz(
            self.core_key,
            self.core_key_id,
            {
                "v": 1,
                "kind": kind,
                "action_kind": "news.show",
                "args": {},
                "target_node_id": str(target),
                "person_id": str(MANJ),
                "approval_id": None,
                "execution_key": None,
                "reply_id": str(uuid.uuid4()) if kind == "node.action" else None,
                "summary": "Delete a test file" if kind == "approval.card" else None,
                "issued_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=2)).isoformat(),
            },
        )


@contextmanager
def running_gateway(
    dsns: Dsns,
    tmp: Path,
    *,
    gw_key: Ed25519PrivateKey | None = None,
    core_key: Ed25519PrivateKey | None = None,
    advertised_core: Ed25519PrivateKey | None = None,
    bind_check: bool = False,
    recheck_interval: float = 0.1,
    dsn: str | None = None,
    clock: FakeClock | None = None,
    tick_interval: float = 0.05,
) -> Iterator[Gw]:
    clock = clock or FakeClock()
    whois = FakeWhoIs()
    gw_key = gw_key or Ed25519PrivateKey.generate()
    core_key = core_key or Ed25519PrivateKey.generate()
    served_core = advertised_core or core_key
    app = create_app(
        dsn=dsn or dsns.gateway,
        whois=whois,
        gw_key=gw_key,
        gw_key_id=fingerprint(public_bytes(gw_key)),
        core_public_keys={
            fingerprint(public_bytes(served_core)): public_bytes(served_core),
        },
        bind_check=bind_check,
        clock=clock,
        recheck_interval=recheck_interval,
        tick_interval=tick_interval,
    )
    with TestClient(PeerFromHeader(app)) as client:
        gw = Gw(dsns, client, clock, whois, gw_key, core_key, tmp, app=app)
        try:
            yield gw
        finally:
            for fake in gw.nodes_made:
                fake.close()


@pytest.fixture
def gw_client(gdb: Dsns, tmp_path: Path) -> Iterator[Gw]:
    with running_gateway(gdb, tmp_path) as gw:
        yield gw


@pytest.fixture
def enrolment_code(gw_client: Gw) -> str:
    return gw_client.code()


# ------------------------------------------------------------------------------ helpers


def _seq(dsns: Dsns) -> int:
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        row = conn.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


def _audit(dsns: Dsns, since: int, action: str | None = None) -> list[dict[str, Any]]:
    """Audit rows after `since`, read on a separate, new connection (R7-1)."""
    with (
        psycopg.connect(dsns.app, autocommit=True) as other,
        other.cursor(row_factory=psycopg.rows.dict_row) as cur,
    ):
        rows = cur.execute(
            "SELECT action::text, outcome::text, reason, entity_id, details FROM audit.event"
            " WHERE seq > %s ORDER BY seq",
            (since,),
        ).fetchall()
    return [r for r in rows if action is None or r["action"] == action]


def _refused(dsns: Dsns, since: int) -> list[dict[str, Any]]:
    return _audit(dsns, since, "CONNECT_REFUSED")


def _closed_with(fake: FakeNode, code: int = REFUSED, timeout: float = 2.0) -> None:
    with pytest.raises(WebSocketDisconnect) as e:
        fake.recv(timeout)
    assert e.value.code == code


def _wait_for(fn: Callable[[], Any], timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.02)


def _inbound(dsns: Dsns, message_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        return conn.execute(
            "SELECT node_id, person_id, kind FROM conv.inbound WHERE id = %s", (message_id,)
        ).fetchall()


def _jobs(dsns: Dsns, message_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        return conn.execute(
            "SELECT kind FROM ops.job WHERE payload->>'inbound_id' = %s", (str(message_id),)
        ).fetchall()


def _utterance(text: str = "show me the news") -> dict[str, Any]:
    return {"text": text, "screen_summary": None}


def _hello_payload(fake: FakeNode, **kw: Any) -> dict[str, Any]:
    assert fake.challenge is not None
    return {"nonce": fake.challenge["nonce"], "resume": [], **kw}


# ------------------------------------------------------------------------------ challenge


def test_challenge_is_first_message_and_signed(gw_client: Gw) -> None:
    fake = gw_client.node()
    with gw_client.client.websocket_connect(
        "/v1/node", headers={"x-test-peer-ip": fake.peer_ip}
    ) as ws:
        env = ws.receive_json()
    assert env["kind"] == "challenge" and env["seq"] == 1
    assert env["node_id"] == str(NIL_UUID)  # the explicit nil UUID (Manj ruling M2)
    payload = env["payload"]
    assert payload["key_id"] == gw_client.gw_key_id
    assert b64url_decode(payload["public_key"], 32) == public_bytes(gw_client.gw_key)
    assert verify_envelope(
        Purpose.GW_TRANSPORT, public_bytes(gw_client.gw_key), gw_client.gw_key_id, env
    )
    assert len(payload["nonce"]) == 43
    issued = datetime.fromisoformat(payload["issued_at"])
    expires = datetime.fromisoformat(payload["expires_at"])
    assert issued == gw_client.clock() and expires - issued == timedelta(seconds=30)


def test_nonce_unique_per_connection(gw_client: Gw) -> None:
    ip = gw_client.peer()
    nonces = set()
    for _ in range(50):
        with gw_client.client.websocket_connect("/v1/node", headers={"x-test-peer-ip": ip}) as ws:
            nonces.add(ws.receive_json()["payload"]["nonce"])
    assert len(nonces) == 50


# ------------------------------------------------------------------------------ two paths


def test_hello_path_checks_unchanged_by_enrolment(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    a.close()
    since = _seq(gw_client.dsns)
    # A different key, the same node ID and peer.
    impostor = gw_client.node(node_id=a.node_id, peer_ip=a.peer_ip)
    impostor.pinned_gateway = dict(a.pinned_gateway)
    impostor.connect()
    impostor.send("hello", _hello_payload(impostor))
    _closed_with(impostor)
    # The right key from another Tailscale identity.
    elsewhere = gw_client.node(key=a.key, node_id=a.node_id, state=a.state_path)
    elsewhere.connect()
    elsewhere.send("hello", _hello_payload(elsewhere))
    _closed_with(elsewhere)
    # Revoked.
    nodes.revoke(app_conn, a.node_id)  # type: ignore[arg-type]
    again = gw_client.node(key=a.key, state=a.state_path, peer_ip=a.peer_ip)
    again.connect()
    again.send("hello", _hello_payload(again))
    _closed_with(again)
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["bad_signature", "identity_mismatch", "revoked"]
    assert all(r["details"]["node_id"] == str(a.node_id) for r in rows)


def test_enrol_path_signed_by_other_key_refused(gw_client: Gw, enrolment_code: str) -> None:
    token, _, gw_fp = nodes.parse_enrolment_code(enrolment_code)
    fake = gw_client.node()
    fake.connect(enrol_gateway_fingerprint=gw_fp)
    assert fake.challenge is not None
    other = public_bytes(Ed25519PrivateKey.generate())
    since = _seq(gw_client.dsns)
    fake.send(
        "enrol",
        {"token": token, "public_key": b64(other), "nonce": fake.challenge["nonce"]},
        node_id=NIL_UUID,
    )
    _closed_with(fake)
    rows = _audit(gw_client.dsns, since)
    assert [(r["action"], r["reason"]) for r in rows] == [("CONNECT_REFUSED", "bad_signature")]
    assert "node_id" not in rows[0]["details"]


def test_enrol_with_registered_key_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    since = _seq(gw_client.dsns)
    twin = gw_client.node(key=a.key)
    with pytest.raises(WebSocketDisconnect) as e:
        twin.enrol(gw_client.code())
    assert e.value.code == REFUSED
    rows = [r for r in _audit(gw_client.dsns, since) if r["action"] != "ENROL_CREATE"]
    assert [(r["action"], r["outcome"], r["reason"]) for r in rows] == [
        ("ENROL", "REJECTED", "key_in_use")
    ]


def test_enrol_identity_from_connection_not_message(gw_client: Gw, app_conn: Conn) -> None:
    code = gw_client.code()
    token, _, gw_fp = nodes.parse_enrolment_code(code)
    fake = gw_client.node()
    fake.connect(enrol_gateway_fingerprint=gw_fp)
    assert fake.challenge is not None
    since = _seq(gw_client.dsns)
    fake.send(
        "enrol",
        {
            "token": token,
            "public_key": b64(fake.public),
            "nonce": fake.challenge["nonce"],
            "tailscale_identity": "ts:someone-else",
        },
        node_id=NIL_UUID,
    )
    _closed_with(fake)
    assert [r["reason"] for r in _refused(gw_client.dsns, since)] == ["malformed"]
    # The token is still unused: enrol properly and the row gets the whois identity.
    fresh = gw_client.node(key=fake.key, peer_ip=fake.peer_ip)
    node_id = fresh.enrol(code)
    row = app_conn.execute(
        "SELECT tailscale_identity FROM gw.node WHERE id = %s", (node_id,)
    ).fetchone()
    assert row == (gw_client.identity_of(fresh),)


def test_hello_with_nil_node_id_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    since = _seq(gw_client.dsns)
    a.connect()
    a.send("hello", _hello_payload(a), node_id=NIL_UUID)
    _closed_with(a)
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["malformed"]
    assert "node_id" not in rows[0]["details"]


def test_enrol_token_of_wrong_length_is_malformed(gw_client: Gw) -> None:
    fake = gw_client.node()
    _, _, gw_fp = nodes.parse_enrolment_code(gw_client.code())
    fake.connect(enrol_gateway_fingerprint=gw_fp)
    assert fake.challenge is not None
    since = _seq(gw_client.dsns)
    fake.send(
        "enrol",
        {"token": "a" * 20, "public_key": b64(fake.public), "nonce": fake.challenge["nonce"]},
        node_id=NIL_UUID,
    )
    _closed_with(fake)
    rows = _audit(gw_client.dsns, since)
    assert [(r["action"], r["reason"]) for r in rows] == [("CONNECT_REFUSED", "malformed")]


# ------------------------------------------------------------------------------ pinning


def test_enrol_welcome_carries_core_key_and_node_pins_it(gw_client: Gw) -> None:
    fake = gw_client.enrolled()
    assert fake.trusted_core == {gw_client.core_key_id: public_bytes(gw_client.core_key)}
    assert fake.node_id is not None
    fake.inject(
        gw_client.gw_envelope(fake, "node.action", {"authz": gw_client.authz(fake.node_id)})
    )
    assert len(fake.actions) == 1 and fake.rejected == []
    # A gateway signature under the core key ID is never a core signature (R6-3).
    forged = make_authz(
        gw_client.gw_key, gw_client.core_key_id, gw_client.authz(fake.node_id)["object"]
    )
    fake.inject(gw_client.gw_envelope(fake, "node.action", {"authz": forged}))
    assert len(fake.actions) == 1 and fake.rejected == [("node.action", "bad_signature")]


def test_enrol_with_substituted_core_key(gdb: Dsns, tmp_path: Path, app_conn: Conn) -> None:
    real_core = Ed25519PrivateKey.generate()
    with running_gateway(
        gdb, tmp_path, core_key=real_core, advertised_core=Ed25519PrivateKey.generate()
    ) as gw:
        fake = gw.node()
        with pytest.raises(Rejected) as e:
            fake.enrol(gw.code())
        assert e.value.reason == "core_fingerprint_mismatch"
        assert fake.discarded and fake.node_id is None and fake.trusted_core == {}
        assert fake.pinned_gateway == {} and not fake.state_path.exists()
        assert not fake.connected and fake.actions == []


def test_node_ignores_core_content_before_pinning(gw_client: Gw) -> None:
    fake = gw_client.node()
    _, _, gw_fp = nodes.parse_enrolment_code(gw_client.code())
    fake.connect(enrol_gateway_fingerprint=gw_fp)
    target = uuid.uuid4()
    card = {
        "outbox_id": str(uuid.uuid4()),
        "priority": "URGENT",
        "kind": "approval.card",
        "body": "Delete a test file?",
        "expires_at": None,
        "authz": gw_client.authz(target, kind="approval.card"),
    }
    fake.inject(gw_client.gw_envelope(fake, "outbox.item", card))
    fake.inject(gw_client.gw_envelope(fake, "node.action", {"authz": gw_client.authz(target)}))
    assert fake.rejected == [("outbox.item", "unauthorised"), ("node.action", "unauthorised")]
    assert fake.displays == {} and fake.actions == []


def test_node_refuses_challenge_from_unpinned_key(gdb: Dsns, tmp_path: Path) -> None:
    core = Ed25519PrivateKey.generate()
    with running_gateway(gdb, tmp_path, core_key=core) as gw:
        enrolled = gw.enrolled()
        enrolled.close()
        identity = gw.identity_of(enrolled)
    with running_gateway(gdb, tmp_path, core_key=core) as gw2:  # a new gateway key K2
        ip = gw2.peer(identity)
        fake = gw2.node(key=enrolled.key, peer_ip=ip, state=enrolled.state_path)
        since = _seq(gdb)
        with pytest.raises(Rejected) as e:
            fake.hello()
        assert e.value.reason == "unpinned_key"
        assert fake.rejected == [("challenge", "unpinned_key")] and not fake.connected
        time.sleep(0.2)
        assert _audit(gdb, since) == []  # the node sent nothing


def test_enrol_ends_with_welcome(gw_client: Gw, enrolment_code: str, app_conn: Conn) -> None:
    _, core_fp, gw_fp = nodes.parse_enrolment_code(enrolment_code)
    fake = gw_client.node()
    fake.connect(enrol_gateway_fingerprint=gw_fp)
    node_id = fake.enrol(enrolment_code)
    welcome = fake.received_envelopes[-1]
    assert welcome["kind"] == "welcome"
    block = welcome["payload"]["enrolment"]
    row = app_conn.execute("SELECT id, person_id FROM gw.node WHERE id = %s", (node_id,)).fetchone()
    assert row == (uuid.UUID(block["node_id"]), MANJ)
    assert fingerprint(b64url_decode(block["core_public_key"], 32) or b"") == core_fp
    assert fake.trusted_core == {block["core_key_id"]: public_bytes(gw_client.core_key)}
    assert fake.pinned_gateway == {gw_client.gw_key_id: public_bytes(gw_client.gw_key)}
    message_id = fake.send("utterance", _utterance())
    assert json.loads(fake.last_sent or "{}")["node_id"] == str(node_id)
    assert _wait_for(lambda: _inbound(gw_client.dsns, message_id)) == [(node_id, MANJ, "utterance")]
    connects = app_conn.execute(
        "SELECT count(*) FROM audit.event WHERE action = 'CONNECT' AND entity_id = %s", (node_id,)
    ).fetchone()
    assert connects == (1,)  # no second handshake on the enrolment connection


def test_reconnect_after_enrolment_uses_hello(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    again = gw_client.node(key=a.key, peer_ip=a.peer_ip, state=a.state_path)
    assert again.node_id == a.node_id
    welcome = again.hello()
    assert "enrolment" not in welcome
    assert welcome["session_id"] is None and welcome["summary"] == []


def test_enrol_refuses_keys_not_matching_fingerprints(gw_client: Gw, app_conn: Conn) -> None:
    # The code names a different gateway key: the device refuses the challenge itself.
    code = gw_client.code(gw_fp=fingerprint(public_bytes(Ed25519PrivateKey.generate())))
    fake = gw_client.node()
    since = _seq(gw_client.dsns)
    with pytest.raises(Rejected) as e:
        fake.enrol(code)
    assert e.value.reason == "fingerprint_mismatch" and not fake.connected
    time.sleep(0.2)
    assert _audit(gw_client.dsns, since) == []
    token, _, _ = nodes.parse_enrolment_code(code)
    row = app_conn.execute(
        "SELECT used_at FROM gw.enrolment WHERE token_hash = %s", (nodes.token_hash(token),)
    ).fetchone()
    assert row == (None,)


# ------------------------------------------------------------------------------ seq, nonce, expiry


def test_seq_checked_per_direction(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    a.hello()  # node 1; gateway challenge 1, welcome 2
    a.send("ping", {})  # node 2
    assert a.recv()["kind"] == "pong"  # gateway 3
    a.send_raw("not json")  # carries no seq
    assert a.recv()["payload"]["code"] == "malformed"  # gateway 4
    a.send("ping", {})  # node 3
    pong = a.recv()
    assert (pong["kind"], pong["seq"]) == ("pong", 5)
    since = _seq(gw_client.dsns)
    a.send("ping", {}, seq=6)  # continues the gateway's count, not the node's
    _closed_with(a)
    rows = _audit(gw_client.dsns, since, "DISCONNECT")
    assert [r["reason"] for r in rows] == ["bad_seq"]
    # The node side: a gap in the gateway's seq closes the connection.
    b = gw_client.enrolled()
    gap = gw_client.gw_envelope(b, "pong", {})
    gap["seq"] += 1
    gap = sign_envelope(Purpose.GW_TRANSPORT, gw_client.gw_key, gw_client.gw_key_id, gap)
    with pytest.raises(Rejected) as e:
        b.inject(gap)
    assert e.value.reason == "bad_seq" and not b.connected


def test_hello_with_other_connections_nonce_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    first = gw_client.node(key=a.key, peer_ip=a.peer_ip, state=a.state_path)
    second = gw_client.node(key=a.key, peer_ip=a.peer_ip, state=a.state_path)
    first.connect()
    second.connect()
    assert first.challenge is not None
    since = _seq(gw_client.dsns)
    second.send("hello", {"nonce": first.challenge["nonce"], "resume": []})
    _closed_with(second)
    assert [r["reason"] for r in _refused(gw_client.dsns, since)] == ["bad_nonce"]


def test_hello_after_challenge_expiry_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    a.connect()
    gw_client.clock.advance(31)
    since = _seq(gw_client.dsns)
    a.send("hello", _hello_payload(a))
    _closed_with(a)
    assert [r["reason"] for r in _refused(gw_client.dsns, since)] == ["challenge_expired"]


def test_second_hello_on_same_connection_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    a.hello()
    since = _seq(gw_client.dsns)
    a.send_raw(a.last_sent or "")  # the same hello again: its seq repeats
    _closed_with(a)
    assert [r["reason"] for r in _audit(gw_client.dsns, since, "DISCONNECT")] == ["bad_seq"]
    b = gw_client.enrolled()
    b.close()
    b.hello()
    since = _seq(gw_client.dsns)
    b.send("hello", _hello_payload(b))  # a fresh hello, seq 2: the nonce is consumed
    _closed_with(b)
    assert [r["reason"] for r in _audit(gw_client.dsns, since, "DISCONNECT")] == ["bad_nonce"]


def test_failed_first_reply_consumes_nonce(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    a.close()
    wrong = gw_client.node(node_id=a.node_id, peer_ip=a.peer_ip)
    wrong.pinned_gateway = dict(a.pinned_gateway)
    wrong.connect()
    since = _seq(gw_client.dsns)
    wrong.send("hello", _hello_payload(wrong))
    _closed_with(wrong)
    assert not wrong.connected  # one attempt per connection: nothing more can be sent on it
    rows = _audit(gw_client.dsns, since)
    assert [(r["action"], r["reason"]) for r in rows] == [("CONNECT_REFUSED", "bad_signature")]


def test_message_before_hello_refused(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    a.connect()
    since = _seq(gw_client.dsns)
    a.send("utterance", _utterance())
    _closed_with(a)
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["not_first"]
    assert rows[0]["details"] == {"tailscale_identity": gw_client.identity_of(a), "suppressed": "0"}


def test_nil_node_id_only_for_challenge_and_enrol(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    raw = a.envelope("utterance", _utterance(), node_id=NIL_UUID)
    a.send_raw(json.dumps(raw))
    reply = a.recv()
    assert (reply["kind"], reply["payload"]["code"]) == ("error", "malformed")
    assert _inbound(gw_client.dsns, uuid.UUID(raw["message_id"])) == []


def test_clock_skew_computed_by_gateway(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    a.close()

    def events() -> list[tuple[Any, ...]]:
        return app_conn.execute(
            "SELECT details FROM comm.event WHERE kind = 'node.clock_skew' AND subject_id = %s",
            (str(a.node_id),),
        ).fetchall()

    a.hello(sent_at=gw_client.clock() + timedelta(seconds=120))
    assert events() == [({"node_id": str(a.node_id), "skew_ms": "120000"},)]
    a.close()
    a.hello(clock_skew_ms=120000)  # the node's own figure is display-only
    assert len(events()) == 1


# ------------------------------------------------------------------------------ refusal audit


def test_each_handshake_refusal_audited_once(gw_client: Gw, app_conn: Conn) -> None:
    gw = gw_client
    a = gw.enrolled()
    a.close()
    revoked = gw.enrolled()
    revoked.close()
    nodes.revoke(app_conn, revoked.node_id)  # type: ignore[arg-type]
    since = _seq(gw.dsns)

    def attempt(fake: FakeNode, act: Callable[[FakeNode], None]) -> None:
        fake.connect()
        act(fake)
        _closed_with(fake)

    def clone(**kw: Any) -> FakeNode:
        base: dict[str, Any] = {"key": a.key, "state": a.state_path, "peer_ip": a.peer_ip}
        return gw.node(**{**base, **kw})

    # unknown_peer: whois knows nothing.
    stranger = gw.node(peer_ip="100.64.0.250")  # not in the whois table
    with pytest.raises(WebSocketDisconnect) as e:
        stranger.connect()
    assert e.value.code == REFUSED
    attempt(clone(), lambda f: f.send("utterance", _utterance()))  # not_first
    attempt(clone(), lambda f: f.send("hello", _hello_payload(f), seq=2))  # bad_seq
    attempt(clone(), lambda f: f.send("hello", {"nonce": "A" * 43, "resume": []}))  # bad_nonce
    exp = clone()
    exp.connect()
    gw.clock.advance(31)
    exp.send("hello", _hello_payload(exp))
    _closed_with(exp)  # challenge_expired
    impostor = gw.node(node_id=a.node_id, peer_ip=a.peer_ip)
    impostor.pinned_gateway = dict(a.pinned_gateway)
    attempt(impostor, lambda f: f.send("hello", _hello_payload(f)))  # bad_signature
    attempt(clone(peer_ip=gw.peer()), lambda f: f.send("hello", _hello_payload(f)))  # mismatch
    gone = gw.node(key=revoked.key, state=revoked.state_path, peer_ip=revoked.peer_ip)
    attempt(gone, lambda f: f.send("hello", _hello_payload(f)))  # revoked
    attempt(
        clone(), lambda f: f.send("hello", _hello_payload(f), node_id=uuid.uuid4())
    )  # unknown_node
    attempt(clone(), lambda f: f.send_raw("{"))  # malformed
    rows = _audit(gw.dsns, since)
    assert all(r["action"] == "CONNECT_REFUSED" and r["outcome"] == "REJECTED" for r in rows)
    assert sorted(r["reason"] for r in rows) == sorted(
        [
            "unknown_peer",
            "not_first",
            "bad_seq",
            "bad_nonce",
            "challenge_expired",
            "bad_signature",
            "identity_mismatch",
            "revoked",
            "unknown_node",
            "malformed",
        ]
    )
    by_code = {r["reason"]: r for r in rows}
    assert "tailscale_identity" not in by_code["unknown_peer"]["details"]
    for code in ("bad_signature", "identity_mismatch", "revoked"):
        assert "node_id" in by_code[code]["details"]
    for code in ("not_first", "bad_seq", "bad_nonce", "challenge_expired", "unknown_node"):
        assert "node_id" not in by_code[code]["details"]
    # Enrolment with a bad token: one ENROL REJECTED row from gw.enrol_node, no CONNECT_REFUSED.
    since = _seq(gw.dsns)
    bad = gw.node()
    code = gw.code()
    _, core_fp, gw_fp = nodes.parse_enrolment_code(code)
    with pytest.raises(WebSocketDisconnect) as e:
        bad.enrol(f"{'a' * 26}.{core_fp}.{gw_fp}")
    assert e.value.code == REFUSED
    rows = _audit(gw.dsns, since)
    assert [
        (r["action"], r["outcome"], r["reason"]) for r in rows if r["action"] != "ENROL_CREATE"
    ] == [("ENROL", "REJECTED", "bad_token")]


def test_unregistered_node_id_never_stored(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    a.connect()
    since = _seq(gw_client.dsns)
    a.send("hello", _hello_payload(a), node_id=uuid.uuid4())
    _closed_with(a)
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["unknown_node"]
    assert rows[0]["entity_id"] == NIL_UUID and "node_id" not in rows[0]["details"]


def test_repeated_refusals_bounded(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    a.close()
    impostor_key = Ed25519PrivateKey.generate()
    since = _seq(gw_client.dsns)
    for _ in range(100):
        f = gw_client.node(key=impostor_key, node_id=a.node_id, peer_ip=a.peer_ip)
        f.pinned_gateway = dict(a.pinned_gateway)
        f.connect()
        f.send("hello", _hello_payload(f))
        _closed_with(f)
    rows = _refused(gw_client.dsns, since)
    assert len(rows) == 1 and rows[0]["reason"] == "bad_signature"


# ------------------------------------------------------------------------------ P2-S3 and P2-E10


def test_unknown_tailscale_peer_closed(gw_client: Gw) -> None:
    fake = gw_client.node(peer_ip="100.64.0.251")  # not in the whois table
    since = _seq(gw_client.dsns)
    with pytest.raises(WebSocketDisconnect) as e:
        fake.connect()
    assert e.value.code == REFUSED
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["unknown_peer"]
    assert rows[0]["entity_id"] == NIL_UUID
    assert "tailscale_identity" not in rows[0]["details"] and "node_id" not in rows[0]["details"]


def test_bind_check_refuses_non_tailnet_local_address(gdb: Dsns, tmp_path: Path) -> None:
    with running_gateway(gdb, tmp_path, bind_check=True) as gw:  # TestClient's server: testserver
        fake = gw.node()
        since = _seq(gdb)
        with pytest.raises(WebSocketDisconnect) as e:
            fake.connect()
        assert e.value.code == REFUSED
        rows = _refused(gdb, since)
        assert [r["reason"] for r in rows] == ["unknown_peer"]
        assert "tailscale_identity" not in rows[0]["details"]


def test_tailscale_identity_mismatch_closed(gw_client: Gw) -> None:
    a = gw_client.node(peer_ip=gw_client.peer(f"ts:pc-{secrets.token_hex(4)}"))
    a.enrol(gw_client.code())
    a.close()
    moved = gw_client.node(key=a.key, state=a.state_path, peer_ip=gw_client.peer("ts:other"))
    since = _seq(gw_client.dsns)
    moved.connect()
    moved.send("hello", _hello_payload(moved))
    _closed_with(moved)
    rows = _refused(gw_client.dsns, since)
    assert [r["reason"] for r in rows] == ["identity_mismatch"]
    assert rows[0]["details"]["tailscale_identity"] == "ts:other"


def test_bad_signature_closes(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    since = _seq(gw_client.dsns)

    def tamper(env: dict[str, Any]) -> None:
        env["payload"]["text"] = "delete everything"

    message_id = a.send("utterance", _utterance(), mutate=tamper)
    _closed_with(a)
    assert [r["reason"] for r in _audit(gw_client.dsns, since, "DISCONNECT")] == ["bad_signature"]
    assert _inbound(gw_client.dsns, message_id) == []


def test_replayed_seq_closes(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    first = a.send("utterance", _utterance("one"))
    _wait_for(lambda: _inbound(gw_client.dsns, first))
    since = _seq(gw_client.dsns)
    second = a.send("utterance", _utterance("two"), seq=a._seq)  # the same seq again
    _closed_with(a)
    assert [r["reason"] for r in _audit(gw_client.dsns, since, "DISCONNECT")] == ["bad_seq"]
    assert _inbound(gw_client.dsns, second) == []


def test_revoked_node_refused_and_open_connection_closed(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    nodes.revoke(app_conn, a.node_id)  # type: ignore[arg-type]
    _closed_with(a)  # the re-check (0.1 s here, 60 s in service) closes the open socket
    since = _seq(gw_client.dsns)
    again = gw_client.node(key=a.key, state=a.state_path, peer_ip=a.peer_ip)
    again.connect()
    again.send("hello", _hello_payload(again))
    _closed_with(again)
    rows = _refused(gw_client.dsns, since)
    assert [(r["reason"], r["details"]["node_id"]) for r in rows] == [("revoked", str(a.node_id))]


# ------------------------------------------------------------------------------ inbound


def test_utterance_recorded_and_job_enqueued(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    message_id = a.send("utterance", _utterance())
    assert _wait_for(lambda: _inbound(gw_client.dsns, message_id)) == [
        (a.node_id, MANJ, "utterance")
    ]
    assert _jobs(gw_client.dsns, message_id) == [("conv.turn",)]


def test_resend_same_message_id_is_noop(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    message_id = a.send("utterance", _utterance())
    a.send("utterance", _utterance(), message_id=message_id)
    a.send("ping", {})
    assert a.recv()["kind"] == "pong"  # both utterances were handled before the ping
    assert len(_inbound(gw_client.dsns, message_id)) == 1
    assert _jobs(gw_client.dsns, message_id) == [("conv.turn",)]


def test_oversized_message_gets_error_and_stays_open(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    big = a.envelope("utterance", {"text": "x" * 70_000, "screen_summary": None})
    a.send_raw(json.dumps(big))
    reply = a.recv()
    assert (reply["kind"], reply["payload"]["code"]) == ("error", "too_large")
    message_id = a.send("utterance", _utterance())
    assert _wait_for(lambda: _inbound(gw_client.dsns, message_id))


def test_malformed_and_unknown_kind_get_error(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    bogus = a.envelope("utterance", _utterance())
    bogus["kind"] = "bogus"
    for text in ("{not json", json.dumps(bogus)):
        a.send_raw(text)
        reply = a.recv()
        assert (reply["kind"], reply["payload"]["code"]) == ("error", "malformed")
    missing_id = a.send("utterance", {"screen_summary": None})  # signed, but no text
    reply = a.recv()
    assert (reply["kind"], reply["payload"]["code"]) == ("error", "malformed")
    a.send("welcome", {"session_id": None, "summary": []})  # a gateway-only kind
    reply = a.recv()
    assert (reply["kind"], reply["payload"]["code"]) == ("error", "unexpected_kind")
    assert _inbound(gw_client.dsns, missing_id) == []
    assert _inbound(gw_client.dsns, uuid.UUID(bogus["message_id"])) == []
    ok = a.send("utterance", _utterance())
    assert _wait_for(lambda: _inbound(gw_client.dsns, ok))


def test_received_and_ack_of_unknown_item_refused_and_open(gw_client: Gw) -> None:
    """Task 7 wires `received` and `ack` to comm.*: an unknown item is refused
    (`unknown_item`, audited by the function) and the connection stays open."""
    a = gw_client.enrolled()
    a.received(uuid.uuid4())
    a.ack(uuid.uuid4())
    a.send("ping", {})
    kinds = [(e["kind"], e["payload"].get("code")) for e in (a.recv(), a.recv(), a.recv())]
    assert kinds == [("error", "unknown_item"), ("error", "unknown_item"), ("pong", None)]


def test_ping_touches_connection(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    a.send("ping", {})
    pong = a.recv()
    assert pong["kind"] == "pong" and pong["node_id"] == str(a.node_id)
    row = app_conn.execute(
        "SELECT last_seen_at IS NOT NULL FROM gw.node WHERE id = %s", (a.node_id,)
    ).fetchone()
    assert row == (True,)


def test_node_close_closes_connection_row(gw_client: Gw, app_conn: Conn) -> None:
    a = gw_client.enrolled()
    # Past the connect-time delivery first: TestClient cancels the app as soon as the client
    # closes, which a real server doesn't, so a close mid-delivery would test TestClient.
    a.send("ping", {})
    assert a.recv()["kind"] == "pong"
    a.close()

    def closed() -> Any:
        return app_conn.execute(
            "SELECT close_reason FROM gw.node_connection WHERE node_id = %s AND closed_at"
            " IS NOT NULL",
            (a.node_id,),
        ).fetchall()

    assert _wait_for(closed) == [("node_closed",)]


def test_seen_list_survives_a_new_fake_node(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    item = {
        "outbox_id": str(uuid.uuid4()),
        "priority": "TODAY",
        "kind": "reminder",
        "body": "Call the clinic",
        "expires_at": None,
        "authz": None,
    }
    a.inject(gw_client.gw_envelope(a, "outbox.item", item))
    a.inject(gw_client.gw_envelope(a, "outbox.item", item))
    assert a.displays[item["outbox_id"]] == 1
    a.close()
    b = gw_client.node(key=a.key, peer_ip=a.peer_ip, state=a.state_path)
    b.hello()
    b.inject(gw_client.gw_envelope(b, "outbox.item", item))
    assert b.displays[item["outbox_id"]] == 0 and item["outbox_id"] in b.seen_outbox


# ------------------------------------------------------------------------------ startup


def test_gateway_role_only(gdb: Dsns, tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="jarvis_gateway"),
        running_gateway(gdb, tmp_path, dsn=gdb.app),
    ):
        pass


def test_gateway_never_binds_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway_app.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setenv("JARVIS_TS_SOCKET", "/nonexistent/tailscaled.sock")
    monkeypatch.setattr(gateway_app, "_build_app", lambda socket_path: object())
    for address in (None, "0.0.0.0", "192.168.1.5", "::"):  # noqa: S104
        monkeypatch.setattr(gateway_app, "tailnet_ipv4", lambda socket_path, a=address: a)
        assert gateway_app.main() != 0
    assert calls == []
    monkeypatch.delenv("JARVIS_TS_SOCKET")
    assert gateway_app.main() != 0 and calls == []
    monkeypatch.setenv("JARVIS_TS_SOCKET", "/nonexistent/tailscaled.sock")
    monkeypatch.setattr(gateway_app, "tailnet_ipv4", lambda socket_path: "100.101.102.103")
    assert gateway_app.main() == 0
    assert [(c["host"], c["port"]) for c in calls] == [("100.101.102.103", 8443)]


# ------------------------------------------------------------------------------ fix round 1 (I-1)


def _connections(dsns: Dsns, node_id: uuid.UUID | None) -> list[tuple[Any, ...]]:
    """(closed?, close_reason) of the node's connection rows, read on a new connection."""
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        return conn.execute(
            "SELECT closed_at IS NOT NULL, close_reason FROM gw.node_connection"
            " WHERE node_id = %s ORDER BY id",
            (node_id,),
        ).fetchall()


def _closed_after_db_error(gw: Gw, a: FakeNode, since: int) -> None:
    reply = a.recv()
    assert (reply["kind"], reply["payload"]["code"]) == ("error", "server_error")
    _closed_with(a)
    rows = _audit(gw.dsns, since, "DISCONNECT")
    assert [(r["reason"], r["details"]["node_id"]) for r in rows] == [
        ("server_error", str(a.node_id))
    ]
    assert _connections(gw.dsns, a.node_id) == [(True, "server_error")]


def test_db_error_on_inbound_closes_connection(gw_client: Gw) -> None:
    a = gw_client.enrolled()
    since = _seq(gw_client.dsns)
    # Valid for the Utterance schema, but Postgres jsonb refuses U+0000.
    message_id = a.send("utterance", _utterance("a\u0000b"))
    _closed_after_db_error(gw_client, a, since)
    assert _inbound(gw_client.dsns, message_id) == []  # the failed call left nothing behind


def test_generic_db_error_closes_connection(gw_client: Gw, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_server.gateway import session as gateway_session

    real = gateway_session.audited_call_async

    async def failing(conn: Any, sql: Any, params: Any) -> Any:
        if "record_inbound" in sql:
            raise psycopg.errors.InternalError("injected")
        return await real(conn, sql, params)

    a = gw_client.enrolled()
    monkeypatch.setattr(gateway_session, "audited_call_async", failing)
    since = _seq(gw_client.dsns)
    a.send("utterance", _utterance())
    _closed_after_db_error(gw_client, a, since)
