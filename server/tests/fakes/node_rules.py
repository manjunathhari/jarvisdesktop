"""Reference checkers for the node-side rules (spec §6.1, §7.1, §7.2).

Written once here and tested against contracts/signing-vectors/authz-cases.json, which are
shared with the Rust node:

- ``check_node_action``: rules 2 to 4 for a ``node.action``. Rule 1 (a valid ``gw-transport``
  envelope on the authenticated connection) is the transport's job; rule 5 (an unused
  ``execution_key`` for IRREVERSIBLE actions) needs the node's journal, so the fake node
  checks it on top of this.
- ``check_keyset``: a ``keyset`` announcement (R6-2).
- ``check_challenge``: the gateway's first message on a connection (§6.1 step 3, R6-1).
- ``check_enrolment_welcome``: core-key pinning after an enrolment (§7.2 step 5, R6-6).
- ``SeqChecker``: the peer's per-connection ``seq`` (R6-8).

``trusted`` always holds CORE keys only, and ``pinned_gateway`` GATEWAY keys only, so a key of
one role is never accepted for the other (R6-3).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from jarvis_server.contracts import (
    Authz,
    AuthzKind,
    Challenge,
    Keyset,
    MessageKind,
    NodeEnvelope,
    Welcome,
)
from jarvis_server.signing import Purpose, fingerprint, verify, verify_envelope

CLOCK_TOLERANCE = timedelta(seconds=30)


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _raw_key(b64url: str) -> bytes:
    return base64.urlsafe_b64decode(b64url + "=")


def _verified(authz: Mapping[str, Any], trusted: Mapping[str, bytes]) -> Authz:
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
    return parsed


def check_node_action(
    authz: Mapping[str, Any], *, trusted: Mapping[str, bytes], me: UUID, now: datetime
) -> None:
    """Raise ``Rejected(reason)`` unless the node may run this ``node.action``.

    Reasons, checked in this order: ``malformed``, ``untrusted_key_id``, ``bad_signature``,
    ``wrong_kind``, ``wrong_target``, ``expired``.
    """
    parsed = _verified(authz, trusted)
    obj = parsed.object
    if obj.kind is not AuthzKind.NODE_ACTION:
        raise Rejected("wrong_kind")
    if obj.target_node_id != me:
        raise Rejected("wrong_target")
    if now > obj.valid_until + CLOCK_TOLERANCE:
        raise Rejected("expired")


KEYSET_FIELDS = ("role", "key_id", "public_key")


def check_keyset(
    payload: Mapping[str, Any],
    authz: Mapping[str, Any],
    *,
    trusted: Mapping[str, bytes],
    now: datetime,
) -> dict[str, Any]:
    """The signed keyset object, or ``Rejected(reason)``.

    ``payload`` is the ``keyset`` message payload; its own ``authz`` key, if present, is
    ignored in favour of ``authz``. Validity comes only from the signed ``authz.object``
    (R6-2). Reasons, in order: ``malformed``, ``untrusted_key_id``, ``bad_signature``,
    ``wrong_kind``, ``expired`` (``now`` > ``valid_until`` + 30 s), ``keyset_mismatch`` (the
    payload's role, key_id or public_key differs from ``authz.object.args``).
    """
    try:
        Keyset.model_validate({**payload, "authz": authz})
    except ValidationError:
        raise Rejected("malformed") from None
    parsed = _verified(authz, trusted)
    if parsed.object.kind is not AuthzKind.KEYSET:
        raise Rejected("wrong_kind")
    if now > parsed.object.valid_until + CLOCK_TOLERANCE:
        raise Rejected("expired")
    args = parsed.object.args
    if any(args.get(f) != payload[f] for f in KEYSET_FIELDS) or set(args) != set(KEYSET_FIELDS):
        raise Rejected("keyset_mismatch")
    return dict(authz["object"])


def check_challenge(
    env: Mapping[str, Any],
    *,
    pinned_gateway: Mapping[str, bytes] | None,
    enrol_fingerprint: str | None,
) -> dict[str, Any]:
    """The challenge payload, or ``Rejected(reason)`` (spec §6.1 step 3).

    An enrolled node passes its pinned GATEWAY keys; a device enrolling passes
    ``pinned_gateway=None`` and the gateway fingerprint from its enrolment code. That the
    challenge is the first message (``seq`` = 1) is ``SeqChecker``'s job. Server time is
    advisory, so expiry is not checked here. Reasons, in order: ``malformed`` (including any
    kind other than ``challenge``), ``unpinned_key``, ``fingerprint_mismatch``,
    ``bad_signature``.
    """
    if pinned_gateway is None and enrol_fingerprint is None:
        raise ValueError("pass the pinned gateway keys or the enrolment fingerprint")
    try:
        envelope = NodeEnvelope.model_validate(env)
        challenge = Challenge.model_validate(env["payload"])
    except ValidationError:
        raise Rejected("malformed") from None
    if envelope.kind is not MessageKind.CHALLENGE:
        raise Rejected("malformed")
    public_key = _raw_key(challenge.public_key)
    if pinned_gateway is not None:
        if pinned_gateway.get(challenge.key_id) != public_key:
            raise Rejected("unpinned_key")
    elif fingerprint(public_key) != enrol_fingerprint:
        raise Rejected("fingerprint_mismatch")
    if not verify_envelope(Purpose.GW_TRANSPORT, public_key, challenge.key_id, env):
        raise Rejected("bad_signature")
    return dict(env["payload"])


def check_enrolment_welcome(
    welcome_payload: Mapping[str, Any], *, core_fingerprint: str
) -> tuple[UUID, str, bytes]:
    """``(node_id, core_key_id, core_public_key)`` to pin, or ``Rejected(reason)``.

    Reasons, in order: ``malformed``, ``enrolment_missing``, ``core_fingerprint_mismatch``.
    On any rejection the device discards its new keys and stores nothing (§7.2 step 5).
    """
    try:
        welcome = Welcome.model_validate(welcome_payload)
    except ValidationError:
        raise Rejected("malformed") from None
    block = welcome.enrolment
    if block is None:
        raise Rejected("enrolment_missing")
    public_key = _raw_key(block.core_public_key)
    if fingerprint(public_key) != core_fingerprint:
        raise Rejected("core_fingerprint_mismatch")
    return block.node_id, block.core_key_id, public_key


class SeqChecker:
    """The peer's ``seq`` on one connection: 1, then exactly +1 each message (R6-8).

    After a failure the connection is closed, so every later call fails too.
    """

    def __init__(self) -> None:
        self._next = 1
        self._failed = False

    def expect(self, peer_seq: int) -> None:
        if self._failed or peer_seq != self._next:
            self._failed = True
            raise Rejected("bad_seq")
        self._next += 1
