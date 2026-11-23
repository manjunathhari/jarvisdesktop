"""Core's signing key for `core-authz` (spec §7.1).

Temporary single-key version (Task 11): the one secret `core_signing_key` (a PEM Ed25519
private key, read through `config.secret`), with `key_id = fingerprint(public key)`. Task 12
replaces the body with the rotating key set and keeps the signature. The key is never logged.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server import config, signing

CORE_KEY_SECRET = "core_signing_key"  # noqa: S105  the secret file's name, not a secret


def active_core_key() -> tuple[Ed25519PrivateKey, str]:
    """(the active core signing key, its key_id)."""
    key = serialization.load_pem_private_key(
        config.secret(CORE_KEY_SECRET).encode("ascii"), password=None
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"secret {CORE_KEY_SECRET} does not hold an Ed25519 private key")
    return key, signing.fingerprint(signing.public_bytes(key))
