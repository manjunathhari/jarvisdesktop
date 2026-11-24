"""Core's own check of a node's signature before an irreversible step (spec §9.3, §9.4; R12-34).

The gateway verifies every envelope, but core doesn't take its word for the messages that can
approve, confirm or verify an IRREVERSIBLE action: an `approval.decision`, an approved action's
receipt, and the listings that propose or verify a delete. `conv.record_inbound` stores the
node's complete signed envelope with each message (`conv.inbound.envelope`), and before acting
core checks, against that row:

- the envelope's `node-msg` signature verifies under the node's registered key,
  `gw.node.public_key`, with `key_id = fingerprint(public_key)` (R12-6);
- its `node_id`, `kind`, `message_id` and `payload` equal the stored row's.

A missing or failing envelope is refused through the existing audited refusals, with code
`bad_node_signature`. A compromised gateway therefore can't approve, confirm or verify an
irreversible action on a node's behalf. Nothing here logs content.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg

from jarvis_server import signing

BAD_NODE_SIGNATURE = "bad_node_signature"


def _same_uuid(value: object, expected: UUID) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return UUID(value) == expected
    except ValueError:
        return False


def _same_json(a: Any, b: Any) -> bool:
    """Equal as JSON values: the same canonical (JCS) bytes."""
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return False
    try:
        return signing.canonical(a) == signing.canonical(b)
    except (ValueError, TypeError):
        return False


def signed_by_node(
    conn: psycopg.Connection[Any],
    *,
    inbound_id: UUID,
    node_id: UUID,
    kind: str,
    payload: Any,
    envelope: Any,
) -> bool:
    """True only if `envelope` is the node's own valid `node-msg` signature over this stored
    inbound message (its node, kind, message ID and payload)."""
    if not isinstance(envelope, Mapping):
        return False
    if not (
        _same_uuid(envelope.get("node_id"), node_id)
        and _same_uuid(envelope.get("message_id"), inbound_id)
        and envelope.get("kind") == kind
        and _same_json(envelope.get("payload"), payload)
    ):
        return False
    row = conn.execute("SELECT public_key FROM gw.node WHERE id = %s", (node_id,)).fetchone()
    if row is None:
        return False
    public_key = bytes(row[0])
    return signing.verify_envelope(
        signing.Purpose.NODE_MSG, public_key, signing.fingerprint(public_key), envelope
    )
