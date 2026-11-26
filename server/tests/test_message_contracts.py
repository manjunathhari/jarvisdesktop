"""Node message payloads: each example passes (or fails) both the JSON Schema and the model."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from jarvis_server import contracts as c

from .schema_registry import CONTRACTS, validator

MESSAGES = CONTRACTS / "messages"
EXAMPLES = CONTRACTS / "examples" / "messages"
MODELS: dict[str, Any] = {**c.MESSAGE_MODELS, **c.DESKTOP_MODELS}
NAMES = sorted(MODELS)
DESKTOP = [
    "pc.list_test_folder.args",
    "pc.delete_test_file.args",
    "pc.list_test_folder.receipt",
    "pc.delete_test_file.receipt",
    # The Board (slice 1): the live actions' `authz.object.args` and their receipts.
    "board.show.args",
    "board.update.args",
    "board.set_state.args",
    "board.focus.args",
    "board.show.receipt",
    "board.set_state.receipt",
]

KINDS = [
    "challenge",
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
    "board.state",
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
    assert on_disk == set(NAMES) == {*KINDS, "authz", *DESKTOP}
    assert not set(c.DESKTOP_MODELS) & set(c.MESSAGE_MODELS)  # never a message kind
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
    MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", NAMES)
def test_invalid_example_fails_schema_and_model(name: str) -> None:
    example = _example(name, "invalid")
    assert not validator(_schema(name)).is_valid(example)
    with pytest.raises(ValidationError):
        MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", [n for n in NAMES if "properties" in _schema(n)])
def test_model_fields_match_schema_properties(name: str) -> None:
    schema = _schema(name)
    model = MODELS[name]
    assert set(model.model_fields) == set(schema["properties"])
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert required == set(schema.get("required", []))


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


SPEECH = {
    "reply_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "seq": 1,
    "final": False,
    "in_reply_to": None,
}


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
def test_shared_case_inputs_match_the_contracts(case: dict[str, Any]) -> None:
    ok = case["reason"] != "malformed"
    if case["check"] == "keyset" and not ok and validator(_schema("authz")).is_valid(case["authz"]):
        # The malformed part is the keyset payload itself (e.g. an unknown role), not its authz:
        # the message must fail the keyset schema and the model.
        message = {
            **case["payload"],
            "authz": case["authz"],
            "outbox_id": "6f1c2b9e-3d4a-4e5f-9a8b-7c6d5e4f3a2b",
        }
        assert not validator(_schema("keyset")).is_valid(message)
        with pytest.raises(ValidationError):
            c.KeysetAnnouncement.model_validate({**case["payload"], "authz": case["authz"]})
        return
    if "authz" in case:
        assert validator(_schema("authz")).is_valid(case["authz"]) is ok
        if ok:
            c.Authz.model_validate(case["authz"])
        else:
            with pytest.raises(ValidationError):
                c.Authz.model_validate(case["authz"])
    if case["check"] == "keyset" and ok:
        # The message adds the unsigned outbox_id; the case payload (what check_keyset gets)
        # leaves it out (R12-26).
        payload = {**case["payload"], "authz": case["authz"]}
        c.KeysetAnnouncement.model_validate(payload)
        message = {**payload, "outbox_id": "6f1c2b9e-3d4a-4e5f-9a8b-7c6d5e4f3a2b"}
        validator(_schema("keyset")).validate(message)
        c.Keyset.model_validate(message)
        assert not validator(_schema("keyset")).is_valid(payload)  # outbox_id is required
    if case["check"] == "challenge":
        envelope_schema = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
        validator(envelope_schema).validate(case["envelope"])
        c.NodeEnvelope.model_validate(case["envelope"])
        assert validator(_schema("challenge")).is_valid(case["envelope"]["payload"]) is ok
    if case["check"] == "enrolment_welcome":
        validator(_schema("welcome")).validate(case["welcome"])
        c.Welcome.model_validate(case["welcome"])


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


NIL = "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(("kind", "ok"), [("challenge", True), ("enrol", True), ("hello", False)])
def test_nil_node_id_only_for_challenge_and_enrol(kind: str, ok: bool) -> None:
    envelope_schema = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
    example = json.loads((CONTRACTS / "examples" / "node-envelope.valid.json").read_text())
    env = {**example, "node_id": NIL, "kind": kind}
    assert validator(envelope_schema).is_valid(env) is ok
    if ok:
        c.NodeEnvelope.model_validate(env)
    else:
        with pytest.raises(ValidationError):
            c.NodeEnvelope.model_validate(env)


def test_envelope_describes_per_sender_seq() -> None:
    envelope_schema = json.loads((CONTRACTS / "node-envelope.schema.json").read_text())
    text = envelope_schema["properties"]["seq"]["description"].lower()
    assert "per sender" in text and "exactly 1" in text


@pytest.mark.parametrize("last_seq", [-5, 0, 1, 10**9])
def test_hello_resume_last_seq_is_any_integer(last_seq: int) -> None:
    payload = {
        **_example("hello", "valid"),
        "resume": [{"reply_id": SPEECH["reply_id"], "last_seq": last_seq}],
    }
    validator(_schema("hello")).validate(payload)
    c.Hello.model_validate(payload)


@pytest.mark.parametrize(("name", "field"), [("hello", "clock_skew_ms"), ("welcome", "enrolment")])
def test_optional_fields_may_be_absent_but_not_null(name: str, field: str) -> None:
    example = _example(name, "valid")
    absent = {k: v for k, v in example.items() if k != field}
    validator(_schema(name)).validate(absent)
    c.MESSAGE_MODELS[name].model_validate(absent)
    nulled = {**example, field: None}
    assert not validator(_schema(name)).is_valid(nulled)
    with pytest.raises(ValidationError):
        c.MESSAGE_MODELS[name].model_validate(nulled)


@pytest.mark.parametrize("name", ["challenge", "enrol", "hello"])
def test_nonce_is_43_char_base64url(name: str) -> None:
    example = _example(name, "valid")
    assert len(example["nonce"]) == 43
    short = {**example, "nonce": example["nonce"][:42]}
    assert not validator(_schema(name)).is_valid(short)
    with pytest.raises(ValidationError):
        c.MESSAGE_MODELS[name].model_validate(short)


# --- the test-folder shapes (R12-25): schema and model agree case by case -----------------------

_FILE = {
    "name": "a.txt",
    "size": 12,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "file_id": "00000000a1b2c3d4:0000000000000000000000000000a001",
}


@pytest.mark.parametrize(
    ("change", "ok"),
    [
        ({}, True),
        ({"name": "Report-2026.final.txt"}, True),
        ({"name": "../a.txt"}, False),
        ({"name": "a:b"}, False),
        ({"name": ".hidden"}, False),
        ({"name": "nul"}, False),
        ({"name": "Com1.log"}, False),
        ({"name": "console.txt"}, True),
        ({"size": -1}, False),
        ({"size": "12"}, False),
        ({"sha256": "9F86" + _FILE["sha256"][4:]}, False),
        ({"file_id": "0x0000000000001a2b"}, False),
    ],
)
def test_folder_file_schema_and_model_agree(change: dict[str, Any], ok: bool) -> None:
    payload = {**_FILE, **change}
    assert validator(_schema("pc.delete_test_file.args")).is_valid(payload) is ok
    if ok:
        c.FolderFile.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            c.FolderFile.model_validate(payload)


def test_delete_receipt_without_a_file_found() -> None:
    payload = {
        "execution_key": "e1d2c3b4-a5f6-4e7d-8c9b-0a1f2e3d4c5b",
        "result": "NOT_FOUND",
        "evidence": {"file_id": None, "before_sha256": None, "recycled_at": None},
    }
    validator(_schema("pc.delete_test_file.receipt")).validate(payload)
    c.DeleteReceipt.model_validate(payload)
    # each also passes the generic `action.receipt` the gateway checks
    validator(_schema("action.receipt")).validate(payload)
    for name in ("pc.list_test_folder.receipt", "pc.delete_test_file.receipt"):
        validator(_schema("action.receipt")).validate(_example(name, "valid"))


@pytest.mark.parametrize(
    ("check", "ok"),
    [("omitted", True), ("folder_reparse_point", True), (None, False), ("Bad", False)],
)
def test_delete_evidence_check_is_omitted_never_null(check: Any, ok: bool) -> None:
    """Fix round 2 (C): model and schema agree that `check` is optional but never null."""
    evidence: dict[str, Any] = {"file_id": None, "before_sha256": None, "recycled_at": None}
    if check != "omitted":
        evidence["check"] = check
    payload = {
        "execution_key": "e1d2c3b4-a5f6-4e7d-8c9b-0a1f2e3d4c5b",
        "result": "REFUSED",
        "evidence": evidence,
    }
    assert validator(_schema("pc.delete_test_file.receipt")).is_valid(payload) is ok
    if ok:
        c.DeleteReceipt.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            c.DeleteReceipt.model_validate(payload)


# --- the Board (slice 1) ---------------------------------------------------------------------


def _both(name: str, payload: Any, *, ok: bool) -> None:
    """The schema and the model agree on `payload`."""
    assert validator(_schema(name)).is_valid(payload) is ok
    if ok:
        MODELS[name].model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            MODELS[name].model_validate(payload)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("name", ["board.show.args", "board.update.args"])
def test_board_panel_limits(name: str) -> None:
    good = _example(name, "valid")
    _both(name, good, ok=True)
    _both(name, {**good, "title": "t" * 120}, ok=True)
    _both(name, {**good, "title": "t" * 121}, ok=False)
    _both(name, {**good, "title": ""}, ok=False)
    long = "c" * 32_000
    _both(name, {**good, "content": long, "content_sha256": _sha(long)}, ok=True)
    longer = "c" * 32_001
    _both(name, {**good, "content": longer, "content_sha256": _sha(longer)}, ok=False)
    _both(name, {**good, "content": "", "content_sha256": _sha("")}, ok=False)
    _both(name, {**good, "revision": 0}, ok=False)
    _both(name, {**good, "cost_paise": -1}, ok=False)
    _both(name, {**good, "extra": 1}, ok=False)


def test_board_show_kinds_are_text_and_canvas() -> None:
    good = _example("board.show.args", "valid")
    _both("board.show.args", {**good, "kind": "canvas"}, ok=True)
    _both("board.show.args", {**good, "kind": "html"}, ok=False)
    assert _schema("board.show.args")["properties"]["kind"]["enum"] == ["text", "canvas"]


@pytest.mark.parametrize("name", ["board.show.args", "board.update.args"])
def test_board_content_is_limited_in_bytes_by_the_model(name: str) -> None:
    """JSON Schema counts characters; the limit is 32,000 bytes of UTF-8, so the model (and the
    node) also count bytes. 16,001 two-byte characters pass the schema and fail the model."""
    good = _example(name, "valid")
    wide = "é" * 16_001
    payload = {**good, "content": wide, "content_sha256": _sha(wide)}
    assert validator(_schema(name)).is_valid(payload)
    with pytest.raises(ValidationError):
        MODELS[name].model_validate(payload)
    fits = "é" * 16_000
    MODELS[name].model_validate({**good, "content": fits, "content_sha256": _sha(fits)})


@pytest.mark.parametrize("name", ["board.show.args", "board.update.args"])
def test_board_hash_must_be_of_the_content(name: str) -> None:
    good = _example(name, "valid")
    assert good["content_sha256"] == _sha(good["content"])
    with pytest.raises(ValidationError):
        MODELS[name].model_validate({**good, "content_sha256": _sha("something else")})


STATES = ["open", "minimised", "maximised", "closed"]


@pytest.mark.parametrize("name", ["board.set_state.args", "board.state"])
def test_board_states_are_the_four(name: str) -> None:
    good = _example(name, "valid")
    assert _schema(name)["properties"]["state"]["enum"] == STATES
    for state in STATES:
        _both(name, {**good, "state": state}, ok=True)
    _both(name, {**good, "state": "hidden"}, ok=False)


def test_board_receipt_results() -> None:
    show = _example("board.show.receipt", "valid")
    for result in ("RENDERED", "REFUSED", "ERROR"):
        _both("board.show.receipt", {**show, "result": result}, ok=True)
    _both("board.show.receipt", {**show, "result": "OK"}, ok=False)
    state = _example("board.set_state.receipt", "valid")
    for result in ("OK", "REFUSED"):
        _both("board.set_state.receipt", {**state, "result": result}, ok=True)
    _both("board.set_state.receipt", {**state, "result": "RENDERED"}, ok=False)


@pytest.mark.parametrize("name", ["board.show.receipt", "board.set_state.receipt"])
def test_board_receipt_check_is_omitted_never_null(name: str) -> None:
    good = _example(name, "valid")
    evidence = {k: v for k, v in good["evidence"].items() if k != "check"}
    _both(name, {**good, "evidence": evidence}, ok=True)
    _both(name, {**good, "evidence": {**evidence, "check": "too_large"}}, ok=True)
    _both(name, {**good, "evidence": {**evidence, "check": None}}, ok=False)


@pytest.mark.parametrize("name", ["board.show.receipt", "board.set_state.receipt"])
def test_board_receipts_are_live_action_receipts(name: str) -> None:
    """A Board receipt travels as `action.receipt` (its live form), so it must pass that too."""
    good = _example(name, "valid")
    validator(_schema("action.receipt")).validate(good)
    c.MESSAGE_MODELS["action.receipt"].model_validate(good)
