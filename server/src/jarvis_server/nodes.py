"""Node admin on the core side (spec §7.1, §7.2): enrolment codes, enrolment, revocation and
re-enrolment (R5-1).

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
from psycopg.pq import TransactionStatus

from jarvis_server.dbcall import EnrolmentRefused, RevokeRefused, TransactionNotIdle, audited_call

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


def reenrol(conn: psycopg.Connection[Any], node_id: UUID, core_fp: str, gw_fp: str) -> str:
    """Revoke a node and create a fresh enrolment for the same person, kind and name, in one
    transaction; return the new enrolment code (spec §7.1, R5-1; Task 12 ruling 6).

    `core_fp` and `gw_fp` are the current ACTIVE fingerprints. The old node's open items
    addressed to it alone (`target_node_id`, such as `action.approved` and `keys.keyset`) are
    superseded, never re-targeted, because their signed authz names the old node.
    Person-addressed items reach the new node through the normal rule. A revocation refusal
    (`unknown_node`, `already_revoked`) commits its audit row, creates nothing and raises
    `RevokeRefused`.
    """
    _check_fingerprint(core_fp)
    _check_fingerprint(gw_fp)
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise TransactionNotIdle("reenrol runs in its own transaction")
    token = new_token()
    with conn.transaction():
        row = conn.execute(
            "SELECT person_id, kind, name FROM gw.node WHERE id = %s", (node_id,)
        ).fetchone()
        applied, refusal = _row(
            conn.execute("SELECT applied, refusal FROM gw.revoke_node(%s)", (node_id,)).fetchone()
        )
        if applied:
            assert row is not None  # revoke_node applies to a registered node only
            for (item,) in conn.execute(
                "SELECT id FROM comm.outbox WHERE target_node_id = %s"
                " AND state IN ('PENDING', 'DELIVERING', 'DELIVERED') ORDER BY created_at, id",
                (node_id,),
            ).fetchall():
                conn.execute("SELECT comm.supersede(%s)", (item,))
            conn.execute(
                "SELECT gw.create_enrolment(%s, %s, %s, %s)",
                (token_hash(token), row[0], row[1], row[2]),
            )
    if not applied:  # the refusal's audit row has committed
        raise RevokeRefused(str(refusal))
    return f"{token}.{core_fp}.{gw_fp}"


def _row(row: Any) -> tuple[Any, ...]:
    if row is None:
        raise RuntimeError("gw.revoke_node returned no row")
    return tuple(row)
