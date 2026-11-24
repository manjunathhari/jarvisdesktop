"""The gateway's key material (spec §4.3, §7.1, R12-6; Task 12).

The gateway signs `gw-transport` envelopes with its ACTIVE GATEWAY key from `gw.trusted_key`.
The private key comes from the secret `gateway_signing_key_<key_id>`, or, for the bootstrap key
only, from the legacy `gateway_signing_key` when its fingerprint is the ACTIVE key_id
(`signing.load_rotating_key`). The gateway holds core's ACTIVE *public* key only, to hand to a
newly enrolled node in `welcome.enrolment`. The node pins that key only if it matches its
enrolment code (§7.2 step 5). The gateway never reads a core secret.

`load_keys` runs at startup (the gateway refuses to start if it fails) and every 60 s after.
Key IDs are `fingerprint(public)`. A node's `node-msg` key ID is the fingerprint of its
registered public key (R12-6). Nothing here logs a key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server import signing

GATEWAY_KEY_SECRET = "gateway_signing_key"  # noqa: S105  the secret file's name, not a secret


@dataclass(frozen=True)
class GatewayKeys:
    gw_key: Ed25519PrivateKey
    gw_key_id: str
    core_key_id: str
    core_public: bytes

    def __post_init__(self) -> None:
        if len(self.core_public) != 32:
            raise ValueError("core public keys are raw 32-byte Ed25519 keys")

    @property
    def gw_public(self) -> bytes:
        return signing.public_bytes(self.gw_key)

    @property
    def current_core_key(self) -> tuple[str, bytes]:
        """The ACTIVE core key, announced to an enrolling node."""
        return self.core_key_id, self.core_public

    @property
    def ids(self) -> tuple[str, str]:
        return self.gw_key_id, self.core_key_id


def node_key_id(public_key: bytes) -> str:
    """The key ID a node signs its `node-msg` envelopes under (R12-6)."""
    return signing.fingerprint(public_key)


async def load_keys(conn: psycopg.AsyncConnection[Any], secrets_dir: Path) -> GatewayKeys:
    """The ACTIVE gateway key (with its private key) and core's ACTIVE public key.
    RuntimeError if either row is missing or the gateway's secret doesn't hold its key."""
    cur = await conn.execute(
        "SELECT role, key_id, public_key FROM gw.trusted_key WHERE state = 'ACTIVE'"
    )
    active = {
        str(role): (str(key_id), bytes(public)) for role, key_id, public in await cur.fetchall()
    }
    for role in ("GATEWAY", "CORE"):
        if role not in active:
            raise RuntimeError(f"no ACTIVE {role} key in gw.trusted_key")
    gw_key_id, gw_public = active["GATEWAY"]
    core_key_id, core_public = active["CORE"]
    gw_key = signing.load_rotating_key(secrets_dir, GATEWAY_KEY_SECRET, gw_key_id, gw_public)
    return GatewayKeys(
        gw_key=gw_key, gw_key_id=gw_key_id, core_key_id=core_key_id, core_public=core_public
    )
