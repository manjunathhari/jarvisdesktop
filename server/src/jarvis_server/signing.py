"""JCS + Ed25519 signing for node messages and core authorisations (spec §7.1).

Signed bytes: ``"jarvis-sig-v1\\n" + purpose + "\\n" + key_id + "\\n" + JCS(object)``.
Signature: Ed25519 over those bytes, base64url without padding.

The golden vectors in contracts/signing-vectors/ come from an independent generator;
this module must match them and never produces them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = b"jarvis-sig-v1\n"
SIG_FIELD = "sig"


class Purpose(StrEnum):
    NODE_MSG = "node-msg"
    GW_TRANSPORT = "gw-transport"
    CORE_AUTHZ = "core-authz"


class AuthzInvalid(Exception):
    """An authz whose key_id is not trusted or whose signature does not verify."""


def canonical(obj: Mapping[str, Any]) -> bytes:
    """RFC 8785 (JCS) serialisation."""
    return rfc8785.dumps(obj)


def signed_bytes(purpose: Purpose, key_id: str, obj: Mapping[str, Any]) -> bytes:
    if not key_id or "\n" in key_id:
        raise ValueError("key_id must be non-empty and contain no newline")
    head = f"{Purpose(purpose).value}\n{key_id}\n".encode()
    return PREFIX + head + canonical(obj)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: object, length: int) -> bytes | None:
    """Exactly ``length`` bytes, or None unless ``text`` is their canonical unpadded base64url.

    Strict: no padding, only the URL-safe alphabet, and no non-zero unused trailing bits
    (checked by re-encoding), so every value has exactly one accepted spelling.
    """
    if not isinstance(text, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != length or _b64url(raw) != text:
        return None
    return raw


def _decode_sig(sig: object) -> bytes | None:
    """The 64 signature bytes, or None unless ``sig`` is exactly their canonical base64url form."""
    return b64url_decode(sig, 64)


def sign(purpose: Purpose, key: Ed25519PrivateKey, key_id: str, obj: Mapping[str, Any]) -> str:
    return _b64url(key.sign(signed_bytes(purpose, key_id, obj)))


def verify(
    purpose: Purpose, public_key: bytes, key_id: str, obj: Mapping[str, Any], sig: str
) -> bool:
    """True only for a valid signature. Never raises on a bad signature, key or key_id."""
    raw = _decode_sig(sig)
    if raw is None:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(public_key)
        pub.verify(raw, signed_bytes(purpose, key_id, obj))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _without_sig(env: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in env.items() if k != SIG_FIELD}


def sign_envelope(
    purpose: Purpose, key: Ed25519PrivateKey, key_id: str, env: dict[str, Any]
) -> dict[str, Any]:
    """A copy of ``env`` with ``sig`` set over every other field."""
    body = _without_sig(env)
    return {**body, SIG_FIELD: sign(purpose, key, key_id, body)}


def verify_envelope(
    purpose: Purpose, public_key: bytes, key_id: str, env: Mapping[str, Any]
) -> bool:
    sig = env.get(SIG_FIELD)
    if not isinstance(sig, str):
        return False
    return verify(purpose, public_key, key_id, _without_sig(env), sig)


def make_authz(key: Ed25519PrivateKey, key_id: str, obj: Mapping[str, Any]) -> dict[str, Any]:
    """``{object, key_id, sig}``, signed for purpose ``core-authz``."""
    return {
        "object": dict(obj),
        "key_id": key_id,
        "sig": sign(Purpose.CORE_AUTHZ, key, key_id, obj),
    }


def check_authz(authz: Mapping[str, Any], trusted: Mapping[str, bytes]) -> dict[str, Any]:
    """The signed object, if ``key_id`` is trusted and the ``core-authz`` signature verifies."""
    key_id = authz.get("key_id")
    obj = authz.get("object")
    if not isinstance(key_id, str) or key_id not in trusted:
        raise AuthzInvalid("untrusted key_id")
    sig = authz.get("sig")
    if not isinstance(obj, Mapping) or not isinstance(sig, str):
        raise AuthzInvalid("malformed authz")
    if not verify(Purpose.CORE_AUTHZ, trusted[key_id], key_id, obj, sig):
        raise AuthzInvalid("bad signature")
    return dict(obj)


def new_nonce() -> str:
    """32 bytes from the OS's cryptographic random source, base64url without padding (43 chars)."""
    return _b64url(secrets.token_bytes(32))


def fingerprint(public_key: bytes) -> str:
    """First 10 bytes of SHA-256, lower-case base32 without padding (16 characters)."""
    return base64.b32encode(hashlib.sha256(public_key).digest()[:10]).decode("ascii").lower()


def load_private_key(pem_path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{pem_path} does not hold an Ed25519 private key")
    return key


def load_rotating_key(
    secrets_dir: Path, name: str, key_id: str, public_key: bytes
) -> Ed25519PrivateKey:
    """The private key of a trusted key (`gw.trusted_key`), from the secret `<name>_<key_id>`.

    For the bootstrap key only, the legacy secret `<name>` is used instead, and only when its
    public key's fingerprint is `key_id`. Raises RuntimeError naming the secret file (never its
    content) if neither holds the key whose public key is `public_key`.
    """
    if fingerprint(public_key) != key_id:
        raise RuntimeError(f"{name}: key_id {key_id} is not the fingerprint of its public key")
    named = secrets_dir / f"{name}_{key_id}"
    if named.exists():
        key = load_private_key(named)
    else:
        legacy = secrets_dir / name
        if not legacy.exists():
            raise RuntimeError(f"secret {named.name} is missing (and there is no {name})")
        key = load_private_key(legacy)
        if fingerprint(public_bytes(key)) != key_id:
            raise RuntimeError(f"secret {named.name} is missing, and {name} is not key {key_id}")
    if public_bytes(key) != public_key:
        raise RuntimeError(f"secret {named.name} does not match key {key_id}")
    return key


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    """The raw 32-byte public key."""
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
