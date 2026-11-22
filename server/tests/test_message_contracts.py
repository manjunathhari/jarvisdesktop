"""Node message payloads: each example passes (or fails) both the JSON Schema and the model."""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from jarvis_server import contracts as c

from .schema_registry import CONTRACTS, validator

MESSAGES = CONTRACTS / "messages"
EXAMPLES = CONTRACTS / "examples" / "messages"
NAMES = sorted(c.MESSAGE_MODELS)

KINDS = [
    "enrol",
    "hello",
    "welcome",
    "utterance",
    "reply.chunk",
    "outbox.item",
    "outbox.cleared",
    "received",
    "ack",
    "approval.decision",
    "node.action",
    "action.receipt",
    "keyset",
    "ping",
    "pong",
    "error",
]


def _schema(name: str) -> dict[str, Any]:
    return json.loads((MESSAGES / f"{name}.schema.json").read_text())


def _example(name: str, kind: str) -> Any:
    return json.loads((EXAMPLES / f"{name}.{kind}.json").read_text())


def test_every_message_schema_has_a_model() -> None:
    on_disk = {p.name.removesuffix(".schema.json") for p in MESSAGES.glob("*.schema.json")}
    assert on_disk == set(NAMES) == {*KINDS, "authz"}
    assert c.MESSAGE_MODELS["authz"] is c.Authz


def test_envelope_kind_enum_is_exactly_the_message_kinds() -> None:
    envelope = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
    assert envelope["properties"]["kind"]["enum"] == KINDS
    assert {"seq", "sig"} <= set(envelope["required"])


@pytest.mark.parametrize("name", NAMES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_schema(name))


@pytest.mark.parametrize("name", NAMES)
def test_valid_example_passes_schema_and_model(name: str) -> None:
    example = _example(name, "valid")
    validator(_schema(name)).validate(example)
    c.MESSAGE_MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", NAMES)
def test_invalid_example_fails_schema_and_model(name: str) -> None:
    example = _example(name, "invalid")
    assert not validator(_schema(name)).is_valid(example)
    with pytest.raises(ValidationError):
        c.MESSAGE_MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", [n for n in NAMES if "properties" in _schema(n)])
def test_model_fields_match_schema_properties(name: str) -> None:
    schema = _schema(name)
    model = c.MESSAGE_MODELS[name]
    assert set(model.model_fields) == set(schema["properties"])
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert required == set(schema["required"])


def test_authz_object_fields_are_exactly_the_spec_list() -> None:
    fields = _schema("authz")["properties"]["object"]["properties"]
    assert list(fields) == [
        "v",
        "kind",
        "action_kind",
        "args",
        "target_node_id",
        "person_id",
        "approval_id",
        "execution_key",
        "reply_id",
        "summary",
        "issued_at",
        "valid_until",
    ]
    assert fields["kind"]["enum"] == [k.value for k in c.AuthzKind]
    assert set(c.AuthzObject.model_fields) == set(fields)


SPEECH = {"reply_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "seq": 1, "final": False}


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({**SPEECH, "type": "SPEECH", "text": "Hello", "authz": None}, True),
        ({**SPEECH, "type": "SPEECH", "text": None, "authz": None}, False),
        ({**SPEECH, "type": "ACTION", "text": "Hello", "authz": None}, False),
    ],
)
def test_reply_chunk_type_decides_text_or_authz(payload: dict[str, Any], ok: bool) -> None:
    assert validator(_schema("reply.chunk")).is_valid(payload) is ok
    if ok:
        c.MESSAGE_MODELS["reply.chunk"].model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            c.MESSAGE_MODELS["reply.chunk"].model_validate(payload)


@pytest.mark.parametrize("name", ["received", "ack"])
@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"outbox_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"}, True),
        ({"reply_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "seq": 3}, True),
        ({"reply_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"}, False),
        (
            {
                "outbox_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                "reply_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                "seq": 3,
            },
            False,
        ),
    ],
)
def test_received_and_ack_take_an_outbox_id_or_a_reply_position(
    name: str, payload: dict[str, Any], ok: bool
) -> None:
    assert validator(_schema(name)).is_valid(payload) is ok
    if ok:
        c.MESSAGE_MODELS[name].model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            c.MESSAGE_MODELS[name].model_validate(payload)


def test_signed_vectors_envelope_is_a_valid_envelope_body() -> None:
    vectors = json.loads((CONTRACTS / "signing-vectors" / "vectors.json").read_text())
    env = next(v for v in vectors if v["name"] == "envelope-utterance")
    full = {**env["object"], "sig": env["expected_sig"]}
    envelope_schema = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
    validator(envelope_schema).validate(full)
    c.NodeEnvelope.model_validate(full)
    c.MESSAGE_MODELS[full["kind"]].model_validate(full["payload"])


AUTHZ_CASES = json.loads((CONTRACTS / "signing-vectors" / "authz-cases.json").read_text())


@pytest.mark.parametrize("case", AUTHZ_CASES, ids=lambda case: case["name"])
def test_authz_case_objects_match_the_contract(case: dict[str, Any]) -> None:
    ok = case["reason"] != "malformed"
    assert validator(_schema("authz")).is_valid(case["authz"]) is ok
    if ok:
        c.Authz.model_validate(case["authz"])
    else:
        with pytest.raises(ValidationError):
            c.Authz.model_validate(case["authz"])
    if "payload" in case:
        payload = {**case["payload"], "authz": case["authz"]}
        validator(_schema("keyset")).validate(payload)
        c.Keyset.model_validate(payload)


def test_date_time_format_is_actually_checked() -> None:
    assert "date-time" in jsonschema.FormatChecker().checkers
    schema = _schema("outbox.cleared")  # any schema: the validator's format checker is shared
    check = validator(schema).format_checker
    assert check is not None
    assert not check.conforms("not-a-date", "date-time")
    assert not check.conforms("2026-11-22T10:02:00", "date-time")
    assert check.conforms("2026-11-22T10:02:00Z", "date-time")


@pytest.mark.parametrize("model", ["authz", "outbox.item"])
def test_naive_timestamps_fail_schema_and_model(model: str) -> None:
    example = _example(model, "valid")
    if model == "authz":
        example = {**example, "object": {**example["object"], "valid_until": "2026-11-22T10:10:00"}}
    else:
        example = {**example, "expires_at": "2026-11-22T10:10:00"}
    assert not validator(_schema(model)).is_valid(example)
    with pytest.raises(ValidationError):
        c.MESSAGE_MODELS[model].model_validate(example)


def test_envelope_naive_sent_at_and_non_uuid_node_id_fail() -> None:
    envelope_schema = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
    example = json.loads((CONTRACTS / "examples" / "node-envelope.valid.json").read_text())
    for bad in ({"sent_at": "2026-11-21T10:00:00"}, {"node_id": "desktop-manj"}):
        broken = {**example, **bad}
        assert not validator(envelope_schema).is_valid(broken)
        with pytest.raises(ValidationError):
            c.NodeEnvelope.model_validate(broken)
