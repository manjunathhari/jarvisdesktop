"""JCS + Ed25519 signing (spec §7.1), checked against the independently generated vectors.

The expected values in contracts/signing-vectors/ come from a separate Node.js generator; this
suite only reads them and never regenerates them.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_server.signing import (
    AuthzInvalid,
    Purpose,
    b64url_decode,
    canonical,
    check_authz,
    fingerprint,
    load_private_key,
    make_authz,
    new_nonce,
    public_bytes,
    sign,
    sign_envelope,
    signed_bytes,
    verify,
    verify_envelope,
)

VECTORS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "signing-vectors"
VECTORS: list[dict[str, Any]] = json.loads((VECTORS_DIR / "vectors.json").read_text())
CASE_IDS = [v["name"] for v in VECTORS]

# Fixed test seed (not a secret): only for the unit tests below.
key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
pub = public_bytes(key)
other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
obj = {"kind": "ping", "text": "ನಮಸ್ಕಾರ 🙏", "amount_paise": 500000}

base_env: dict[str, Any] = {
    "version": 1,
    "message_id": "0b8f7c1e-3a8e-4d4f-9a55-2f6f1c3d9e01",
    "node_id": "7a1d3c5e-0f2b-4c6d-8e9f-a0b1c2d3e4f5",
    "session_id": None,
    "seq": 1,
    "sent_at": "2026-11-22T10:00:00Z",
    "kind": "utterance",
    "payload": {"text": "नमस्ते", "screen_summary": None},
}


def _key_of(case: dict[str, Any]) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(case["seed_hex"]))


def altered(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, dict):
        return {**value, "text": "altered"}
    raise AssertionError(f"no alteration for {value!r}")


# ---------------------------------------------------------------- golden vectors


@pytest.mark.parametrize("case", VECTORS, ids=CASE_IDS)
def test_vectors_match(case: dict[str, Any]) -> None:
    key = _key_of(case)
    assert canonical(case["object"]).decode() == case["expected_canonical_utf8"]
    assert (
        sign(Purpose(case["purpose"]), key, case["key_id"], case["object"]) == case["expected_sig"]
    )


@pytest.mark.parametrize("case", VECTORS, ids=CASE_IDS)
def test_signed_bytes_match_vectors(case: dict[str, Any]) -> None:
    got = signed_bytes(Purpose(case["purpose"]), case["key_id"], case["object"])
    assert got.hex() == case["expected_signed_bytes_hex"]


@pytest.mark.parametrize("case", VECTORS, ids=CASE_IDS)
def test_verify_accepts_every_vector(case: dict[str, Any]) -> None:
    pub = public_bytes(_key_of(case))
    assert verify(
        Purpose(case["purpose"]), pub, case["key_id"], case["object"], case["expected_sig"]
    )


def test_vector_set_has_the_required_cases() -> None:
    names = set(CASE_IDS)
    for required in (
        "rfc8785-appendix",
        "ascii-simple",
        "nested-keys-unsorted",
        "kannada-and-emoji",
        "hindi",
        "integers-only",
        "rfc8785-sorting",
        "key-order-utf16",
    ):
        assert required in names
    by_name = {v["name"]: v for v in VECTORS}
    two_purposes = (
        by_name["same-object-two-purposes:node-msg"],
        by_name["same-object-two-purposes:core-authz"],
    )
    two_key_ids = (
        by_name["same-object-two-key-ids:core-1"],
        by_name["same-object-two-key-ids:core-2"],
    )
    for a, b in (two_purposes, two_key_ids):
        assert a["object"] == b["object"] and a["seed_hex"] == b["seed_hex"]
        assert a["expected_sig"] != b["expected_sig"]


def test_rfc8785_case_is_the_published_output() -> None:
    # RFC 8785 §3.2.4: the UTF-8 bytes of the §3.2.3 canonical output.
    rfc_hex = (
        "7b226c69746572616c73223a5b6e756c6c2c747275652c66616c73655d2c226e756d62657273223a"
        "5b3333333333333333332e333333333333332c31652b33302c342e352c302e3030322c31652d3237"
        "5d2c22737472696e67223a22e282ac245c75303030665c6e4127425c225c5c5c5c5c222f227d"
    )
    case = next(v for v in VECTORS if v["name"] == "rfc8785-appendix")
    assert case["expected_canonical_utf8"].encode().hex() == rfc_hex
    assert canonical(case["object"]).hex() == rfc_hex


# ---------------------------------------------------------------- binding and tampering


def test_rfc8785_sorting_case_is_the_published_order() -> None:
    """RFC 8785 §3.2.3: keys sort by UTF-16 code units; the RFC lists the values in order."""
    case = next(v for v in VECTORS if v["name"] == "rfc8785-sorting")
    text = case["expected_canonical_utf8"]
    order = [
        "Carriage Return",
        "One",
        "Control",
        "Latin Small Letter O With Diaeresis",
        "Euro Sign",
        "Emoji: Grinning Face",
        "Hebrew Letter Dalet With Dagesh",
    ]
    at = [text.index(f'"{value}"') for value in order]
    assert at == sorted(at)
    assert canonical(case["object"]).decode() == text


def test_signed_bytes_layout() -> None:
    got = signed_bytes(Purpose.NODE_MSG, "n-1", {"b": 1, "a": "x"})
    assert got == b'jarvis-sig-v1\nnode-msg\nn-1\n{"a":"x","b":1}'


def test_signature_is_base64url_without_padding() -> None:
    sig = sign(Purpose.NODE_MSG, key, "n-1", obj)
    assert len(sig) == 86 and "=" not in sig and "+" not in sig and "/" not in sig
    assert len(base64.urlsafe_b64decode(sig + "==")) == 64


def test_purpose_is_bound() -> None:
    sig = sign(Purpose.GW_TRANSPORT, key, "gw-1", obj)
    assert verify(Purpose.GW_TRANSPORT, pub, "gw-1", obj, sig)
    assert not verify(Purpose.CORE_AUTHZ, pub, "gw-1", obj, sig)


def test_key_id_is_bound() -> None:
    sig = sign(Purpose.CORE_AUTHZ, key, "core-1", obj)
    assert not verify(Purpose.CORE_AUTHZ, pub, "core-2", obj, sig)


def test_verify_rejects_another_key_and_a_changed_object() -> None:
    sig = sign(Purpose.CORE_AUTHZ, key, "core-1", obj)
    assert not verify(Purpose.CORE_AUTHZ, public_bytes(other_key), "core-1", obj, sig)
    assert not verify(Purpose.CORE_AUTHZ, pub, "core-1", {**obj, "amount_paise": 1}, sig)


@pytest.mark.parametrize(
    "bad_sig",
    ["", "not base64!", "A" * 86, "A" * 85, "A" * 88, "A" * 86 + "==", 42, None],
)
def test_verify_never_raises_on_a_bad_signature(bad_sig: Any) -> None:
    assert verify(Purpose.NODE_MSG, pub, "n-1", obj, bad_sig) is False


@pytest.mark.parametrize("beyond", [2**53, -(2**53), 2**64, 2**70])
def test_integers_beyond_the_jcs_range_never_sign_or_verify(beyond: int) -> None:
    """Owner final-review area 1 (no widening): JCS (RFC 8785) limits signed integers to
    ±(2^53 - 1). One more can't be signed, and verifying an object that carries it returns
    False without raising, so a `hello` with a larger `last_seq` can never verify."""
    edge = 2**53 - 1
    for ok in (edge, -edge):
        assert verify(Purpose.NODE_MSG, pub, "n-1", {"last_seq": ok},
                      sign(Purpose.NODE_MSG, key, "n-1", {"last_seq": ok}))  # fmt: skip
    with pytest.raises(ValueError, match="safe integer"):
        sign(Purpose.NODE_MSG, key, "n-1", {"last_seq": beyond})
    sig = sign(Purpose.NODE_MSG, key, "n-1", {"last_seq": edge})
    assert verify(Purpose.NODE_MSG, pub, "n-1", {"last_seq": beyond}, sig) is False


def test_verify_rejects_padded_or_non_canonical_encodings() -> None:
    sig = sign(Purpose.NODE_MSG, key, "n-1", obj)
    assert not verify(Purpose.NODE_MSG, pub, "n-1", obj, sig + "==")
    raw = base64.urlsafe_b64decode(sig + "==")
    as_std = base64.b64encode(raw).decode().rstrip("=")
    if as_std != sig:  # only differs when the signature contains '-' or '_'
        assert not verify(Purpose.NODE_MSG, pub, "n-1", obj, as_std)


def test_verify_returns_false_for_a_malformed_public_key() -> None:
    sig = sign(Purpose.NODE_MSG, key, "n-1", obj)
    assert verify(Purpose.NODE_MSG, b"short", "n-1", obj, sig) is False


@pytest.mark.parametrize("key_id", ["", "a\nb"])
def test_key_id_may_not_be_empty_or_contain_a_newline(key_id: str) -> None:
    with pytest.raises(ValueError):
        signed_bytes(Purpose.NODE_MSG, key_id, obj)


# ---------------------------------------------------------------- envelopes


def test_envelope_sig_covers_every_field() -> None:
    env = sign_envelope(Purpose.NODE_MSG, key, "n-1", base_env)
    assert verify_envelope(Purpose.NODE_MSG, pub, "n-1", env)
    for field in ("message_id", "node_id", "seq", "kind", "payload", "sent_at"):
        tampered = {**env, field: altered(env[field])}
        assert not verify_envelope(Purpose.NODE_MSG, pub, "n-1", tampered)
    assert not verify_envelope(
        Purpose.NODE_MSG, pub, "n-1", {**env, "session_id": base_env["message_id"]}
    )
    added = {**env, "extra": 1}
    assert not verify_envelope(Purpose.NODE_MSG, pub, "n-1", added)


def test_sign_envelope_signs_everything_but_sig() -> None:
    env = sign_envelope(Purpose.GW_TRANSPORT, key, "gw-1", base_env)
    assert set(env) == {*base_env, "sig"}
    assert env["sig"] == sign(Purpose.GW_TRANSPORT, key, "gw-1", base_env)
    assert "sig" not in base_env  # the input is not changed
    resigned = sign_envelope(Purpose.GW_TRANSPORT, key, "gw-1", {**env, "sig": "old"})
    assert resigned["sig"] == env["sig"]


def test_envelope_purpose_is_bound() -> None:
    env = sign_envelope(Purpose.GW_TRANSPORT, key, "gw-1", base_env)
    assert not verify_envelope(Purpose.NODE_MSG, pub, "gw-1", env)


def test_verify_envelope_without_sig_is_false() -> None:
    assert verify_envelope(Purpose.NODE_MSG, pub, "n-1", base_env) is False


def test_envelope_vector_matches() -> None:
    case = next(v for v in VECTORS if v["name"] == "envelope-utterance")
    env = sign_envelope(Purpose.NODE_MSG, _key_of(case), case["key_id"], case["object"])
    assert env["sig"] == case["expected_sig"]


# ---------------------------------------------------------------- authz


def test_make_and_check_authz_round_trip() -> None:
    authz = make_authz(key, "core-1", obj)
    assert set(authz) == {"object", "key_id", "sig"}
    assert authz["sig"] == sign(Purpose.CORE_AUTHZ, key, "core-1", obj)
    assert check_authz(authz, {"core-1": pub}) == obj


def test_check_authz_rejects_untrusted_key_id() -> None:
    authz = make_authz(key, "core-1", obj)
    with pytest.raises(AuthzInvalid):
        check_authz(authz, {"core-2": pub})


def test_check_authz_rejects_a_bad_signature_or_tampering() -> None:
    authz = make_authz(key, "core-1", obj)
    with pytest.raises(AuthzInvalid):
        check_authz({**authz, "object": {**obj, "amount_paise": 1}}, {"core-1": pub})
    with pytest.raises(AuthzInvalid):
        check_authz(authz, {"core-1": public_bytes(other_key)})
    gw_signed = {
        "object": obj,
        "key_id": "core-1",
        "sig": sign(Purpose.GW_TRANSPORT, key, "core-1", obj),
    }
    with pytest.raises(AuthzInvalid):
        check_authz(gw_signed, {"core-1": pub})


@pytest.mark.parametrize(
    "malformed", [{}, {"object": [], "key_id": "core-1", "sig": "x"}, {"key_id": 1}]
)
def test_check_authz_rejects_malformed_input(malformed: dict[str, Any]) -> None:
    with pytest.raises(AuthzInvalid):
        check_authz(malformed, {"core-1": pub})


# ---------------------------------------------------------------- keys


def test_fingerprint_is_16_chars_and_stable() -> None:
    assert fingerprint(pub) == fingerprint(pub) and len(fingerprint(pub)) == 16
    assert fingerprint(pub) != fingerprint(public_bytes(other_key))


def test_fingerprint_is_lower_base32_of_sha256_prefix() -> None:
    import hashlib

    expected = base64.b32encode(hashlib.sha256(pub).digest()[:10]).decode().lower()
    assert fingerprint(pub) == expected and expected.isalnum()


def test_load_private_key_round_trips_pem(tmp_path: Path) -> None:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "test.pem"
    path.write_bytes(pem)
    loaded = load_private_key(path)
    assert public_bytes(loaded) == pub and len(pub) == 32


def test_load_private_key_refuses_a_non_ed25519_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "ec.pem"
    path.write_bytes(pem)
    with pytest.raises(TypeError):
        load_private_key(path)


def test_new_nonce_is_32_random_bytes_as_43_char_base64url() -> None:
    nonces = {new_nonce() for _ in range(50)}
    assert len(nonces) == 50
    for nonce in nonces:
        assert len(nonce) == 43 and "=" not in nonce
        assert len(base64.urlsafe_b64decode(nonce + "=")) == 32


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _flip_trailing_bit(text: str) -> str:
    return text[:-1] + ALPHABET[ALPHABET.index(text[-1]) | 1]


def test_b64url_decode_is_strict() -> None:
    raw = bytes(range(32))
    text = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert b64url_decode(text, 32) == raw
    assert b64url_decode(_flip_trailing_bit(text), 32) is None  # non-zero trailing bits
    assert b64url_decode(text + "=", 32) is None  # padding
    assert b64url_decode(text[:-1], 32) is None  # wrong length
    assert b64url_decode(text[:-1] + "+", 32) is None  # standard alphabet
    assert b64url_decode(42, 32) is None  # type: ignore[arg-type]


def test_verify_rejects_a_signature_with_non_zero_trailing_bits() -> None:
    sig = sign(Purpose.NODE_MSG, key, "n-1", obj)
    flipped = _flip_trailing_bit(sig)
    assert flipped != sig
    assert not verify(Purpose.NODE_MSG, pub, "n-1", obj, flipped)
