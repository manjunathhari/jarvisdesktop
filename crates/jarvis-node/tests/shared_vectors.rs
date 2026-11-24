//! P2-S4 (Rust half, part 1): the committed JCS + Ed25519 signing vectors.

mod support;

use jarvis_node::sign::{
    b64url, b64url_decode, canonical, fingerprint, sign, sign_envelope, signed_bytes, verify,
    verify_envelope, Purpose,
};
use serde_json::{json, Value};

fn purpose(v: &Value) -> Purpose {
    match v["purpose"].as_str().unwrap() {
        "node-msg" => Purpose::NodeMsg,
        "gw-transport" => Purpose::GwTransport,
        "core-authz" => Purpose::CoreAuthz,
        other => panic!("unknown purpose {other}"),
    }
}

#[test]
fn every_signing_vector_matches() {
    let vectors = support::vectors();
    assert!(vectors.len() >= 11, "the committed vectors are missing");
    for v in &vectors {
        let name = v["name"].as_str().unwrap();
        let key_id = v["key_id"].as_str().unwrap();
        let obj = &v["object"];
        let canon = canonical(obj).unwrap();
        assert_eq!(
            String::from_utf8(canon).unwrap(),
            v["expected_canonical_utf8"].as_str().unwrap(),
            "{name}: canonical form"
        );
        let bytes = signed_bytes(purpose(v), key_id, obj).unwrap();
        assert_eq!(
            hex::encode(&bytes),
            v["expected_signed_bytes_hex"].as_str().unwrap(),
            "{name}: signed bytes"
        );
        let seed: [u8; 32] = hex::decode(v["seed_hex"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let key = ed25519_dalek::SigningKey::from_bytes(&seed);
        let sig = sign(&key, purpose(v), key_id, obj).unwrap();
        assert_eq!(
            sig,
            v["expected_sig"].as_str().unwrap(),
            "{name}: signature"
        );
        let public = key.verifying_key().to_bytes();
        assert!(
            verify(&public, purpose(v), key_id, obj, &sig),
            "{name}: verify"
        );
    }
}

#[test]
fn verify_rejects_other_purpose_and_key_id() {
    let vectors = support::vectors();
    let by = |n: &str| vectors.iter().find(|v| v["name"] == n).unwrap().clone();
    let v = by("same-object-two-purposes:node-msg");
    let key = support::test_key(v["key_id"].as_str().unwrap());
    let public = key.verifying_key().to_bytes();
    let (key_id, obj, sig) = (
        v["key_id"].as_str().unwrap(),
        &v["object"],
        v["expected_sig"].as_str().unwrap(),
    );
    assert!(verify(&public, Purpose::NodeMsg, key_id, obj, sig));
    assert!(!verify(&public, Purpose::CoreAuthz, key_id, obj, sig));
    assert!(!verify(&public, Purpose::GwTransport, key_id, obj, sig));
    assert!(!verify(
        &public,
        Purpose::NodeMsg,
        "another-key-id",
        obj,
        sig
    ));
    // The two-key-id pair: the same object and key under two key_ids, never interchangeable.
    let one = by("same-object-two-key-ids:core-1");
    let two = by("same-object-two-key-ids:core-2");
    assert_ne!(one["expected_sig"], two["expected_sig"]);
    let k1 = support::test_key(one["key_id"].as_str().unwrap());
    assert!(!verify(
        &k1.verifying_key().to_bytes(),
        purpose(&two),
        two["key_id"].as_str().unwrap(),
        &two["object"],
        one["expected_sig"].as_str().unwrap()
    ));
    // A changed object or a mangled signature never verifies.
    let mut changed = obj.clone();
    changed["extra"] = json!(1);
    assert!(!verify(&public, Purpose::NodeMsg, key_id, &changed, sig));
    assert!(!verify(
        &public,
        Purpose::NodeMsg,
        key_id,
        obj,
        "not-a-signature"
    ));
    assert!(!verify(&[0u8; 32], Purpose::NodeMsg, key_id, obj, sig));
}

#[test]
fn signing_refuses_a_bad_key_id() {
    let key = support::test_key("core-1");
    assert!(signed_bytes(Purpose::CoreAuthz, "", &json!({})).is_err());
    assert!(sign(&key, Purpose::CoreAuthz, "a\nb", &json!({})).is_err());
    assert!(!verify(
        &key.verifying_key().to_bytes(),
        Purpose::CoreAuthz,
        "",
        &json!({}),
        &"A".repeat(86)
    ));
}

#[test]
fn envelope_signature_covers_every_field_but_sig() {
    let v = support::vectors()
        .into_iter()
        .find(|v| v["name"] == "envelope-utterance")
        .unwrap();
    let key = support::test_key(v["key_id"].as_str().unwrap());
    let key_id = v["key_id"].as_str().unwrap();
    let mut env = v["object"].clone();
    sign_envelope(&key, purpose(&v), key_id, &mut env).unwrap();
    assert_eq!(env["sig"], v["expected_sig"]);
    let public = key.verifying_key().to_bytes();
    assert!(verify_envelope(&public, purpose(&v), key_id, &env));
    // Re-signing an envelope that already has a sig gives the same sig.
    let mut again = env.clone();
    sign_envelope(&key, purpose(&v), key_id, &mut again).unwrap();
    assert_eq!(again, env);
    let mut tampered = env.clone();
    tampered["seq"] = json!(99);
    assert!(!verify_envelope(&public, purpose(&v), key_id, &tampered));
    let mut unsigned = env.clone();
    unsigned.as_object_mut().unwrap().remove("sig");
    assert!(!verify_envelope(&public, purpose(&v), key_id, &unsigned));
    assert!(sign_envelope(&key, purpose(&v), key_id, &mut json!([1])).is_err());
}

#[test]
fn fingerprint_matches_shared_case() {
    let case = support::case("accept-challenge-enrol-by-fingerprint");
    let public = b64url_decode(
        case["envelope"]["payload"]["public_key"].as_str().unwrap(),
        32,
    )
    .unwrap();
    let public: [u8; 32] = public.try_into().unwrap();
    let fp = fingerprint(&public);
    assert_eq!(fp, case["enrol_fingerprint"].as_str().unwrap());
    assert_eq!(fp.len(), 16);
}

#[test]
fn b64url_is_strict() {
    let raw = [0xfbu8; 32];
    let good = b64url(&raw);
    assert_eq!(good.len(), 43);
    assert_eq!(b64url_decode(&good, 32).unwrap(), raw);
    assert!(b64url_decode(&format!("{good}="), 32).is_none(), "padding");
    assert!(
        b64url_decode(&good.replace('-', "+").replace('_', "/"), 32).is_none()
            || !good.contains(['-', '_'])
    );
    assert!(b64url_decode("+/+/", 3).is_none(), "standard alphabet");
    assert!(b64url_decode(&good, 31).is_none(), "wrong length");
    assert!(b64url_decode("", 32).is_none());
    assert!(b64url_decode("AB==", 1).is_none(), "padded");
    // "AB" decodes 1 byte with 4 unused trailing bits set: not canonical ("AA" is).
    assert!(b64url_decode("AB", 1).is_none(), "non-zero trailing bits");
    assert_eq!(b64url_decode("AA", 1).unwrap(), vec![0]);
    assert!(b64url_decode("A A", 2).is_none(), "whitespace");
}
