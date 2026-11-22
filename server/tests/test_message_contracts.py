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


@pytest.mark.parametrize(
    "case",
    json.loads((CONTRACTS / "signing-vectors" / "authz-cases.json").read_text()),
    ids=lambda case: case["name"],
)
def test_authz_case_objects_are_valid_authz(case: dict[str, Any]) -> None:
    validator(_schema("authz")).validate(case["authz"])
    c.Authz.model_validate(case["authz"])
