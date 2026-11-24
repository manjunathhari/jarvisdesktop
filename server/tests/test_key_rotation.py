"""Key trust and rotation (spec §6.1, §7.1, §7.2; R5-1, R6-2, R6-3, R6-6, R12-23, R12-26,
R12-27; plan Task 12).

- `gw.trusted_key` (migration 0011) holds the CORE and GATEWAY public keys: NEXT, ACTIVE (one per
  role) or RETIRED. `gw.node.key_state` is CURRENT or STALE.
- `core.keys.announce` writes one `keys.keyset` outbox item per active node, targeted at it and
  signed by the ACTIVE core key. The gateway sends it as a `keyset` message carrying the
  item's unsigned `outbox_id`. The node acks it only after `check_keyset` accepts it.
- `activate_ready` activates a NEXT key once every item is acknowledged, or after 24 h. In the
  same transaction it marks every node that didn't acknowledge as STALE.
- The gateway refuses a STALE node's `hello` with 4409, sending nothing after the challenge.
- `jarvis-core node reenrol` recovers a STALE node.

Runs in its own database. Every person is fictional, and every key is a throwaway test key.
Time is injected: the gateway's and the nodes' FakeClock, and `activate_ready(now=...)`.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.websockets import WebSocketDisconnect

from jarvis_server import comm, config, core_main, migrate, nodes, signing
from jarvis_server.comm import Priority
from jarvis_server.core import dispatch, keys, rules
from jarvis_server.signing import fingerprint, make_authz, public_bytes

from .conftest import Dsns, fresh_database
from .fakes.fake_node import FakeNode, b64
from .fakes.node_rules import Rejected
from .helpers import flow
from .test_gateway_handshake import MANJ, FakeClock, Gw, _wait_for, running_gateway

Conn = psycopg.Connection[Any]
REENROL = 4409
DAY = timedelta(hours=24)


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with fresh_database() as d:
        yield d


@pytest.fixture(autouse=True)
def _clean(dsns: Dsns) -> Iterator[None]:
    """Each test starts with no trusted keys, no active node and an empty refusal tally."""
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        conn.execute("DELETE FROM gw.trusted_key")
        conn.execute("UPDATE gw.node SET revoked_at = now() WHERE revoked_at IS NULL")
        conn.execute("DELETE FROM gw.refusal_tally")
        conn.execute(
            "UPDATE gw.refusal_window SET window_start = '2000-01-01 00:00+00', audited = 0"
        )
    yield


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _id(key: Ed25519PrivateKey) -> str:
    return fingerprint(public_bytes(key))


@dataclass
class Keys:
    """The secrets directory (core's and the gateway's) and the keys placed in it."""

    secrets: Path
    core: Ed25519PrivateKey
    gw: Ed25519PrivateKey

    def place(self, name: str, key: Ed25519PrivateKey) -> None:
        (self.secrets / name).write_bytes(_pem(key))


@pytest.fixture
def ks(dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Keys:
    """The bootstrap keys, as `keys init` leaves them: the legacy secrets `core_signing_key` and
    `gateway_signing_key` (+ `.pub`), and both public keys ACTIVE in gw.trusted_key."""
    directory = tmp_path / "secrets"
    directory.mkdir()
    monkeypatch.setattr(config, "SECRETS_DIR", directory)
    k = Keys(directory, Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate())
    k.place("core_signing_key", k.core)
    k.place("gateway_signing_key", k.gw)
    (directory / "gateway_signing_key.pub").write_bytes(public_bytes(k.gw))
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        keys.init_keys(conn, core_public=public_bytes(k.core), gateway_public=public_bytes(k.gw))
    return k


@contextmanager
def gateway(
    dsns: Dsns, ks: Keys, tmp: Path, clock: FakeClock, *,
    as_key: Ed25519PrivateKey | None = None, refresh: float = 0.05,
) -> Iterator[Gw]:  # fmt: skip
    """A gateway that loads its keys from gw.trusted_key and the secrets directory, re-reading
    them every `refresh` real seconds. `as_key` is only what the test signs injected gateway
    frames with (the gateway's own ACTIVE key)."""
    with running_gateway(
        dsns, tmp, gw_key=as_key or ks.gw, core_key=ks.core, install_keys=False,
        secrets_dir=ks.secrets, clock=clock, key_refresh_interval=refresh,
    ) as gw:  # fmt: skip
        yield gw


def _clock(app: Conn) -> FakeClock:
    return FakeClock(flow.db_now(app))


def _code(app: Conn, person: uuid.UUID = MANJ) -> str:
    core_fp, gw_fp = core_main._key_fingerprints(app)
    return nodes.create_enrolment(
        app, person_id=person, kind="DESKTOP", name="study pc", core_fp=core_fp, gw_fp=gw_fp
    )


_IDENTITY: dict[str, str] = {}  # peer address -> Tailscale identity, across gateways


def _enrolled(gw: Gw, app: Conn) -> FakeNode:
    fake = gw.node()
    _IDENTITY[fake.peer_ip] = gw.identity_of(fake)
    fake.enrol(_code(app))
    fake.sync()
    return fake


def _again(gw: Gw, fake: FakeNode) -> FakeNode:
    """The same device (key, address, identity, state file) as a new FakeNode on `gw`."""
    gw.whois.table[fake.peer_ip] = _IDENTITY[fake.peer_ip]
    return gw.node(key=fake.key, peer_ip=fake.peer_ip, state=fake.state_path)


def _device(gw: Gw, old: FakeNode) -> FakeNode:
    """A re-enrolling device: the old one's address and identity, a new key and state file."""
    gw.whois.table[old.peer_ip] = _IDENTITY[old.peer_ip]
    return gw.node(peer_ip=old.peer_ip)


def _row(app: Conn, key_id: str) -> dict[str, Any]:
    with app.cursor(row_factory=psycopg.rows.dict_row) as cur:
        row = cur.execute("SELECT * FROM gw.trusted_key WHERE key_id = %s", (key_id,)).fetchone()
    assert row is not None
    return row


def _state(app: Conn, node_id: uuid.UUID | None) -> str:
    row = app.execute("SELECT key_state FROM gw.node WHERE id = %s", (node_id,)).fetchone()
    assert row is not None
    return str(row[0])


def _item_of(app: Conn, key_id: str, node_id: uuid.UUID | None) -> dict[str, Any]:
    with app.cursor(row_factory=psycopg.rows.dict_row) as cur:
        row = cur.execute(
            "SELECT o.* FROM comm.outbox o"
            " JOIN gw.trusted_key k ON o.id = ANY(k.announced_outbox_ids)"
            " WHERE k.key_id = %s AND o.target_node_id = %s",
            (key_id, node_id),
        ).fetchone()
    assert row is not None
    return row


def _acked(app: Conn, key_id: str, fake: FakeNode) -> bool:
    fake.drain(0.02)
    return _item_of(app, key_id, fake.node_id)["state"] == "ACKNOWLEDGED"


def _action(
    key: Ed25519PrivateKey, key_id: str, target: uuid.UUID, now: datetime
) -> dict[str, Any]:
    return make_authz(key, key_id, {
        "v": 1, "kind": "node.action", "action_kind": "news.show", "args": {},
        "target_node_id": str(target), "person_id": str(MANJ), "approval_id": None,
        "execution_key": None, "reply_id": str(uuid.uuid4()), "summary": None,
        "issued_at": now.isoformat(), "valid_until": (now + timedelta(minutes=2)).isoformat(),
    })  # fmt: skip


def _keyset(
    signer: Ed25519PrivateKey, signer_id: str, new: Ed25519PrivateKey, now: datetime,
    *, role: str = "core", target: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    """A `keyset` payload (with its unsigned outbox_id) as the gateway sends it."""
    args = {"role": role, "key_id": _id(new), "public_key": b64(public_bytes(new))}
    authz = make_authz(signer, signer_id, {
        "v": 1, "kind": "keyset", "action_kind": "keyset.add", "args": args,
        "target_node_id": None if target is None else str(target), "person_id": str(MANJ),
        "approval_id": None, "execution_key": None, "reply_id": None, "summary": None,
        "issued_at": now.isoformat(), "valid_until": (now + DAY).isoformat(),
    })  # fmt: skip
    return {"outbox_id": str(uuid.uuid4()), **args, "authz": authz}


def _created(app: Conn, key_id: str) -> datetime:
    created: datetime = _row(app, key_id)["created_at"]
    return created


def _audit_refusals(dsns: Dsns, since: int) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.app, autocommit=True) as conn:
        return conn.execute(
            "SELECT action::text, outcome::text, reason, entity_id FROM audit.event"
            " WHERE seq > %s AND action::text = 'CONNECT_REFUSED' ORDER BY seq",
            (since,),
        ).fetchall()


def _connections(app: Conn, node_id: uuid.UUID | None) -> int:
    row = app.execute(
        "SELECT count(*) FROM gw.node_connection WHERE node_id = %s", (node_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seq(app: Conn) -> int:
    row = app.execute("SELECT coalesce(max(seq), 0) FROM audit.event").fetchone()
    assert row is not None
    return int(row[0])


def _person_item(app: Conn, text: str = "Claude budget 80% used this month") -> uuid.UUID:
    """A person-addressed TODAY item for MANJ, as a rule writes it."""
    event = comm.record_event(
        app, "budget.80", subject_type="budget", subject_id="2026-11", person_id=MANJ, details={}
    )
    (item,) = rules.notify(app, "budget.80", person_id=MANJ, event_id=event, context={})
    return item


# ------------------------------------------------------------------------------ the table


def test_one_active_key_per_role(app: Conn, ks: Keys) -> None:
    other = public_bytes(Ed25519PrivateKey.generate())
    with pytest.raises(psycopg.errors.UniqueViolation):
        app.execute(
            "INSERT INTO gw.trusted_key (key_id, role, public_key, state)"
            " VALUES (%s, 'CORE', %s, 'ACTIVE')",
            (fingerprint(other), other),
        )
    # A GATEWAY key is another role; NEXT keys don't count.
    app.execute(
        "INSERT INTO gw.trusted_key (key_id, role, public_key, state) VALUES (%s, 'CORE', %s,"
        " 'NEXT')",
        (fingerprint(other), other),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):  # one key, one row
        app.execute(
            "INSERT INTO gw.trusted_key (key_id, role, public_key, state) VALUES (%s, 'GATEWAY',"
            " %s, 'NEXT')",
            ("a" * 16, other),
        )


def test_gateway_cannot_write_trusted_keys(gateway: Conn, ks: Keys) -> None:
    row = gateway.execute("SELECT count(*) FROM gw.trusted_key").fetchone()
    assert row == (2,)
    other = public_bytes(Ed25519PrivateKey.generate())
    for sql, params in (
        ("INSERT INTO gw.trusted_key (key_id, role, public_key, state)"
         " VALUES (%s, 'GATEWAY', %s, 'NEXT')", (fingerprint(other), other)),
        ("UPDATE gw.trusted_key SET state = 'RETIRED'", ()),
        ("DELETE FROM gw.trusted_key", ()),
        ("UPDATE gw.node SET key_state = 'CURRENT'", ()),
    ):  # fmt: skip
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gateway.execute(sql, params)


def test_key_state_defaults_current_and_is_checked(app: Conn, owner: Conn) -> None:
    node = flow.new_node(owner, MANJ)
    assert _state(app, node) == "CURRENT"
    with pytest.raises(psycopg.errors.CheckViolation):
        app.execute("UPDATE gw.node SET key_state = 'LOST' WHERE id = %s", (node,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # only key_state is app-writable
        app.execute("UPDATE gw.node SET name = 'renamed' WHERE id = %s", (node,))


# ------------------------------------------------------------------------------ bootstrap


def test_keys_init_is_idempotent_and_refuses_a_different_active_key(app: Conn) -> None:
    core, gw = (
        public_bytes(Ed25519PrivateKey.generate()),
        public_bytes(Ed25519PrivateKey.generate()),
    )
    assert keys.init_keys(app, core_public=core, gateway_public=gw) == [
        fingerprint(core),
        fingerprint(gw),
    ]
    assert keys.init_keys(app, core_public=core, gateway_public=gw) == []  # nothing new
    for role in ("CORE", "GATEWAY"):
        row = app.execute(
            "SELECT key_id, state, activated_at IS NOT NULL FROM gw.trusted_key WHERE role = %s",
            (role,),
        ).fetchone()
        assert row == (fingerprint(core if role == "CORE" else gw), "ACTIVE", True)
    other = public_bytes(Ed25519PrivateKey.generate())
    with pytest.raises(keys.KeyRefused) as e:
        keys.init_keys(app, core_public=other, gateway_public=gw)
    assert e.value.reason == "active_key_differs"
    row = app.execute("SELECT count(*) FROM gw.trusted_key").fetchone()
    assert row == (2,)  # nothing written, not even the matching gateway row twice


def test_active_core_key_uses_the_secret_named_for_its_key_id(app: Conn, ks: Keys) -> None:
    key, key_id = keys.active_core_key(app)
    assert key_id == _id(ks.core) and public_bytes(key) == public_bytes(ks.core)
    # The named secret wins over the legacy one.
    (ks.secrets / "core_signing_key").unlink()
    ks.place(f"core_signing_key_{_id(ks.core)}", ks.core)
    assert keys.active_core_key(app)[1] == _id(ks.core)


def test_legacy_secret_only_when_its_fingerprint_is_the_active_key(app: Conn, ks: Keys) -> None:
    ks.place("core_signing_key", Ed25519PrivateKey.generate())  # not the ACTIVE key
    with pytest.raises(RuntimeError, match="core_signing_key"):
        keys.active_core_key(app)
    (ks.secrets / "core_signing_key").unlink()
    with pytest.raises(RuntimeError, match="core_signing_key"):
        keys.active_core_key(app)
    # A named secret that holds another key is refused too.
    ks.place(f"core_signing_key_{_id(ks.core)}", Ed25519PrivateKey.generate())
    with pytest.raises(RuntimeError, match="does not match"):
        keys.active_core_key(app)


def test_active_core_key_without_an_active_row_fails(app: Conn) -> None:
    with pytest.raises(RuntimeError, match="no ACTIVE CORE key"):
        keys.active_core_key(app)


def test_enrolment_fingerprints_come_from_trusted_keys(app: Conn, ks: Keys) -> None:
    assert core_main._key_fingerprints(app) == (_id(ks.core), _id(ks.gw))


# ------------------------------------------------------------------------------ announce


def test_announce_writes_one_targeted_keyset_per_active_node(
    app: Conn, owner: Conn, ks: Keys
) -> None:
    a, b = flow.new_node(owner, MANJ), flow.new_node(owner, MANJ)
    revoked = flow.new_node(owner, MANJ)
    nodes.revoke(app, revoked)
    stale = flow.new_node(owner, MANJ)
    app.execute("UPDATE gw.node SET key_state = 'STALE' WHERE id = %s", (stale,))
    new = Ed25519PrivateKey.generate()
    key_id = keys.announce(app, role="CORE", public_key=public_bytes(new))
    assert key_id == _id(new)
    row = _row(app, key_id)
    assert row["state"] == "NEXT" and row["role"] == "CORE" and row["activated_at"] is None
    assert len(row["announced_outbox_ids"]) == 2
    trusted = {_id(ks.core): public_bytes(ks.core)}
    for node in (a, b):
        item = _item_of(app, key_id, node)
        assert item["kind"] == "keys.keyset" and item["priority"] == Priority.SILENT.value
        assert item["group_key"] == "keys" and item["person_id"] == MANJ
        obj = signing.check_authz(item["authz"], trusted)  # signed by the ACTIVE core key
        assert obj["kind"] == "keyset" and obj["action_kind"] == "keyset.add"
        assert obj["args"] == {
            "role": "core",
            "key_id": key_id,
            "public_key": b64(public_bytes(new)),
        }
        assert obj["target_node_id"] == str(node) and obj["person_id"] == str(MANJ)
        issued = datetime.fromisoformat(obj["issued_at"])
        assert datetime.fromisoformat(obj["valid_until"]) == issued + DAY
        assert issued == row["created_at"] and item["expires_at"] == issued + DAY
    # A second NEXT key for the same role, or a known key, is refused.
    with pytest.raises(keys.KeyRefused) as e:
        keys.announce(app, role="CORE", public_key=public_bytes(Ed25519PrivateKey.generate()))
    assert e.value.reason == "next_pending"
    with pytest.raises(keys.KeyRefused) as e:
        keys.announce(app, role="GATEWAY", public_key=public_bytes(ks.gw))
    assert e.value.reason == "known_key"


def test_gateway_keyset_is_signed_by_the_core_key(app: Conn, owner: Conn, ks: Keys) -> None:
    node = flow.new_node(owner, MANJ)
    new = Ed25519PrivateKey.generate()
    key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
    authz = _item_of(app, key_id, node)["authz"]
    assert authz["key_id"] == _id(ks.core)
    obj = signing.check_authz(authz, {_id(ks.core): public_bytes(ks.core)})
    assert obj["args"]["role"] == "gateway"
    with pytest.raises(signing.AuthzInvalid):  # never the gateway's own key (R6-3)
        signing.check_authz(authz, {_id(ks.core): public_bytes(ks.gw)})


# ------------------------------------------------------------------------------ activation


def _ack_as_gateway(dsns: Dsns, node: uuid.UUID, item: uuid.UUID) -> None:
    with psycopg.connect(dsns.gateway, autocommit=True) as gw:
        row = gw.execute("SELECT conn_id FROM gw.open_connection(%s, NULL)", (node,)).fetchone()
        assert row is not None
        gw.execute("SELECT comm.record_sent(%s, %s)", (row[0], item))
        assert gw.execute("SELECT comm.acknowledge(%s, %s)", (row[0], item)).fetchone() == (
            "ACKNOWLEDGED",
        )
        gw.execute("SELECT gw.close_connection(%s, 'node_closed')", (row[0],))


def test_activation_waits_for_all_acks_or_24h(app: Conn, owner: Conn, dsns: Dsns, ks: Keys) -> None:
    a, b = flow.new_node(owner, MANJ), flow.new_node(owner, MANJ)
    new = Ed25519PrivateKey.generate()
    key_id = keys.announce(app, role="CORE", public_key=public_bytes(new))
    t0 = _created(app, key_id)
    assert keys.activate_ready(app, t0 + timedelta(hours=1)) == []
    _ack_as_gateway(dsns, a, _item_of(app, key_id, a)["id"])
    assert keys.activate_ready(app, t0 + timedelta(hours=22, minutes=59)) == []  # b hasn't
    assert _row(app, key_id)["state"] == "NEXT" and _state(app, b) == "CURRENT"
    # R12-28: at 23 h, an hour before nodes drop the old key.
    assert keys.activate_ready(app, t0 + timedelta(hours=23)) == [key_id]
    assert _row(app, key_id)["state"] == "ACTIVE"
    assert _row(app, key_id)["activated_at"] == t0 + timedelta(hours=23)
    old = _row(app, _id(ks.core))
    assert old["state"] == "RETIRED" and old["retired_at"] is None  # retired, pending
    assert (_state(app, a), _state(app, b)) == ("CURRENT", "STALE")
    assert keys.activate_ready(app, t0 + DAY * 2) == []  # nothing left to do
    # Core now signs with the new key, and still verifies with the retired-pending one.
    ks.place(f"core_signing_key_{key_id}", new)
    assert keys.active_core_key(app)[1] == key_id
    assert set(keys.trusted_core_keys(app)) == {key_id, _id(ks.core)}
    keys.retire(app, _id(ks.core))
    assert _row(app, _id(ks.core))["retired_at"] is not None
    assert set(keys.trusted_core_keys(app)) == {key_id}


def test_activation_as_soon_as_every_node_acked(
    app: Conn, owner: Conn, dsns: Dsns, ks: Keys
) -> None:
    a, b = flow.new_node(owner, MANJ), flow.new_node(owner, MANJ)
    revoked = flow.new_node(owner, MANJ)
    key_id = keys.announce(
        app, role="GATEWAY", public_key=public_bytes(Ed25519PrivateKey.generate())
    )
    nodes.revoke(app, revoked)  # never acknowledges; doesn't hold activation back
    for node in (a, b):
        _ack_as_gateway(dsns, node, _item_of(app, key_id, node)["id"])
    t0 = _created(app, key_id)
    assert keys.activate_ready(app, t0 + timedelta(minutes=1)) == [key_id]
    assert (_state(app, a), _state(app, b)) == ("CURRENT", "CURRENT")
    assert _row(app, _id(ks.gw))["state"] == "RETIRED"
    assert _row(app, _id(ks.core))["state"] == "ACTIVE"  # the other role is untouched


def test_retire_rules(app: Conn, owner: Conn, ks: Keys) -> None:
    node = flow.new_node(owner, MANJ)
    with pytest.raises(keys.KeyRefused) as e:
        keys.retire(app, _id(ks.core))
    assert e.value.reason == "active"
    with pytest.raises(keys.KeyRefused) as e:
        keys.retire(app, "a" * 16)
    assert e.value.reason == "unknown_key"
    # A NEXT key is never withdrawn (fix round 1, I-2): refused, and nothing changes.
    key_id = keys.announce(app, role="CORE", public_key=public_bytes(Ed25519PrivateKey.generate()))
    before = (_row(app, key_id), _item_of(app, key_id, node))
    with pytest.raises(keys.KeyRefused) as e:
        keys.retire(app, key_id)
    assert e.value.reason == "next_pending"
    assert (_row(app, key_id), _item_of(app, key_id, node)) == before
    # A retired-pending key is ended once.
    assert keys.activate_ready(app, _created(app, key_id) + DAY) == [key_id]
    keys.retire(app, _id(ks.core))
    assert _row(app, _id(ks.core))["retired_at"] is not None
    with pytest.raises(keys.KeyRefused) as e:
        keys.retire(app, _id(ks.core))
    assert e.value.reason == "already_retired"


def test_one_rotation_at_a_time(app: Conn, owner: Conn, ks: Keys) -> None:
    """Fix round 1, M-4: while either role has a NEXT key, the other can't be announced."""
    flow.new_node(owner, MANJ)
    gw_key = keys.announce(
        app, role="GATEWAY", public_key=public_bytes(Ed25519PrivateKey.generate())
    )
    count = app.execute("SELECT count(*) FROM gw.trusted_key").fetchone()
    items = app.execute("SELECT count(*) FROM comm.outbox WHERE kind = 'keys.keyset'").fetchone()
    with pytest.raises(keys.KeyRefused) as e:
        keys.announce(app, role="CORE", public_key=public_bytes(Ed25519PrivateKey.generate()))
    assert e.value.reason == "rotation_pending"
    assert app.execute("SELECT count(*) FROM gw.trusted_key").fetchone() == count
    assert app.execute(
        "SELECT count(*) FROM comm.outbox WHERE kind = 'keys.keyset'"
    ).fetchone() == items  # fmt: skip
    assert keys.activate_ready(app, _created(app, gw_key) + DAY) == [gw_key]
    assert keys.announce(app, role="CORE", public_key=public_bytes(Ed25519PrivateKey.generate()))


def test_keys_activate_job_runs_activate_ready(app: Conn, owner: Conn, ks: Keys) -> None:
    key_id = keys.announce(app, role="CORE", public_key=public_bytes(Ed25519PrivateKey.generate()))
    assert "keys.activate" in dispatch._HANDLERS
    flow.run(app, "keys.activate", {})  # no active node: every item (none) is acknowledged
    assert _row(app, key_id)["state"] == "ACTIVE"


# ------------------------------------------------------------------------------ the gateway


def test_gateway_refuses_start_without_active_secret(dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    (ks.secrets / "gateway_signing_key").unlink()
    with (
        pytest.raises(RuntimeError, match="gateway_signing_key"),
        gateway(dsns, ks, tmp_path, FakeClock()),
    ):
        pass
    ks.place("gateway_signing_key", Ed25519PrivateKey.generate())  # a key that isn't ACTIVE
    with (
        pytest.raises(RuntimeError, match="gateway_signing_key"),
        gateway(dsns, ks, tmp_path, FakeClock()),
    ):
        pass
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        conn.execute("DELETE FROM gw.trusted_key WHERE role = 'GATEWAY'")
    ks.place("gateway_signing_key", ks.gw)
    with (
        pytest.raises(RuntimeError, match="no ACTIVE GATEWAY key"),
        gateway(dsns, ks, tmp_path, FakeClock()),
    ):
        pass


def test_gateway_signs_with_active_key_and_rereads(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        fake = gw.node()
        challenge = fake.connect(enrol_gateway_fingerprint=_id(ks.gw))
        assert challenge["key_id"] == _id(ks.gw)
        fake.close()
        # A new ACTIVE gateway key is picked up by the re-read, for new connections.
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        ks.place(f"gateway_signing_key_{key_id}", new)
        assert keys.activate_ready(app, _created(app, key_id)) == [key_id]

        def presented() -> bool:
            probe = gw.node()
            try:
                return probe.connect(enrol_gateway_fingerprint=key_id)["key_id"] == key_id
            except Rejected:
                return False
            finally:
                probe.close()

        assert _wait_for(presented)


# ------------------------------------------------------------------------------ the node


def test_keyset_signed_by_gateway_key_rejected(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    with gateway(dsns, ks, tmp_path, _clock(app)) as gw:
        fake = _enrolled(gw, app)
        before = (dict(fake.trusted_core), dict(fake.pinned_gateway))
        new = Ed25519PrivateKey.generate()
        # The gateway's key under the core key's ID: never a core signature (R6-3).
        forged = _keyset(ks.gw, _id(ks.core), new, gw.clock(), target=fake.node_id)
        fake.inject(gw.gw_envelope(fake, "keyset", forged))
        assert fake.rejected == [("keyset", "bad_signature")] and fake.prompt is None
        for role in ("core", "gateway"):  # under its own ID: an unknown core key ID
            forged = _keyset(ks.gw, _id(ks.gw), new, gw.clock(), role=role, target=fake.node_id)
            fake.inject(gw.gw_envelope(fake, "keyset", forged))
        assert fake.rejected[1:] == [("keyset", "untrusted_key_id")] * 2
        assert (fake.trusted_core, fake.pinned_gateway) == before
        assert fake.prompt == "reenrol" and not fake.connected


def test_keyset_from_untrusted_core_key_rejected(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    with gateway(dsns, ks, tmp_path, _clock(app)) as gw:
        fake = _enrolled(gw, app)
        before = dict(fake.trusted_core)
        stranger = Ed25519PrivateKey.generate()
        payload = _keyset(stranger, _id(stranger), Ed25519PrivateKey.generate(), gw.clock())
        fake.inject(gw.gw_envelope(fake, "keyset", payload))
        assert fake.rejected == [("keyset", "untrusted_key_id")]
        assert fake.trusted_core == before and fake.prompt == "reenrol" and not fake.connected


def test_keyset_for_another_node_rejected_null_accepted(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """R12-27: a keyset whose signed target is neither null nor this node is refused."""
    with gateway(dsns, ks, tmp_path, _clock(app)) as gw:
        fake = _enrolled(gw, app)
        other = Ed25519PrivateKey.generate()
        sent = fake.sent_kinds()
        fake.inject(gw.gw_envelope(
            fake, "keyset", _keyset(ks.core, _id(ks.core), other, gw.clock(), target=uuid.uuid4())
        ))  # fmt: skip
        assert fake.rejected == [("keyset", "wrong_target")] and _id(other) not in fake.trusted_core
        assert fake.sent_kinds() == sent  # not acknowledged
        fake.inject(
            gw.gw_envelope(fake, "keyset", _keyset(ks.core, _id(ks.core), other, gw.clock()))
        )
        assert fake.trusted_core[_id(other)] == public_bytes(other)
        assert fake.sent_kinds()[len(sent) :] == ["received", "ack"]


def test_keyset_payload_differing_from_signed_args_rejected(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    with gateway(dsns, ks, tmp_path, _clock(app)) as gw:
        fake = _enrolled(gw, app)
        payload = _keyset(ks.core, _id(ks.core), Ed25519PrivateKey.generate(), gw.clock())
        payload["key_id"] = "core-x"
        fake.inject(gw.gw_envelope(fake, "keyset", payload))
        missing = _keyset(ks.core, _id(ks.core), Ed25519PrivateKey.generate(), gw.clock())
        del missing["outbox_id"]
        fake.inject(gw.gw_envelope(fake, "keyset", missing))
        assert fake.rejected == [("keyset", "keyset_mismatch"), ("keyset", "malformed")]


def test_core_rotation_end_to_end(app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        fake = _enrolled(gw, app)
        assert fake.node_id is not None
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="CORE", public_key=public_bytes(new))
        ks.place(f"core_signing_key_{key_id}", new)
        assert _wait_for(lambda: _acked(app, key_id, fake))
        keysets = [e for e in fake.received_envelopes if e["kind"] == "keyset"]
        assert len(keysets) == 1
        payload = keysets[0]["payload"]
        assert payload["outbox_id"] == str(_item_of(app, key_id, fake.node_id)["id"])
        assert fake.trusted_core[key_id] == public_bytes(new) and fake.rejected == []
        assert keys.activate_ready(app, clock()) == [key_id]
        # Core signs with the new key; the node runs it.
        signer, signer_id = keys.active_core_key(app)
        assert signer_id == key_id
        fake.inject(gw.gw_envelope(fake, "node.action", {
            "authz": _action(signer, signer_id, fake.node_id, clock())
        }))  # fmt: skip
        assert len(fake.actions) == 1
        # The old key is still accepted inside the 24 h overlap ...
        clock.advance((DAY - timedelta(minutes=1)).total_seconds())
        fake.inject(gw.gw_envelope(fake, "node.action", {
            "authz": _action(ks.core, _id(ks.core), fake.node_id, clock())
        }))  # fmt: skip
        assert len(fake.actions) == 2 and fake.rejected == []
        # ... and not after it: an unknown key ID, so the node runs nothing and asks to re-enrol.
        clock.advance(timedelta(minutes=2).total_seconds())
        fake.inject(gw.gw_envelope(fake, "node.action", {
            "authz": _action(ks.core, _id(ks.core), fake.node_id, clock())
        }))  # fmt: skip
        assert len(fake.actions) == 2 and fake.rejected == [("node.action", "untrusted_key_id")]
        assert fake.prompt == "reenrol" and _id(ks.core) not in fake.trusted_core


def test_gateway_rotation_end_to_end(app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        fake = _enrolled(gw, app)
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        assert _wait_for(lambda: _acked(app, key_id, fake))
        keyset = next(e for e in fake.received_envelopes if e["kind"] == "keyset")
        assert keyset["payload"]["authz"]["key_id"] == _id(ks.core)  # signed by core
        assert fake.pinned_gateway[key_id] == public_bytes(new)
        assert key_id not in fake.trusted_core  # a gateway key is never a core key (R6-3)
        assert keys.activate_ready(app, clock()) == [key_id]
        fake.close()
    ks.place(f"gateway_signing_key_{key_id}", new)
    with gateway(dsns, ks, tmp_path, clock, as_key=new) as gw2:  # the gateway restarts
        again = _again(gw2, fake)
        assert again.connect()["key_id"] == key_id
        welcome = again.hello()
        assert "session_id" in welcome and again.rejected == []
        again.close()
    # After the overlap, a gateway still on the old key is refused at its challenge.
    clock.advance((DAY + timedelta(minutes=1)).total_seconds())
    with psycopg.connect(dsns.owner, autocommit=True) as conn:  # an impostor on the old key
        conn.execute("UPDATE gw.trusted_key SET state = 'RETIRED' WHERE key_id = %s", (key_id,))
        conn.execute("UPDATE gw.trusted_key SET state = 'ACTIVE' WHERE key_id = %s", (_id(ks.gw),))
    with gateway(dsns, ks, tmp_path, clock) as gw3:
        old = _again(gw3, fake)
        with pytest.raises(Rejected) as e:
            old.connect()
        assert e.value.reason == "unpinned_key" and old.prompt == "reenrol"
        assert _id(ks.gw) not in old.pinned_gateway


def _stale_after_core_rotation(
    app: Conn, gw: Gw, clock: FakeClock, *, role: str = "CORE"
) -> tuple[FakeNode, FakeNode, str, Ed25519PrivateKey]:
    """Nodes A (online, acks) and B (offline); a new key announced; 24 h pass; activated."""
    a = _enrolled(gw, app)
    b = _enrolled(gw, app)
    b.close()
    new = Ed25519PrivateKey.generate()
    key_id = keys.announce(app, role=role, public_key=public_bytes(new))
    assert _wait_for(lambda: _acked(app, key_id, a))
    t0 = _created(app, key_id)
    assert keys.activate_ready(app, t0 + timedelta(hours=22)) == []
    clock.now = t0 + timedelta(hours=23)  # the timeout activation (R12-28)
    assert keys.activate_ready(app, clock()) == [key_id]
    assert (_state(app, a.node_id), _state(app, b.node_id)) == ("CURRENT", "STALE")
    return a, b, key_id, new


def test_core_rotation_node_offline_beyond_24h(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        _, b, key_id, new = _stale_after_core_rotation(app, gw, clock)
        ks.place(f"core_signing_key_{key_id}", new)
        _person_item(app)  # something B would otherwise be sent
        stale = _again(gw, b)
        seen = len(stale.received_envelopes)
        assert stale.connect()["key_id"] == _id(ks.gw)  # the challenge: the one message
        with pytest.raises(WebSocketDisconnect) as e:
            stale.hello()
        assert e.value.code == REENROL
        assert len(stale.received_envelopes) == seen  # no welcome, item, chunk or action
        assert stale.actions == [] and stale.displays == {}
        assert stale.prompt == "reenrol" and stale.state_path.exists()  # journal kept


def test_stale_node_refusal_recorded(app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        b = _enrolled(gw, app)
        b.close()
        app.execute("UPDATE gw.node SET key_state = 'STALE' WHERE id = %s", (b.node_id,))
        since = _seq(app)
        opened = _connections(app, b.node_id)
        stale = _again(gw, b)
        with pytest.raises(WebSocketDisconnect) as e:
            stale.hello()
        assert e.value.code == REENROL
        assert _wait_for(lambda: _audit_refusals(dsns, since)) == [
            ("CONNECT_REFUSED", "REJECTED", "reenrol_required", b.node_id)
        ]
        assert _connections(app, b.node_id) == opened  # no connection was opened


def test_gateway_rotation_node_offline_beyond_24h(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        _, b, key_id, new = _stale_after_core_rotation(app, gw, clock, role="GATEWAY")
        # Still on the old gateway key: B passes the challenge, then gets 4409.
        stale = _again(gw, b)
        with pytest.raises(WebSocketDisconnect) as e:
            stale.hello()
        assert e.value.code == REENROL and stale.prompt == "reenrol"
    ks.place(f"gateway_signing_key_{key_id}", new)
    app.execute("UPDATE gw.node SET key_state = 'CURRENT' WHERE id = %s", (b.node_id,))
    with gateway(dsns, ks, tmp_path, clock, as_key=new) as gw2:
        # Even if B were not flagged: the rotated gateway key is unknown to it, so it closes on
        # the challenge itself and never sends hello. It never takes the key from transport.
        since = _seq(app)
        unflagged = _again(gw2, b)
        with pytest.raises(Rejected) as e:
            unflagged.hello()
        assert e.value.reason == "unpinned_key" and unflagged.prompt == "reenrol"
        assert key_id not in unflagged.pinned_gateway and unflagged.sent_kinds() == []
        assert _audit_refusals(dsns, since) == []  # B sent nothing for the gateway to refuse


def test_reconnect_within_overlap_gets_keyset_first(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        a = _enrolled(gw, app)
        b = _enrolled(gw, app)
        b.close()
        _person_item(app)  # an older, person-addressed item
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="CORE", public_key=public_bytes(new))
        assert _wait_for(lambda: _acked(app, key_id, a))
        t0 = _created(app, key_id)
        clock.now = t0 + timedelta(hours=2)
        assert keys.activate_ready(app, clock()) == []  # waits for B (or 24 h)
        back = _again(gw, b)
        back.hello()
        first = back.recv()
        assert first["kind"] == "keyset" and first["payload"]["key_id"] == key_id
        assert back.trusted_core[key_id] == public_bytes(new)
        assert _wait_for(lambda: _acked(app, key_id, back))
        assert any(e["kind"] == "outbox.item" for e in back.received_envelopes)  # then the rest
        assert keys.activate_ready(app, clock()) == [key_id]
        assert (_state(app, a.node_id), _state(app, b.node_id)) == ("CURRENT", "CURRENT")


# ------------------------------------------------------------------------------ re-enrolment


def test_reenrol_recovers_stale_node(app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        _, b, key_id, new = _stale_after_core_rotation(app, gw, clock)
        ks.place(f"core_signing_key_{key_id}", new)
        assert b.node_id is not None
        stale_item = _item_of(app, key_id, b.node_id)["id"]
        pending = _person_item(app)  # person-addressed: reaches the new node
        code = nodes.reenrol(app, b.node_id, *core_main._key_fingerprints(app))
        _, core_fp, gw_fp = nodes.parse_enrolment_code(code)
        assert (core_fp, gw_fp) == (key_id, _id(ks.gw))  # the current fingerprints
        row = app.execute(
            "SELECT revoked_at IS NOT NULL FROM gw.node WHERE id = %s", (b.node_id,)
        ).fetchone()
        assert row == (True,)
        # B's own targeted item names B in its signed authz: superseded, never re-targeted.
        assert _item_of(app, key_id, b.node_id)["id"] == stale_item
        assert _item_of(app, key_id, b.node_id)["state"] == "SUPERSEDED"
        _wait_for(lambda: gw.app.state.gateway.keys.current_core_key[0] == key_id)
        fresh = _device(gw, b)  # the device makes a new key pair
        node_id = fresh.enrol(code)
        assert node_id != b.node_id and fresh.trusted_core == {key_id: public_bytes(new)}
        fresh.sync()
        assert _wait_for(lambda: (fresh.drain(0.02), str(pending) in fresh.seen_outbox)[1])
        signer, signer_id = keys.active_core_key(app)
        fresh.inject(gw.gw_envelope(fresh, "node.action", {
            "authz": _action(signer, signer_id, node_id, clock())
        }))  # fmt: skip
        assert len(fresh.actions) == 1 and fresh.rejected == []


def test_reenrol_is_one_transaction_and_refusals_are_kept(app: Conn, owner: Conn, ks: Keys) -> None:
    node = flow.new_node(owner, MANJ)
    nodes.revoke(app, node)
    since = _seq(app)
    with pytest.raises(nodes.RevokeRefused) as e:
        nodes.reenrol(app, node, _id(ks.core), _id(ks.gw))
    assert e.value.reason == "already_revoked"
    rows = app.execute(
        "SELECT action::text, outcome::text FROM audit.event WHERE seq > %s ORDER BY seq", (since,)
    ).fetchall()
    assert rows == [("REVOKE", "REJECTED")]  # the refusal is kept; no enrolment was created


def test_reenrol_code_pins_from_fingerprints_not_gateway(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        b = _enrolled(gw, app)
        b.close()
        assert b.node_id is not None
        code = nodes.reenrol(app, b.node_id, *core_main._key_fingerprints(app))
    # A gateway presenting keys that don't match the code: an impostor gateway key, then an
    # impostor core key. The device pins neither.
    impostor_gw, impostor_core = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    ks.place(f"gateway_signing_key_{_id(impostor_gw)}", impostor_gw)
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        conn.execute("DELETE FROM gw.trusted_key WHERE role = 'GATEWAY'")
        conn.execute(
            "INSERT INTO gw.trusted_key (key_id, role, public_key, state) VALUES (%s, 'GATEWAY',"
            " %s, 'ACTIVE')",
            (_id(impostor_gw), public_bytes(impostor_gw)),
        )
    with gateway(dsns, ks, tmp_path, clock, as_key=impostor_gw) as gw2:
        device = _device(gw2, b)
        with pytest.raises(Rejected) as e:
            device.enrol(code)
        assert e.value.reason == "fingerprint_mismatch" and device.pinned_gateway == {}
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        swap = "UPDATE gw.trusted_key SET public_key = %s, key_id = %s WHERE role = %s"
        conn.execute(swap, (public_bytes(ks.gw), _id(ks.gw), "GATEWAY"))
        conn.execute(swap, (public_bytes(impostor_core), _id(impostor_core), "CORE"))
    with gateway(dsns, ks, tmp_path, clock) as gw3:
        device = _device(gw3, b)
        with pytest.raises(Rejected) as e:
            device.enrol(code)
        assert e.value.reason == "core_fingerprint_mismatch"
        assert device.discarded and device.trusted_core == {} and device.pinned_gateway == {}


# ------------------------------------------------------------------------------ the CLI


def test_keys_cli(
    app: Conn,
    owner: Conn,
    dsns: Dsns,
    ks: Keys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(config, "app_dsn", lambda: dsns.app)
    with psycopg.connect(dsns.owner, autocommit=True) as conn:
        conn.execute("DELETE FROM gw.trusted_key")
    assert core_main.main(["keys", "init"]) == 0
    assert core_main.main(["keys", "init"]) == 0  # idempotent
    assert {r[0] for r in app.execute("SELECT key_id FROM gw.trusted_key")} == {
        _id(ks.core),
        _id(ks.gw),
    }
    stale = flow.new_node(owner, MANJ)
    new = Ed25519PrivateKey.generate()
    pub = tmp_path / "new.pub"
    pub.write_bytes(public_bytes(new))
    capsys.readouterr()
    assert (
        core_main.main(["keys", "announce", "--role", "core", "--public-key-file", str(pub)]) == 0
    )
    out = capsys.readouterr().out
    assert f"core_signing_key_{_id(new)}" in out
    assert (
        core_main.main(["keys", "announce", "--role", "CORE", "--public-key-file", str(pub)]) == 1
    )
    keys.activate_ready(app, _created(app, _id(new)) + DAY)
    capsys.readouterr()
    assert core_main.main(["keys", "list"]) == 0
    out = capsys.readouterr().out
    assert _id(new) in out and "ACTIVE" in out and "RETIRED" in out
    assert f"STALE {stale}" in out
    assert core_main.main(["keys", "retire", _id(ks.core)]) == 0
    assert core_main.main(["keys", "retire", _id(new)]) == 1  # the ACTIVE key
    pem = _pem(ks.core).decode()
    assert pem not in out  # no key material printed


def test_node_reenrol_cli(
    app: Conn,
    owner: Conn,
    dsns: Dsns,
    ks: Keys,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(config, "app_dsn", lambda: dsns.app)
    node = flow.new_node(owner, MANJ)
    assert core_main.main(["node", "reenrol", str(node)]) == 0
    code = capsys.readouterr().out.rsplit(" ", 1)[1].strip()
    assert nodes.parse_enrolment_code(code)[1:] == (_id(ks.core), _id(ks.gw))
    assert core_main.main(["node", "reenrol", str(node)]) == 1  # already revoked
    row = app.execute(
        "SELECT person_id, kind, name FROM gw.enrolment ORDER BY expires_at DESC LIMIT 1"
    ).fetchone()
    assert row == (MANJ, "DESKTOP", "pc")


# ------------------------------------------- Task 13 carry-overs (a missing secret, enrol warning)


def test_core_activation_without_its_secret_is_loud(
    app: Conn, owner: Conn, dsns: Dsns, ks: Keys, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """No notification rule kind fits, so the signal is an ERROR log at activation and a
    `keys list` line while the secret is missing (Task 13 ruling 4b)."""
    monkeypatch.setattr(config, "app_dsn", lambda: dsns.app)
    new = Ed25519PrivateKey.generate()
    key_id = keys.announce(app, role="CORE", public_key=public_bytes(new))
    secret = f"core_signing_key_{key_id}"
    assert keys.missing_secrets(app) == [("CORE", key_id, secret)]
    capsys.readouterr()
    assert core_main.main(["keys", "list"]) == 0
    assert f"MISSING SECRET CORE {key_id}: place {secret}" in capsys.readouterr().out
    caplog.set_level(logging.ERROR, logger="jarvis.core.keys")
    assert keys.activate_ready(app, _created(app, key_id) + timedelta(hours=23)) == [key_id]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert secret in record.getMessage() and "cannot sign" in record.getMessage()
    ks.place(secret, new)
    assert keys.missing_secrets(app) == []
    capsys.readouterr()
    assert core_main.main(["keys", "list"]) == 0
    assert "MISSING SECRET" not in capsys.readouterr().out


def test_gateway_without_its_new_secret_logs_an_error(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Core can't see the gateway's secrets; the gateway's re-read says so at ERROR, every
    time, and keeps signing with the old key until the secret is placed."""
    caplog.set_level(logging.ERROR, logger="jarvis_server.gateway.app")
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        assert keys.activate_ready(app, _created(app, key_id) + timedelta(hours=23)) == [key_id]

        def complained() -> bool:
            return any(r.levelno == logging.ERROR and f"gateway_signing_key_{key_id}"
                       in r.getMessage() and _id(ks.gw) in r.getMessage()
                       for r in caplog.records)  # fmt: skip

        assert _wait_for(complained)
        probe = gw.node()
        assert probe.connect(enrol_gateway_fingerprint=_id(ks.gw))["key_id"] == _id(ks.gw)
        probe.close()


@pytest.mark.parametrize("command", ["enrol", "reenrol"])
def test_enrolment_warns_while_a_next_key_exists(
    app: Conn, owner: Conn, dsns: Dsns, ks: Keys, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], command: str,
) -> None:  # fmt: skip
    """A device enrolled during a rotation never gets the new key, so it goes STALE when the
    key activates (Task 12 concern b). Both commands warn; the code is still the last thing
    printed on stdout."""
    monkeypatch.setattr(config, "app_dsn", lambda: dsns.app)
    monkeypatch.setattr(core_main, "_person_id", lambda slug: MANJ)

    def run() -> tuple[str, str]:
        node = flow.new_node(owner, MANJ)
        argv = (["node", "enrol", "--person", "manj", "--kind", "DESKTOP", "--name", "pc"]
                if command == "enrol" else ["node", "reenrol", str(node)])  # fmt: skip
        assert core_main.main(argv) == 0
        out, err = capsys.readouterr()
        nodes.parse_enrolment_code(out.rsplit(" ", 1)[1].strip())
        return out, err

    capsys.readouterr()
    assert "STALE" not in run()[1]
    key_id = keys.announce(
        app, role="GATEWAY", public_key=public_bytes(Ed25519PrivateKey.generate())
    )
    out, err = run()
    assert f"GATEWAY key {key_id}" in err and "STALE" in err and "node reenrol" in err
    assert "Warning" not in out


# ------------------------------------------------------------------------------ migration


def test_0011_downgrade_refuses_once_keys_exist() -> None:
    with fresh_database() as d:
        with psycopg.connect(d.app, autocommit=True) as conn:
            keys.init_keys(
                conn,
                core_public=public_bytes(Ed25519PrivateKey.generate()),
                gateway_public=public_bytes(Ed25519PrivateKey.generate()),
            )
        with pytest.raises(Exception, match="0011 cannot be undone"):
            migrate.downgrade(d.owner, "0010")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0011",)
            conn.execute("DELETE FROM gw.trusted_key")
        migrate.downgrade(d.owner, "0010")
        with psycopg.connect(d.owner, autocommit=True) as conn:
            assert conn.execute("SELECT to_regclass('gw.trusted_key')").fetchone() == (None,)
            assert conn.execute(
                "SELECT count(*) FROM pg_attribute WHERE attrelid = 'gw.node'::regclass"
                " AND attname = 'key_state'"
            ).fetchone() == (0,)
        migrate.upgrade(d.owner)


# ------------------------------------------------------------------------------ fix round 1


def test_connection_spanning_gateway_activation_moves_to_the_new_key(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """I-1: a connection opened before a GATEWAY activation is closed (`key_rotated`) once the
    gateway re-reads its keys, and the node reconnects under the new key. It is never asked
    to re-enrol, before or after the overlap ends."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        fake = _enrolled(gw, app)
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        assert _wait_for(lambda: _acked(app, key_id, fake))
        ks.place(f"gateway_signing_key_{key_id}", new)
        old_conn = app.execute(
            "SELECT id FROM gw.node_connection WHERE node_id = %s AND closed_at IS NULL",
            (fake.node_id,),
        ).fetchone()
        assert old_conn is not None
        fake.sync()  # the old-key connection works until the re-read
        assert keys.activate_ready(app, clock()) == [key_id]

        def closed() -> bool:
            try:
                fake.drain(0.02)
            except WebSocketDisconnect:
                return True
            return not fake.connected

        assert _wait_for(closed)
        row = app.execute(
            "SELECT close_reason FROM gw.node_connection WHERE id = %s", (old_conn[0],)
        ).fetchone()
        assert row == ("key_rotated",)
        assert fake.prompt is None
        again = _again(gw, fake)
        assert again.connect()["key_id"] == key_id  # the new key, pinned from the keyset
        again.hello()
        clock.advance((DAY + timedelta(minutes=5)).total_seconds())  # the overlap ends
        again.sync()
        assert _id(ks.gw) not in again.pinned_gateway
        assert again.prompt is None and again.rejected == [] and again.connected


def test_node_rejects_transport_on_a_key_past_its_overlap(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """I-1, the node's side: if a connection were left on the old gateway key past the
    overlap, the node refuses its next frame and asks to re-enrol."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock, refresh=3600) as gw:  # never re-read here
        fake = _enrolled(gw, app)
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        assert _wait_for(lambda: _acked(app, key_id, fake))
        fake.sync()
        clock.advance((DAY + timedelta(minutes=5)).total_seconds())
        with pytest.raises(Rejected) as e:
            fake.inject(gw.gw_envelope(fake, "ping", {}))
        assert e.value.reason == "unpinned_key"
        assert fake.prompt == "reenrol" and not fake.connected


def test_transport_bad_signature_reconnects_expired_key_reenrols(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """Spec §7.1 / R12-29 (fix round 3): the node verifies every gw-transport envelope under the
    challenge's key. A tampered envelope, or one signed by another key, while that key is
    trusted is a bad signature: it runs nothing, closes and reconnects, and is never asked to
    re-enrol. Once the challenge key's overlap has ended, the next envelope asks to re-enrol."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock, refresh=3600) as gw:
        fake = _enrolled(gw, app)
        assert fake.node_id is not None
        action = {"authz": _action(ks.core, _id(ks.core), fake.node_id, clock())}
        tampered = gw.gw_envelope(fake, "node.action", action)
        tampered["payload"]["authz"]["object"]["action_kind"] = "pc.delete_test_file"
        with pytest.raises(Rejected) as e:
            fake.inject(tampered)
        assert e.value.reason == "bad_signature"
        assert fake.prompt is None and fake.may_reconnect and fake.actions == []
        assert not fake.connected and fake.state_path.exists()  # the journal is kept
        fake.reconnect()
        signer = gw.gw_key
        gw.gw_key = Ed25519PrivateKey.generate()  # another key, the trusted one still current
        with pytest.raises(Rejected) as e:
            fake.inject(gw.gw_envelope(fake, "node.action", action))
        assert e.value.reason == "bad_signature" and fake.prompt is None and fake.may_reconnect
        gw.gw_key = signer
        fake.reconnect()
        # The challenge key's overlap ends (a GATEWAY keyset accepted, then + 24 h).
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        assert _wait_for(lambda: _acked(app, key_id, fake))
        clock.advance((DAY + timedelta(minutes=5)).total_seconds())
        with pytest.raises(Rejected) as e:
            fake.inject(gw.gw_envelope(fake, "node.action", action))
        assert e.value.reason == "unpinned_key"
        assert fake.prompt == "reenrol" and not fake.may_reconnect and fake.actions == []


# ------------------------------------------------------------------------------ fix round 2


def _closed(fake: FakeNode) -> bool:
    if not fake.connected:
        return True
    try:
        fake.drain(0.02)
    except WebSocketDisconnect:
        return True
    return not fake.connected


def _close_reason(app: Conn, node_id: uuid.UUID | None) -> list[str | None]:
    rows = app.execute(
        "SELECT close_reason FROM gw.node_connection WHERE node_id = %s ORDER BY id", (node_id,)
    ).fetchall()
    return [r[0] for r in rows]


def test_timeout_activation_at_23h_moves_acked_nodes_via_notify(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """R12-28: B never acks, so the GATEWAY key activates at issued_at + 23 h. Core's NOTIFY
    jarvis_keys makes the gateway re-read at once (the 60 s backstop is off here) and close A's
    old-key connection. A reconnects under the new key and is never asked to re-enrol, also
    after the old key's 24 h; B is STALE."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock, refresh=3600) as gw:
        a = _enrolled(gw, app)
        b = _enrolled(gw, app)
        b.close()
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        ks.place(f"gateway_signing_key_{key_id}", new)
        assert _wait_for(lambda: _acked(app, key_id, a))
        t0 = _created(app, key_id)
        assert keys.activate_ready(app, t0 + timedelta(hours=22, minutes=59)) == []
        clock.now = t0 + timedelta(hours=23)
        a.sync()  # still on the old key, which A trusts until + 24 h
        assert keys.activate_ready(app, clock()) == [key_id]
        assert _wait_for(lambda: _closed(a))
        assert _close_reason(app, a.node_id)[-1] == "key_rotated"
        assert a.last_close == 4401 and a.may_reconnect and a.prompt is None
        a.reconnect()
        assert a.challenge is not None and a.challenge["key_id"] == key_id
        clock.now = t0 + DAY + timedelta(minutes=1)  # A drops the old key
        a.sync()
        assert _id(ks.gw) not in a.pinned_gateway
        assert a.prompt is None and a.rejected == [] and a.connected
        assert (_state(app, a.node_id), _state(app, b.node_id)) == ("CURRENT", "STALE")


def test_all_acks_activation_rereads_at_once(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """R12-28: an activation from all acks wakes the gateway through NOTIFY too."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock, refresh=3600) as gw:
        a = _enrolled(gw, app)
        new = Ed25519PrivateKey.generate()
        key_id = keys.announce(app, role="GATEWAY", public_key=public_bytes(new))
        ks.place(f"gateway_signing_key_{key_id}", new)
        assert _wait_for(lambda: _acked(app, key_id, a))
        assert keys.activate_ready(app, clock()) == [key_id]
        assert _wait_for(lambda: _closed(a))
        assert _close_reason(app, a.node_id)[-1] == "key_rotated"
        assert gw.app.state.gateway.keys.gw_key_id == key_id
        a.reconnect()
        assert a.challenge is not None and a.challenge["key_id"] == key_id
        assert a.prompt is None and a.rejected == []


def test_close_codes_follow_r12_29(app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path) -> None:
    """R12-29: 4401 with any reason means reconnect; only 4409 means re-enrol."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock) as gw:
        a = _enrolled(gw, app)
        # superseded: the same device connects again.
        second = _again(gw, a)
        second.hello()
        assert _wait_for(lambda: _closed(a))
        assert a.last_close == 4401 and a.may_reconnect and a.prompt is None
        second.close()
        a.reconnect()
        # heartbeat: three pings left unanswered.
        a.answer_pings = False
        for _ in range(5):
            clock.advance(31)
            _wait_for(lambda: _closed(a), timeout=0.3)
        assert _wait_for(lambda: _closed(a))
        assert _close_reason(app, a.node_id)[-1] == "heartbeat"
        assert a.last_close == 4401 and a.may_reconnect and a.prompt is None
        a.answer_pings = True
        a.reconnect()
        a.close()
        # 4409: re-enrol, never reconnect.
        app.execute("UPDATE gw.node SET key_state = 'STALE' WHERE id = %s", (a.node_id,))
        with pytest.raises(WebSocketDisconnect):
            a.connect()
            a.hello()
        assert a.last_close == 4409 and a.prompt == "reenrol" and not a.may_reconnect
        with pytest.raises(RuntimeError, match="re-enrolled"):
            a.reconnect()


def test_handshake_caught_by_a_key_swap_delivers_nothing(
    app: Conn, dsns: Dsns, ks: Keys, tmp_path: Path
) -> None:
    """Re-review Minor: a session that finishes its handshake on a key the gateway has just
    replaced is closed `key_rotated`, and its link delivers nothing (no outbox item)."""
    clock = _clock(app)
    with gateway(dsns, ks, tmp_path, clock, refresh=3600) as gw:
        a = _enrolled(gw, app)
        a.close()
        _person_item(app)
        ctx = gw.app.state.gateway
        old = ctx.keys
        # The swap lands between this connection's challenge and its registration.
        fake_new = dataclasses.replace(old, gw_key_id="b" * 16)
        again = _again(gw, a)
        again.connect()
        ctx.keys = fake_new
        try:
            again.hello()
            assert _wait_for(lambda: _closed(again))
        finally:
            ctx.keys = old
        assert not any(e["kind"] == "outbox.item" for e in again.received_envelopes)
        assert _close_reason(app, a.node_id)[-1] == "key_rotated"
        assert again.may_reconnect and again.prompt is None
