"""The reference node authorisation rules (spec §7.1) against the shared authz cases."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from .fakes.node_rules import Rejected, check_node_action

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
    kwargs = {
        "trusted": trusted,
        "me": UUID(case["receiving_node_id"]),
        "now": datetime.fromisoformat(case["now"]),
    }
    if case["expect"] == "accept":
        check_node_action(case["authz"], **kwargs)
    else:
        with pytest.raises(Rejected) as excinfo:
            check_node_action(case["authz"], **kwargs)
        assert excinfo.value.reason == case["reason"]


def test_malformed_authz_is_rejected() -> None:
    with pytest.raises(Rejected) as excinfo:
        check_node_action(
            {"object": {}, "key_id": "core-1", "sig": "x"},
            trusted={},
            me=UUID(int=1),
            now=datetime.fromisoformat("2026-11-22T10:00:00Z"),
        )
    assert excinfo.value.reason == "malformed"
