"""Node admin on the core side (spec §7.2): enrolment codes, enrolment and revocation.

The enrolment code is ``"<token>.<core_fp>.<gw_fp>"``: a 128-bit random token (lower-case
base32, no padding: 26 characters) and the core and gateway key fingerprints
(`signing.fingerprint`, 16 characters each). Only the token's SHA-256 is stored.

Every call goes through `dbcall.audited_call`, so a refusal's audit row is committed before
the matching `Refused` exception is raised (R7-1).
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any
from uuid import UUID

import psycopg

from jarvis_server.dbcall import EnrolmentRefused, RevokeRefused, audited_call

KINDS = ("DESKTOP", "PHONE")
TOKEN_BYTES = 16  # 128 bits

_TOKEN_RE = re.compile(r"[a-z2-7]{26}")
_FP_RE = re.compile(r"[a-z2-7]{16}")


def new_token() -> str:
    return base64.b32encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii").rstrip("=").lower()


def token_hash(token: str) -> bytes:
    """SHA-256 of the token's UTF-8 bytes, as gw.enrol_node computes it."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _check_fingerprint(fp: str) -> None:
    if not _FP_RE.fullmatch(fp):
        raise ValueError("not a key fingerprint for an enrolment code")


def create_enrolment(
    conn: psycopg.Connection[Any],
    *,
    person_id: UUID,
    kind: str,
    name: str,
    core_fp: str,
    gw_fp: str,
) -> str:
    """Create a one-time enrolment (15 minutes, set by the database) and return its code."""
    _check_fingerprint(core_fp)
    _check_fingerprint(gw_fp)
    token = new_token()
    audited_call(
        conn,
        "SELECT gw.create_enrolment(%s, %s, %s, %s)",
        (token_hash(token), person_id, kind, name),
    )
    return f"{token}.{core_fp}.{gw_fp}"


def parse_enrolment_code(code: str) -> tuple[str, str, str]:
    """Split an enrolment code into (token, core_fp, gw_fp); ValueError if malformed."""
    parts = code.split(".")
    if (
        len(parts) != 3
        or not _TOKEN_RE.fullmatch(parts[0])
        or not all(_FP_RE.fullmatch(p) for p in parts[1:])
    ):
        raise ValueError("malformed enrolment code")
    return parts[0], parts[1], parts[2]


def enrol(
    conn: psycopg.Connection[Any], *, token: str, public_key: bytes, tailscale_identity: str
) -> UUID:
    """Register a new node with a valid, unused, unexpired token (as the gateway role)."""
    node_id, refusal = audited_call(
        conn,
        "SELECT node_id, refusal FROM gw.enrol_node(%s, %s, %s)",
        (token, public_key, tailscale_identity),
    )
    if refusal is not None:
        raise EnrolmentRefused(str(refusal))
    return UUID(str(node_id))


def revoke(conn: psycopg.Connection[Any], node_id: UUID) -> None:
    """Revoke a node and close its open connection (core role only)."""
    applied, refusal = audited_call(
        conn, "SELECT applied, refusal FROM gw.revoke_node(%s)", (node_id,)
    )
    if not applied:
        raise RevokeRefused(str(refusal))
