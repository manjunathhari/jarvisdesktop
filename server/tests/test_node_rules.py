"""The reference node rules (spec §6.1, §7.1, §7.2) against the shared cases."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from jarvis_server.signing import fingerprint

from .fakes.node_rules import (
    Rejected,
    SeqChecker,
    apply_keyset,
    check_challenge,
    check_enrolment_welcome,
    check_keyset,
    check_node_action,
)

CASES: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts" / "signing-vectors" / "authz-cases.json"
    ).read_text()
)
BY_NAME = {case["name"]: case for case in CASES}


def _keys(hex_map: dict[str, str] | None) -> dict[str, bytes] | None:
    return None if hex_map is None else {k: bytes.fromhex(v) for k, v in hex_map.items()}


def _run(case: dict[str, Any]) -> None:
    check = case["check"]
    if check == "node_action":
        check_node_action(
            case["authz"],
            trusted=_keys(case["trusted_keys"]) or {},
            me=UUID(case["receiving_node_id"]),
            now=datetime.fromisoformat(case["now"]),
        )
    elif check == "keyset":
        trusted = _keys(case["trusted_keys"]) or {}
        obj = check_keyset(
            case["payload"],
            case["authz"],
            trusted=trusted,
            me=UUID(case["receiving_node_id"]),
            now=datetime.fromisoformat(case["now"]),
        )
        assert obj == case["authz"]["object"]
        # The role picks the pin set; a gateway key never enters core trust (R6-3, S-9).
        core, gateway = dict(trusted), {}
        role = apply_keyset(obj, trusted_core=core, pinned_gateway=gateway, key_expiry={})
        assert role == case["pins_as"]
        new = case["payload"]["key_id"]
        assert (new in core) == (role == "core")
        assert (new in gateway) == (role == "gateway")
    elif check == "challenge":
        payload = check_challenge(
            case["envelope"],
            pinned_gateway=_keys(case["pinned_gateway"]),
            enrol_fingerprint=case["enrol_fingerprint"],
        )
        assert payload == case["envelope"]["payload"]
    elif check == "enrolment_welcome":
        node_id, key_id, public_key = check_enrolment_welcome(
            case["welcome"], core_fingerprint=case["core_fingerprint"]
        )
        block = case["welcome"]["enrolment"]
        assert (str(node_id), key_id) == (block["node_id"], block["core_key_id"])
        assert fingerprint(public_key) == case["core_fingerprint"] and len(public_key) == 32
    elif check == "seq":
        checker = SeqChecker()
        *before, last = case["seqs"]
        for seq in before:
            checker.expect(seq)
        checker.expect(last)
    else:
        raise AssertionError(f"unknown check {check!r}")


REQUIRED = {
    "accept-valid",
    "reject-gateway-key-only",
    "reject-wrong-purpose",
    "reject-other-target-node",
    "reject-expired",
    "accept-within-tolerance",
    "reject-untrusted-key-id",
    "reject-tampered-args",
    "reject-gateway-key-under-core-key-id",
    "accept-keyset",
    "reject-keyset-expired",
    "accept-keyset-within-tolerance",
    "reject-keyset-payload-mismatch",
    "reject-naive-valid-until",
    "reject-keyset-naive-valid-until",
    "accept-enrolment-welcome",
    "reject-enrolment-core-fingerprint-mismatch",
    "reject-enrolment-block-missing",
    "accept-consecutive",
    "reject-gap",
    "reject-repeat",
    "reject-decrease",
    "reject-first-not-1",
    "accept-challenge",
    "accept-challenge-enrol-by-fingerprint",
    "reject-challenge-unpinned-key",
    "reject-challenge-fingerprint-mismatch",
    "reject-challenge-bad-signature",
    "reject-challenge-signed-by-core-key",
    "reject-challenge-naive-time",
    "reject-non-canonical-base64url-key",
    "reject-keyset-other-target",
    "accept-keyset-for-this-node",
    "accept-keyset-gateway-role",
}


def test_case_set_has_the_required_cases() -> None:
    assert set(BY_NAME) >= REQUIRED
    assert len(BY_NAME) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_node_rules_match_authz_cases(case: dict[str, Any]) -> None:
    if case["expect"] == "accept":
        assert case["reason"] is None
        _run(case)
    else:
        with pytest.raises(Rejected) as excinfo:
            _run(case)
        assert excinfo.value.reason == case["reason"]


def test_specific_reasons() -> None:
    assert BY_NAME["reject-gateway-key-under-core-key-id"]["reason"] == "bad_signature"
    assert BY_NAME["reject-gateway-key-under-core-key-id"]["authz"]["key_id"] == "core-1"
    assert BY_NAME["reject-keyset-expired"]["reason"] == "expired"
    assert BY_NAME["reject-naive-valid-until"]["reason"] == "malformed"
    assert BY_NAME["reject-keyset-naive-valid-until"]["reason"] == "malformed"
    assert BY_NAME["reject-challenge-naive-time"]["reason"] == "malformed"
    assert BY_NAME["reject-keyset-other-target"]["reason"] == "wrong_target"
    assert BY_NAME["accept-keyset-gateway-role"]["pins_as"] == "gateway"


def test_every_keyset_case_names_its_node_and_every_accepted_one_its_pin_set() -> None:
    for case in CASES:
        if case["check"] == "keyset":
            assert UUID(case["receiving_node_id"])
            assert ("pins_as" in case) == (case["expect"] == "accept")
            if case["expect"] == "accept":
                assert case["pins_as"] == case["payload"]["role"]


def test_seq_reject_cases_pass_every_value_but_the_last() -> None:
    for case in CASES:
        if case["check"] == "seq" and case["expect"] == "reject":
            checker = SeqChecker()
            for seq in case["seqs"][:-1]:
                checker.expect(seq)
            with pytest.raises(Rejected):
                checker.expect(case["seqs"][-1])


def test_seq_checker_stays_closed_after_a_failure() -> None:
    checker = SeqChecker()
    checker.expect(1)
    with pytest.raises(Rejected):
        checker.expect(3)
    with pytest.raises(Rejected):
        checker.expect(2)


@pytest.mark.parametrize("field", ["role", "key_id", "public_key"])
def test_keyset_rejects_any_payload_field_that_differs(field: str) -> None:
    case = BY_NAME["accept-keyset"]
    other = {"role": "gateway", "key_id": "core-3", "public_key": "A" * 43}
    with pytest.raises(Rejected) as excinfo:
        check_keyset(
            {**case["payload"], field: other[field]},
            case["authz"],
            trusted=_keys(case["trusted_keys"]) or {},
            me=UUID(case["receiving_node_id"]),
            now=datetime.fromisoformat(case["now"]),
        )
    assert excinfo.value.reason == "keyset_mismatch"


def test_keyset_rejects_an_action_authz() -> None:
    action = BY_NAME["accept-valid"]
    keyset = BY_NAME["accept-keyset"]
    with pytest.raises(Rejected) as excinfo:
        check_keyset(
            keyset["payload"],
            action["authz"],
            trusted=_keys(action["trusted_keys"]) or {},
            me=UUID(keyset["receiving_node_id"]),
            now=datetime.fromisoformat(keyset["now"]),
        )
    assert excinfo.value.reason == "wrong_kind"


def test_challenge_must_be_a_challenge() -> None:
    case = BY_NAME["accept-challenge"]
    with pytest.raises(Rejected) as excinfo:
        check_challenge(
            {**case["envelope"], "kind": "welcome"},
            pinned_gateway=_keys(case["pinned_gateway"]),
            enrol_fingerprint=None,
        )
    assert excinfo.value.reason == "malformed"


def test_challenge_needs_a_pinned_key_or_a_fingerprint() -> None:
    with pytest.raises(ValueError):
        check_challenge(
            BY_NAME["accept-challenge"]["envelope"], pinned_gateway=None, enrol_fingerprint=None
        )


def test_malformed_enrolment_block_is_rejected() -> None:
    case = BY_NAME["accept-enrolment-welcome"]
    welcome = {**case["welcome"], "enrolment": {**case["welcome"]["enrolment"], "node_id": "x"}}
    with pytest.raises(Rejected) as excinfo:
        check_enrolment_welcome(welcome, core_fingerprint=case["core_fingerprint"])
    assert excinfo.value.reason == "malformed"


def test_malformed_authz_is_rejected() -> None:
    with pytest.raises(Rejected) as excinfo:
        check_node_action(
            {"object": {}, "key_id": "core-1", "sig": "x"},
            trusted={},
            me=UUID(int=1),
            now=datetime.fromisoformat("2026-11-22T10:00:00Z"),
        )
    assert excinfo.value.reason == "malformed"


def test_non_canonical_base64url_key_is_rejected_not_normalised() -> None:
    bad = BY_NAME["reject-non-canonical-base64url-key"]
    good = BY_NAME["accept-enrolment-welcome"]
    bad_key = bad["welcome"]["enrolment"]["core_public_key"]
    good_key = good["welcome"]["enrolment"]["core_public_key"]
    assert bad_key[:-1] == good_key[:-1] and bad_key != good_key
    with pytest.raises(Rejected) as excinfo:
        check_enrolment_welcome(bad["welcome"], core_fingerprint=bad["core_fingerprint"])
    assert excinfo.value.reason == "malformed_key"


def test_challenge_with_a_non_canonical_key_is_rejected() -> None:
    case = BY_NAME["accept-challenge"]
    env = case["envelope"]
    key = env["payload"]["public_key"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    flipped = key[:-1] + alphabet[alphabet.index(key[-1]) | 1]
    assert flipped != key
    with pytest.raises(Rejected) as excinfo:
        check_challenge(
            {**env, "payload": {**env["payload"], "public_key": flipped}},
            pinned_gateway=_keys(case["pinned_gateway"]),
            enrol_fingerprint=None,
        )
    assert excinfo.value.reason == "malformed_key"
