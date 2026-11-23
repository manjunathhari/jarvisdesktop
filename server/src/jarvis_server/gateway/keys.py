"""The gateway's key material (spec §4.3, §7.1, R12-6, R12-7).

The gateway signs `gw-transport` envelopes with `gateway_signing_key` under `gw_key_id`. It holds
core's *public* keys only, to hand to a newly enrolled node in `welcome.enrolment`, which the node
pins only if the key matches its enrolment code (§7.2 step 5). It never reads
`core_signing_key`. Key IDs are `fingerprint(public)`; a node's `node-msg` key ID is the
fingerprint of its registered public key (R12-6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server import signing

GATEWAY_KEY_FILE = "gateway_signing_key"
CORE_PUBLIC_KEY_FILE = "core_signing_key.pub"  # raw 32-byte public key, not secret (R12-7)


@dataclass(frozen=True)
class GatewayKeys:
    gw_key: Ed25519PrivateKey
    gw_key_id: str
    core_public_keys: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if not self.core_public_keys:
            raise ValueError("the gateway needs core's current public key")
        if any(len(k) != 32 for k in self.core_public_keys.values()):
            raise ValueError("core public keys are raw 32-byte Ed25519 keys")

    @property
    def gw_public(self) -> bytes:
        return signing.public_bytes(self.gw_key)

    @property
    def current_core_key(self) -> tuple[str, bytes]:
        """The core key announced to an enrolling node: the first of `core_public_keys`.
        Task 12 replaces this with the ACTIVE key from `gw.trusted_key`."""
        key_id, public = next(iter(self.core_public_keys.items()))
        return key_id, public


def node_key_id(public_key: bytes) -> str:
    """The key ID a node signs its `node-msg` envelopes under (R12-6)."""
    return signing.fingerprint(public_key)


def load_keys(secrets_dir: Path) -> GatewayKeys:
    """The gateway key and core's public key from the secrets directory."""
    gw_key = signing.load_private_key(secrets_dir / GATEWAY_KEY_FILE)
    core_path = secrets_dir / CORE_PUBLIC_KEY_FILE
    try:
        core_public = core_path.read_bytes()
    except FileNotFoundError as e:
        raise RuntimeError(f"{core_path} is missing") from e
    if len(core_public) != 32:
        raise RuntimeError(f"{core_path} must hold a raw 32-byte Ed25519 public key")
    return GatewayKeys(
        gw_key=gw_key,
        gw_key_id=signing.fingerprint(signing.public_bytes(gw_key)),
        core_public_keys={signing.fingerprint(core_public): core_public},
    )
