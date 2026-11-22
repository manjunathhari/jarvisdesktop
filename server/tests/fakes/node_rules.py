"""Reference checker for the node-side authorisation rules (spec §7.1, rules 2 to 4).

Written once here and tested against contracts/signing-vectors/authz-cases.json, which are
shared with the Rust node. Rule 1 (a valid ``gw-transport`` envelope on the authenticated
connection) is the transport's job; rule 5 (an unused ``execution_key`` for IRREVERSIBLE
actions) needs the node's journal, so the fake node checks it on top of this.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from jarvis_server.contracts import Authz, AuthzKind
from jarvis_server.signing import Purpose, verify

CLOCK_TOLERANCE = timedelta(seconds=30)


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_node_action(
    authz: Mapping[str, Any], *, trusted: Mapping[str, bytes], me: UUID, now: datetime
) -> None:
    """Raise ``Rejected(reason)`` unless the node may run this ``node.action``.

    Reasons, checked in this order: ``malformed``, ``untrusted_key_id``, ``bad_signature``,
    ``wrong_kind``, ``wrong_target``, ``expired``.
    """
    try:
        parsed = Authz.model_validate(authz)
    except ValidationError:
        raise Rejected("malformed") from None
    if parsed.key_id not in trusted:
        raise Rejected("untrusted_key_id")
    # Verify over the raw object exactly as received, never the model re-serialised.
    if not verify(
        Purpose.CORE_AUTHZ, trusted[parsed.key_id], parsed.key_id, authz["object"], parsed.sig
    ):
        raise Rejected("bad_signature")
    obj = parsed.object
    if obj.kind is not AuthzKind.NODE_ACTION:
        raise Rejected("wrong_kind")
    if obj.target_node_id != me:
        raise Rejected("wrong_target")
    if now > obj.valid_until + CLOCK_TOLERANCE:
        raise Rejected("expired")
