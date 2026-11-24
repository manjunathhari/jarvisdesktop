"""Core's key trust and rotation (spec §7.1, §7.2; R5-1, R6-2, R6-3, R12-23, R12-26, R12-27).

**The trusted keys** are the rows of `gw.trusted_key` (migration 0011): CORE and GATEWAY public
keys, NEXT, ACTIVE (one per role) or RETIRED. A RETIRED key with no `retired_at` is
retired-pending: core still verifies its own signatures with it until `retire`. `key_id` is
always `fingerprint(public_key)`.

**Private keys never touch the database.** The ACTIVE core key is read from the secret
`core_signing_key_<key_id>` (placed by `harden.sh`) or, for the bootstrap key only, from the
legacy secret `core_signing_key` when its fingerprint is the ACTIVE key_id
(`signing.load_rotating_key`). Nothing here logs or prints a key.

**Rotation.**
- `init_keys` bootstraps the current keys as ACTIVE (`jarvis-core keys init`).
- `announce` inserts a NEXT key and writes one `keys.keyset` outbox item per active node.
  An active node is one that isn't revoked and is CURRENT. Each item is targeted at its node,
  and its `authz` (kind `keyset`) is signed by the ACTIVE core key, for both roles. The signed
  object's `args` are exactly `{role, key_id, public_key}`. `target_node_id` and `person_id` name
  the node. The overlap starts at `issued_at` (the key row's `created_at`), and `valid_until` is
  `issued_at` + 24 h (R6-2, R12-27).
- `activate_ready` makes a NEXT key ACTIVE once every keyset item for a non-revoked node is
  ACKNOWLEDGED, or once 23 h have passed (R12-28), an hour before nodes drop the old key. The
  previous ACTIVE key of that role becomes retired-pending. In the same transaction it marks every
  non-revoked CURRENT node without an acknowledged item STALE (R5-1). This includes a node
  enrolled after the announcement: it never learned the new key. Each activation NOTIFYs
  `jarvis_keys`, so the gateway re-reads its keys at once. The job `keys.activate` runs it (the
  scheduler queues it every minute, Task 13).
- `retire` ends a retired-pending key's overlap. A NEXT key is never withdrawn (I-2), and only
  one key is announced at a time, for either role (M-4).

Every change takes one transaction-level advisory lock, so announcements, activations,
retirements and the bootstrap never interleave.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server import comm, config, jobs, signing
from jarvis_server.core import dispatch, rules

log = logging.getLogger("jarvis.core.keys")

Role = Literal["CORE", "GATEWAY"]
ROLES: tuple[Role, ...] = ("CORE", "GATEWAY")
SECRET_NAMES: dict[Role, str] = {"CORE": "core_signing_key", "GATEWAY": "gateway_signing_key"}
OVERLAP = timedelta(hours=24)  # the keyset's validity: nodes keep the old key this long
# The latest activation: an hour before any node drops the old key (R12-28).
ACTIVATE_BY = timedelta(hours=23)
KEYSET_KIND = "keys.keyset"
ACTIVATE_JOB = "keys.activate"
# One lock for every change to the trusted keys (announce, activate, retire, init).
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('gw.trusted_key', 0))"


class KeyRefused(Exception):
    """A key operation refused with `reason` (a code); nothing was written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    role: Role
    public_key: bytes
    state: str
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None


def secret_name(role: Role, key_id: str) -> str:
    """The secret file a key's private key is placed in."""
    return f"{SECRET_NAMES[role]}_{key_id}"


def _key(row: Any) -> TrustedKey:
    return TrustedKey(
        key_id=str(row[0]),
        role=cast(Role, row[1]),
        public_key=bytes(row[2]),
        state=str(row[3]),
        created_at=row[4],
        activated_at=row[5],
        retired_at=row[6],
    )


def list_keys(conn: psycopg.Connection[Any]) -> list[TrustedKey]:
    rows = conn.execute(
        "SELECT key_id, role, public_key, state, created_at, activated_at, retired_at"
        " FROM gw.trusted_key ORDER BY role, created_at, key_id"
    ).fetchall()
    return [_key(r) for r in rows]


def active_key(conn: psycopg.Connection[Any], role: Role) -> TrustedKey:
    row = conn.execute(
        "SELECT key_id, role, public_key, state, created_at, activated_at, retired_at"
        " FROM gw.trusted_key WHERE role = %s AND state = 'ACTIVE'",
        (role,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no ACTIVE {role} key in gw.trusted_key (run `jarvis-core keys init`)")
    return _key(row)


def active_core_key(conn: psycopg.Connection[Any]) -> tuple[Ed25519PrivateKey, str]:
    """(the ACTIVE core signing key, its key_id). The key ID comes from gw.trusted_key, and the
    private key from its secret."""
    active = active_key(conn, "CORE")
    key = signing.load_rotating_key(
        config.SECRETS_DIR, SECRET_NAMES["CORE"], active.key_id, active.public_key
    )
    return key, active.key_id


def trusted_core_keys(conn: psycopg.Connection[Any]) -> dict[str, bytes]:
    """The core keys core still verifies its own signatures with: ACTIVE and retired-pending."""
    rows = conn.execute(
        "SELECT key_id, public_key FROM gw.trusted_key WHERE role = 'CORE'"
        " AND (state = 'ACTIVE' OR (state = 'RETIRED' AND retired_at IS NULL))"
    ).fetchall()
    return {str(k): bytes(p) for k, p in rows}


def stale_nodes(conn: psycopg.Connection[Any]) -> list[tuple[UUID, str]]:
    """(node_id, name) of the non-revoked STALE nodes: each needs `node reenrol`."""
    rows = conn.execute(
        "SELECT id, name FROM gw.node WHERE key_state = 'STALE' AND revoked_at IS NULL"
        " ORDER BY registered_at, id"
    ).fetchall()
    return [(UUID(str(i)), str(n)) for i, n in rows]


def _role(role: str) -> Role:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    return role


def _public(public_key: bytes) -> bytes:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("a public key is a raw 32-byte Ed25519 key")
    return public_key


# ------------------------------------------------------------------ bootstrap


def init_keys(
    conn: psycopg.Connection[Any], *, core_public: bytes, gateway_public: bytes
) -> list[str]:
    """Insert the current core and gateway keys as ACTIVE; return the key_ids inserted.

    Idempotent: a role whose ACTIVE key is already this key is left alone. Refuses
    (`active_key_differs`, nothing written) if a role has a different ACTIVE key.
    """
    wanted: dict[Role, bytes] = {"CORE": _public(core_public), "GATEWAY": _public(gateway_public)}
    inserted: list[str] = []
    with conn.transaction():
        conn.execute(_LOCK)
        for role, public in wanted.items():
            row = conn.execute(
                "SELECT public_key FROM gw.trusted_key WHERE role = %s AND state = 'ACTIVE'",
                (role,),
            ).fetchone()
            if row is not None:
                if bytes(row[0]) != public:
                    raise KeyRefused("active_key_differs")
                continue
            key_id = signing.fingerprint(public)
            conn.execute(
                "INSERT INTO gw.trusted_key (key_id, role, public_key, state, activated_at)"
                " VALUES (%s, %s, %s, 'ACTIVE', now())",
                (key_id, role, public),
            )
            inserted.append(key_id)
    return inserted


# ------------------------------------------------------------------ announce


def _keyset_object(
    *, role: Role, key_id: str, public_key: bytes, node_id: UUID, person_id: UUID,
    issued_at: datetime,
) -> dict[str, Any]:  # fmt: skip
    return {
        "v": 1,
        "kind": "keyset",
        "action_kind": "keyset.add",
        "args": {"role": role.lower(), "key_id": key_id, "public_key": _b64(public_key)},
        "target_node_id": str(node_id),
        "person_id": str(person_id),
        "approval_id": None,
        "execution_key": None,
        "reply_id": None,
        "summary": None,
        "issued_at": issued_at.isoformat(),
        "valid_until": (issued_at + OVERLAP).isoformat(),
    }


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def announce(conn: psycopg.Connection[Any], *, role: Role, public_key: bytes) -> str:
    """Insert a NEXT key and one `keys.keyset` item per active node; return its key_id.

    Refuses (`KeyRefused`, nothing written): `known_key` if the key is already a trusted key
    (any state, any role); `next_pending` if the role already has a NEXT key;
    `rotation_pending` if the other role has one. A GATEWAY keyset still queued when a CORE
    key activates would otherwise be signed by a core key nodes drop at the overlap's end.
    Every item's authz is signed by the ACTIVE core key; `RuntimeError` if it can't be loaded.
    """
    role = _role(role)
    public_key = _public(public_key)
    key_id = signing.fingerprint(public_key)
    with conn.transaction():
        conn.execute(_LOCK)
        if conn.execute(
            "SELECT 1 FROM gw.trusted_key WHERE key_id = %s OR public_key = %s",
            (key_id, public_key),
        ).fetchone():
            raise KeyRefused("known_key")
        pending = conn.execute(
            "SELECT role FROM gw.trusted_key WHERE state = 'NEXT' ORDER BY role LIMIT 1"
        ).fetchone()
        if pending is not None:  # one rotation at a time (fix round 1, M-4)
            raise KeyRefused("next_pending" if pending[0] == role else "rotation_pending")
        signer, signer_id = active_core_key(conn)
        row = conn.execute(
            "INSERT INTO gw.trusted_key (key_id, role, public_key, state) VALUES (%s, %s, %s,"
            " 'NEXT') RETURNING created_at",
            (key_id, role, public_key),
        ).fetchone()
        assert row is not None
        issued_at: datetime = row[0]
        targets = conn.execute(
            "SELECT id, person_id FROM gw.node WHERE revoked_at IS NULL AND key_state = 'CURRENT'"
            " ORDER BY registered_at, id"
        ).fetchall()
        items: list[UUID] = []
        for node, person in targets:
            node_id, person_id = UUID(str(node)), UUID(str(person))
            obj = _keyset_object(
                role=role, key_id=key_id, public_key=public_key, node_id=node_id,
                person_id=person_id, issued_at=issued_at,
            )  # fmt: skip
            authz = cast(rules.AuthzDict, signing.make_authz(signer, signer_id, obj))
            event_id = comm.record_event(
                conn, KEYSET_KIND, subject_type="node", subject_id=str(node_id),
                person_id=person_id, details={"key_id": key_id, "role": role.lower()},
            )  # fmt: skip
            items += rules.notify(
                conn, KEYSET_KIND, person_id=person_id, event_id=event_id,
                context={
                    "target_node_id": node_id, "authz": authz,
                    "expires_at": issued_at + OVERLAP,
                },
            )  # fmt: skip
        conn.execute(
            "UPDATE gw.trusted_key SET announced_outbox_ids = %s WHERE key_id = %s",
            (items, key_id),
        )
    log.info("announced %s key %s to %d nodes", role, key_id, len(items))
    return key_id


# ------------------------------------------------------------------ activation


def activate_ready(conn: psycopg.Connection[Any], now: datetime) -> list[str]:
    """Activate every NEXT key that is ready at `now`; return their key_ids.

    Ready: no keyset item addressed to a non-revoked node is still unacknowledged, or `now >=
    created_at + 23 h` (R12-28: an hour before nodes drop the old key at + 24 h). Each activation
    NOTIFYs `jarvis_keys` with the key_id, so the gateway re-reads at once. In the same transaction
    the role's previous ACTIVE key becomes retired-pending, and every non-revoked CURRENT node
    without an ACKNOWLEDGED item for the key is marked STALE.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry a time zone")
    activated: list[str] = []
    with conn.transaction():
        conn.execute(_LOCK)
        pending = conn.execute(
            "SELECT key_id, role, created_at, announced_outbox_ids FROM gw.trusted_key"
            " WHERE state = 'NEXT' ORDER BY created_at, key_id"
        ).fetchall()
        for key_id, role, created_at, items in pending:
            waiting = conn.execute(
                "SELECT count(*) FROM comm.outbox o JOIN gw.node n ON n.id = o.target_node_id"
                " WHERE o.id = ANY(%s) AND n.revoked_at IS NULL AND o.state <> 'ACKNOWLEDGED'",
                (items,),
            ).fetchone()
            assert waiting is not None
            if waiting[0] > 0 and now < created_at + ACTIVATE_BY:
                continue
            conn.execute(
                "UPDATE gw.trusted_key SET state = 'RETIRED' WHERE role = %s AND state = 'ACTIVE'",
                (role,),
            )
            conn.execute(
                "UPDATE gw.trusted_key SET state = 'ACTIVE', activated_at = %s WHERE key_id = %s",
                (now, key_id),
            )
            stale = conn.execute(
                "UPDATE gw.node SET key_state = 'STALE' WHERE revoked_at IS NULL"
                " AND key_state = 'CURRENT' AND id NOT IN ("
                "  SELECT o.target_node_id FROM comm.outbox o WHERE o.id = ANY(%s)"
                "  AND o.state = 'ACKNOWLEDGED' AND o.target_node_id IS NOT NULL)"
                " RETURNING id",
                (items,),
            ).fetchall()
            # The gateway re-reads its keys at once (R12-28); delivered on commit.
            conn.execute("SELECT pg_notify('jarvis_keys', %s)", (key_id,))
            log.info("activated %s key %s; %d nodes marked STALE", role, key_id, len(stale))
            activated.append(str(key_id))
    return activated


@dispatch.register(ACTIVATE_JOB)
def _activate_job(conn: psycopg.Connection[Any], job: jobs.Job) -> None:
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    activate_ready(conn, row[0])


# ------------------------------------------------------------------ retire


def retire(conn: psycopg.Connection[Any], key_id: str) -> None:
    """End a retired-pending key's overlap: it gets `retired_at`, and core stops verifying
    with it. Refuses (`KeyRefused`, nothing changed): `unknown_key`; `active` (announce and
    activate a successor first); `next_pending` (a NEXT key is never withdrawn: nodes that
    accepted it would keep trusting it and drop the ACTIVE key; fix round 1, I-2);
    `already_retired`."""
    with conn.transaction():
        conn.execute(_LOCK)
        row = conn.execute(
            "SELECT state, retired_at FROM gw.trusted_key WHERE key_id = %s FOR UPDATE",
            (key_id,),
        ).fetchone()
        if row is None:
            raise KeyRefused("unknown_key")
        state, retired_at = row
        if state == "ACTIVE":
            raise KeyRefused("active")
        if state == "NEXT":
            raise KeyRefused("next_pending")
        if retired_at is not None:
            raise KeyRefused("already_retired")
        conn.execute("UPDATE gw.trusted_key SET retired_at = now() WHERE key_id = %s", (key_id,))
    log.info("retired key %s", key_id)
