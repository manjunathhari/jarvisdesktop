"""The reference node authorisation rules (spec §7.1) against the shared authz cases."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from .fakes.node_rules import Rejected, check_keyset, check_node_action

CASES: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts" / "signing-vectors" / "authz-cases.json"
    ).read_text()
)


def test_case_set_has_the_required_cases() -> None:
    names = {case["name"] for case in CASES}
    assert {
        "accept-valid",
        "reject-gateway-key-only",
        "reject-wrong-purpose",
        "reject-other-target-node",
        "reject-expired",
        "accept-within-tolerance",
        "reject-untrusted-key-id",
        "reject-tampered-args",
    } <= names


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_node_rules_match_authz_cases(case: dict[str, Any]) -> None:
    trusted = {k: bytes.fromhex(v) for k, v in case["trusted_keys"].items()}

    def check() -> None:
        if "payload" in case:  # a keyset announcement
            obj = check_keyset(case["payload"], case["authz"], trusted=trusted)
            assert obj == case["authz"]["object"]
        else:
            check_node_action(
                case["authz"],
                trusted=trusted,
                me=UUID(case["receiving_node_id"]),
                now=datetime.fromisoformat(case["now"]),
            )

    if case["expect"] == "accept":
        check()
    else:
        with pytest.raises(Rejected) as excinfo:
            check()
        assert excinfo.value.reason == case["reason"]


def test_case_set_covers_naive_times_and_keyset_mismatch() -> None:
    by_name = {case["name"]: case for case in CASES}
    assert by_name["reject-naive-valid-until"]["reason"] == "malformed"
    assert by_name["reject-keyset-payload-mismatch"]["reason"] == "keyset_mismatch"
    assert by_name["accept-keyset"]["expect"] == "accept"


@pytest.mark.parametrize("field", ["role", "key_id", "public_key"])
def test_keyset_rejects_any_payload_field_that_differs(field: str) -> None:
    case = next(c for c in CASES if c["name"] == "accept-keyset")
    trusted = {k: bytes.fromhex(v) for k, v in case["trusted_keys"].items()}
    other = {"role": "gateway", "key_id": "core-3", "public_key": "A" * 43}
    payload = {**case["payload"], field: other[field]}
    with pytest.raises(Rejected) as excinfo:
        check_keyset(payload, case["authz"], trusted=trusted)
    assert excinfo.value.reason == "keyset_mismatch"


def test_keyset_rejects_an_action_authz() -> None:
    action = next(c for c in CASES if c["name"] == "accept-valid")
    keyset = next(c for c in CASES if c["name"] == "accept-keyset")
    trusted = {k: bytes.fromhex(v) for k, v in action["trusted_keys"].items()}
    with pytest.raises(Rejected) as excinfo:
        check_keyset(keyset["payload"], action["authz"], trusted=trusted)
    assert excinfo.value.reason == "wrong_kind"


def test_malformed_authz_is_rejected() -> None:
    with pytest.raises(Rejected) as excinfo:
        check_node_action(
            {"object": {}, "key_id": "core-1", "sig": "x"},
            trusted={},
            me=UUID(int=1),
            now=datetime.fromisoformat("2026-11-22T10:00:00Z"),
        )
    assert excinfo.value.reason == "malformed"
