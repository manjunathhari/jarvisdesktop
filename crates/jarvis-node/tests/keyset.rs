//! Key rotation on the node (R6-2, R6-3, R12-26 to R12-29), through the session.

mod support;

use chrono::{DateTime, Duration, FixedOffset};
use ed25519_dalek::SigningKey;
use jarvis_node::rules::{check_node_action, Rejected};
use jarvis_node::session::{Output, Session, Start, Stop};
use jarvis_node::state::NodeState;
use jarvis_node::trust::{PinnedKey, Trust, KEYSET_OVERLAP};
use serde_json::{json, Value};
use support::authz::{keyset_object, keyset_payload, live_action, signed};
use support::gateway::{now, Gw};
use uuid::Uuid;

fn node_key() -> SigningKey {
    SigningKey::from_bytes(&[0x42; 32])
}

fn ready(gw: &mut Gw, node_id: Uuid) -> Session {
    let mut trust = Trust::default();
    let core = support::test_key("core-1").verifying_key().to_bytes();
    trust.core.insert("core-1".into(), PinnedKey::new(core));
    trust
        .gateway
        .insert(gw.key_id.clone(), PinnedKey::new(gw.public()));
    let state = NodeState {
        node_id: Some(node_id),
        node_key_id: None,
        trust,
    };
    let mut s = Session::new(node_key(), state, Start::Hello { resume: vec![] });
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    s.on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap();
    s
}

/// Feed one keyset frame; return the outputs.
fn keyset(
    s: &mut Session,
    gw: &mut Gw,
    node_id: Uuid,
    authz: &Value,
    outbox_id: Uuid,
) -> Result<Vec<Output>, Stop> {
    let frame = gw.frame("keyset", node_id, keyset_payload(outbox_id, authz), now());
    s.on_frame(&frame, now())
}

fn sent(out: &[Output]) -> Vec<Value> {
    out.iter()
        .filter_map(|o| match o {
            Output::Send(t) => Some(serde_json::from_str(t).unwrap()),
            _ => None,
        })
        .collect()
}

fn issued() -> DateTime<FixedOffset> {
    now() - Duration::minutes(1)
}

#[test]
fn core_keyset_adds_key_with_24h_overlap() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let at = issued();
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", Some(node_id), at),
    );
    let outbox_id = Uuid::new_v4();
    let out = keyset(&mut s, &mut gw, node_id, &authz, outbox_id).unwrap();
    // Saved first, then received, then ack (R12-26).
    let Output::TrustChanged(state) = &out[0] else {
        panic!("TrustChanged first: {out:?}")
    };
    assert!(state.trust.core["core-2"].expires_at.is_none());
    let old = state.trust.core["core-1"]
        .expires_at
        .expect("old key in its overlap");
    let signed_at = authz["object"]["issued_at"].as_str().unwrap();
    let signed_at = jarvis_node::wire::parse_ts(signed_at).unwrap();
    assert_eq!(old, signed_at + KEYSET_OVERLAP, "from the signed issued_at");
    assert!((old - (at + KEYSET_OVERLAP)).num_seconds().abs() <= 1);
    let frames = sent(&out);
    let kinds: Vec<&str> = frames.iter().map(|f| f["kind"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["received", "ack"]);
    for f in &frames {
        assert_eq!(f["payload"], json!({"outbox_id": outbox_id}));
        support::assert_conforms(f["kind"].as_str().unwrap(), &f["payload"]);
    }
    assert_eq!(s.state(), state);

    // A re-send changes nothing but is still confirmed.
    let out = keyset(&mut s, &mut gw, node_id, &authz, outbox_id).unwrap();
    assert!(!out.iter().any(|o| matches!(o, Output::TrustChanged(_))));
    assert_eq!(sent(&out).len(), 2);
}

#[test]
fn gateway_keyset_never_enters_core_trust() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("gateway", "gw-2", "gw-2", None, issued()),
    );
    let out = keyset(&mut s, &mut gw, node_id, &authz, Uuid::new_v4()).unwrap();
    let Output::TrustChanged(state) = &out[0] else {
        panic!("{out:?}")
    };
    assert!(state.trust.gateway.contains_key("gw-2"));
    assert!(
        state.trust.gateway["gw-1"].expires_at.is_some(),
        "old gateway key in its overlap"
    );
    assert!(!state.trust.core.contains_key("gw-2"), "never a core key");
    assert!(
        state.trust.core["core-1"].expires_at.is_none(),
        "core keys untouched"
    );
    // So an action signed by that gateway key, even under its key_id, is refused.
    let action = signed("gw-2", "gw-2", live_action(node_id, Uuid::new_v4(), now()));
    let core = state.trust.core_keys(now());
    assert_eq!(
        check_node_action(&action, &core, node_id, now()),
        Err(Rejected("untrusted_key_id"))
    );
}

#[test]
fn keyset_acked_only_after_verify() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let mut authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", None, issued()),
    );
    authz["object"]["valid_until"] = json!("2099-01-01T00:00:00Z"); // changed after signing
    let out = keyset(&mut s, &mut gw, node_id, &authz, Uuid::new_v4()).unwrap();
    assert_eq!(
        out,
        vec![Output::Refused {
            kind: "keyset".into(),
            reason: "bad_signature"
        }]
    );
    assert!(!s.state().trust.core.contains_key("core-2"));

    // The payload's key must match the signed args.
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", None, issued()),
    );
    let mut payload = keyset_payload(Uuid::new_v4(), &authz);
    let another = support::test_key("unknown-1").verifying_key().to_bytes();
    let another = json!(jarvis_node::sign::b64url(&another));
    assert_ne!(another, payload["public_key"]);
    payload["public_key"] = another;
    let frame = gw.frame("keyset", node_id, payload, now());
    let out = s.on_frame(&frame, now()).unwrap();
    assert!(matches!(&out[..], [Output::Refused { .. }]), "{out:?}");
    assert!(sent(&out).is_empty());
}

#[test]
fn keyset_other_target_rejected() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let other = Uuid::new_v4();
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", Some(other), issued()),
    );
    let out = keyset(&mut s, &mut gw, node_id, &authz, Uuid::new_v4()).unwrap();
    assert_eq!(
        out,
        vec![Output::Refused {
            kind: "keyset".into(),
            reason: "wrong_target"
        }]
    );
}

#[test]
fn a_keyset_from_an_untrusted_core_key_asks_reenrol() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let authz = signed(
        "unknown-1",
        "core-9",
        keyset_object("core", "core-2", "core-2", None, issued()),
    );
    let stop = keyset(&mut s, &mut gw, node_id, &authz, Uuid::new_v4()).unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "untrusted_key_id",
            reenrol: true
        }
    );
}

#[test]
fn a_malformed_keyset_message_is_refused() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let authz = signed(
        "core-1",
        "core-1",
        keyset_object("core", "core-2", "core-2", None, issued()),
    );
    let mut payload = keyset_payload(Uuid::new_v4(), &authz);
    payload.as_object_mut().unwrap().remove("outbox_id"); // required (R12-26)
    let out = s
        .on_frame(&gw.frame("keyset", node_id, payload, now()), now())
        .unwrap();
    assert_eq!(
        out,
        vec![Output::Refused {
            kind: "keyset".into(),
            reason: "malformed"
        }]
    );
}
