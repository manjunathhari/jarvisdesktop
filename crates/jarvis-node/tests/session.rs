//! The connection state machine (spec §6.1, §7.1, §7.2), driven by a scripted fake gateway.

mod support;

use chrono::Duration;
use ed25519_dalek::SigningKey;
use jarvis_node::code::EnrolmentCode;
use jarvis_node::session::{Output, SendError, Session, Start, Stop};
use jarvis_node::sign::{fingerprint, verify_envelope, Purpose};
use jarvis_node::state::NodeState;
use jarvis_node::trust::{PinnedKey, Trust};
use serde_json::{json, Value};
use support::gateway::{code_for, now, Gw, NIL};
use uuid::Uuid;

fn node_key() -> SigningKey {
    SigningKey::from_bytes(&[0x42; 32])
}

fn core_public() -> [u8; 32] {
    support::test_key("core-1").verifying_key().to_bytes()
}

/// The single frame this output list sends, parsed, after checking it's a well-formed,
/// correctly signed node envelope.
fn sent(outputs: &[Output]) -> Value {
    let frames: Vec<&String> = outputs
        .iter()
        .filter_map(|o| match o {
            Output::Send(text) => Some(text),
            _ => None,
        })
        .collect();
    assert_eq!(frames.len(), 1, "{outputs:?}");
    let env: Value = serde_json::from_str(frames[0]).unwrap();
    support::assert_envelope_conforms(&env);
    let public = node_key().verifying_key().to_bytes();
    assert!(
        verify_envelope(&public, Purpose::NodeMsg, &fingerprint(&public), &env),
        "signed node-msg under the node's own fingerprint (R12-6)"
    );
    env
}

fn enrolling() -> (Gw, Session) {
    let gw = Gw::new("gw-1", "gw-1");
    let code = EnrolmentCode::parse(&code_for(&core_public(), &gw.public())).unwrap();
    let session = Session::new(node_key(), NodeState::default(), Start::Enrol(code));
    (gw, session)
}

fn enrolled_state(gw: &Gw, node_id: Uuid) -> NodeState {
    let mut trust = Trust::default();
    trust
        .core
        .insert("core-1".into(), PinnedKey::new(core_public()));
    trust
        .gateway
        .insert(gw.key_id.clone(), PinnedKey::new(gw.public()));
    NodeState {
        node_id: Some(node_id),
        trust,
    }
}

/// An enrolled node's session, through `welcome`.
fn ready(gw: &mut Gw, node_id: Uuid) -> Session {
    let mut s = Session::new(
        node_key(),
        enrolled_state(gw, node_id),
        Start::Hello { resume: vec![] },
    );
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    let out = s
        .on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap();
    assert!(matches!(
        out[..],
        [Output::Ready {
            enrolled: false,
            ..
        }]
    ));
    s
}

#[test]
fn enrol_pins_keys_from_code() {
    let (mut gw, mut s) = enrolling();
    let out = s.on_frame(&gw.challenge(now()), now()).unwrap();
    let env = sent(&out);
    assert_eq!(env["kind"], "enrol");
    assert_eq!(env["seq"], 1);
    assert_eq!(env["node_id"], NIL.to_string());
    support::assert_conforms("enrol", &env["payload"]);
    assert_eq!(env["payload"]["nonce"], gw.nonce);
    assert_eq!(env["payload"]["token"], "a".repeat(26));
    assert!(
        s.state().node_id.is_none(),
        "nothing is pinned before the welcome"
    );

    let node_id = Uuid::new_v4();
    let out = s
        .on_frame(
            &gw.welcome(node_id, Some(("core-1", core_public())), now()),
            now(),
        )
        .unwrap();
    assert!(matches!(out[..], [Output::Ready { enrolled: true, .. }]));
    assert!(s.is_ready());
    assert_eq!(s.state().node_id, Some(node_id));
    assert_eq!(s.state().trust.core["core-1"].public_key, core_public());
    assert_eq!(s.state().trust.gateway["gw-1"].public_key, gw.public());
    assert!(
        !s.state().trust.core.contains_key("gw-1"),
        "the gateway key is never a core key"
    );
}

#[test]
fn enrol_core_mismatch_discards_everything() {
    let (mut gw, mut s) = enrolling();
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    // The gateway offers its own key as the core key: it doesn't match the code.
    let welcome = gw.welcome(Uuid::new_v4(), Some(("core-1", gw.public())), now());
    let stop = s.on_frame(&welcome, now()).unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "core_fingerprint_mismatch",
            reenrol: false
        }
    );
    assert_eq!(s.state(), &NodeState::default(), "nothing pinned or stored");
    assert!(!s.is_ready());
}

#[test]
fn enrol_needs_the_enrolment_block_and_its_node_id() {
    let (mut gw, mut s) = enrolling();
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    let stop = s
        .on_frame(&gw.welcome(Uuid::new_v4(), None, now()), now())
        .unwrap_err();
    assert_eq!(stop.reason, "enrolment_missing");

    let (mut gw, mut s) = enrolling();
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    let mut payload = json!({"session_id": null, "summary": []});
    payload["enrolment"] = json!({
        "node_id": Uuid::new_v4(),
        "core_key_id": "core-1",
        "core_public_key": jarvis_node::sign::b64url(&core_public()),
    });
    let welcome = gw.frame("welcome", Uuid::new_v4(), payload, now()); // a different node_id
    assert_eq!(
        s.on_frame(&welcome, now()).unwrap_err().reason,
        "node_id_mismatch"
    );
}

#[test]
fn enrol_refuses_a_gateway_not_in_the_code() {
    let (_, mut s) = enrolling();
    let mut impostor = Gw::new("core-2", "gw-1");
    let stop = s.on_frame(&impostor.challenge(now()), now()).unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "fingerprint_mismatch",
            reenrol: false
        }
    );
}

#[test]
fn hello_uses_pinned_gateway_key() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let resume = vec![json!({"reply_id": Uuid::new_v4(), "last_seq": 3})];
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello {
            resume: resume.clone(),
        },
    );
    let env = sent(&s.on_frame(&gw.challenge(now()), now()).unwrap());
    assert_eq!(env["kind"], "hello");
    assert_eq!(env["seq"], 1);
    assert_eq!(env["node_id"], node_id.to_string());
    support::assert_conforms("hello", &env["payload"]);
    assert_eq!(env["payload"]["nonce"], gw.nonce);
    assert_eq!(env["payload"]["resume"], json!(resume));
    let out = s
        .on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap();
    assert!(matches!(
        out[..],
        [Output::Ready {
            enrolled: false,
            ..
        }]
    ));

    // A gateway key this node never pinned: re-enrol, send nothing.
    let mut other = Gw::new("core-2", "gw-9");
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello { resume: vec![] },
    );
    let stop = s.on_frame(&other.challenge(now()), now()).unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "unpinned_key",
            reenrol: true
        }
    );
}

#[test]
fn hello_with_no_trusted_core_key_asks_reenrol() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut state = enrolled_state(&gw, node_id);
    state.trust.core.clear();
    let mut s = Session::new(node_key(), state, Start::Hello { resume: vec![] });
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    let stop = s
        .on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "no_core_key",
            reenrol: true
        }
    );
}

#[test]
fn a_welcome_enrolment_block_on_hello_pins_nothing() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello { resume: vec![] },
    );
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    let rogue = support::test_key("unknown-1").verifying_key().to_bytes();
    s.on_frame(&gw.welcome(node_id, Some(("core-9", rogue)), now()), now())
        .unwrap();
    assert!(!s.state().trust.core.contains_key("core-9"));
}

/// Builds a first frame for a fresh fake gateway.
type FirstFrame = Box<dyn Fn(&mut Gw) -> String>;

#[test]
fn anything_but_a_valid_challenge_first_closes_silently() {
    let node_id = Uuid::new_v4();
    let cases: Vec<FirstFrame> = vec![
        Box::new(|_| "not json".to_owned()),
        Box::new(move |gw| gw.frame("ping", node_id, json!({}), now())),
        Box::new(|gw| {
            let mut c: Value = serde_json::from_str(&gw.challenge(now())).unwrap();
            c["payload"]["nonce"] = json!("short");
            c.to_string()
        }),
        Box::new(|gw| {
            // A valid challenge with seq 2 (not the first message).
            gw.seq = 1;
            gw.challenge(now())
        }),
        Box::new(|gw| {
            let mut c: Value = serde_json::from_str(&gw.challenge(now())).unwrap();
            c["sig"] = json!("A".repeat(86));
            c.to_string()
        }),
    ];
    for (n, make) in cases.iter().enumerate() {
        let mut gw = Gw::new("gw-1", "gw-1");
        let mut s = Session::new(
            node_key(),
            enrolled_state(&gw, node_id),
            Start::Hello { resume: vec![] },
        );
        let stop = s.on_frame(&make(&mut gw), now()).unwrap_err();
        assert!(!stop.reenrol, "case {n}: {stop:?}");
        assert!(!s.is_ready());
        // Stopped: it never sends anything afterwards.
        assert!(s.on_frame(&gw.challenge(now()), now()).is_err(), "case {n}");
        assert_eq!(s.send("ping", json!({}), now()), Err(SendError::NotReady));
    }
}

#[test]
fn bad_signature_under_trusted_key_reconnects() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let mut frame: Value = serde_json::from_str(&gw.frame(
        "outbox.cleared",
        node_id,
        json!({"outbox_id": Uuid::new_v4(), "reason": "EXPIRED"}),
        now(),
    ))
    .unwrap();
    frame["payload"]["reason"] = json!("ACKNOWLEDGED"); // changed after signing
    let stop = s.on_frame(&frame.to_string(), now()).unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "bad_signature",
            reenrol: false
        }
    );
}

#[test]
fn untrusted_gateway_key_asks_reenrol() {
    // The connection's gateway key is in a rotation overlap that ends mid-connection.
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut state = enrolled_state(&gw, node_id);
    let until = now() + Duration::minutes(1);
    state.trust.gateway.get_mut("gw-1").unwrap().expires_at = Some(until);
    let mut s = Session::new(node_key(), state, Start::Hello { resume: vec![] });
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    s.on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap();
    let later = until + Duration::seconds(1);
    let stop = s
        .on_frame(&gw.frame("ping", node_id, json!({}), later), later)
        .unwrap_err();
    assert_eq!(
        stop,
        Stop {
            reason: "unpinned_key",
            reenrol: true
        }
    );
}

#[test]
fn outgoing_seq_starts_at_1_and_increments() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello { resume: vec![] },
    );
    let hello = sent(&s.on_frame(&gw.challenge(now()), now()).unwrap());
    assert_eq!(hello["seq"], 1);
    s.on_frame(&gw.welcome(node_id, None, now()), now())
        .unwrap();
    for want in 2..5 {
        let text = s
            .send(
                "utterance",
                json!({"text": "fictional test phrase", "screen_summary": null}),
                now(),
            )
            .unwrap();
        let env: Value = serde_json::from_str(&text).unwrap();
        support::assert_envelope_conforms(&env);
        assert_eq!(env["seq"], want);
        assert_eq!(env["node_id"], node_id.to_string());
    }
    // An unsignable payload doesn't use up a seq.
    assert_eq!(
        s.send("utterance", json!({"n": 9_007_199_254_740_992u64}), now()),
        Err(SendError::Unsignable)
    );
    let env: Value = serde_json::from_str(&s.send("ping", json!({}), now()).unwrap()).unwrap();
    assert_eq!(env["seq"], 5);
}

#[test]
fn peer_seq_gap_repeat_or_decrease_stops() {
    let node_id = Uuid::new_v4();
    // challenge = 1, welcome = 2; next must be 3.
    for bad in [4, 2, 1] {
        let mut gw = Gw::new("gw-1", "gw-1");
        let mut s = ready(&mut gw, node_id);
        let frame = gw.frame_with_seq("ping", node_id, json!({}), now(), bad);
        assert_eq!(
            s.on_frame(&frame, now()).unwrap_err(),
            Stop {
                reason: "bad_seq",
                reenrol: false
            },
            "seq {bad}"
        );
    }
}

#[test]
fn ping_gets_pong() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let env = sent(
        &s.on_frame(&gw.frame("ping", node_id, json!({}), now()), now())
            .unwrap(),
    );
    assert_eq!(env["kind"], "pong");
    support::assert_conforms("pong", &env["payload"]);
    // A pong from the gateway needs no answer.
    assert!(s
        .on_frame(&gw.frame("pong", node_id, json!({}), now()), now())
        .unwrap()
        .is_empty());
}

#[test]
fn clock_off_reported_over_30s() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello { resume: vec![] },
    );
    let gateway_time = now() - Duration::seconds(45); // this device is 45 s ahead
    let out = s.on_frame(&gw.challenge(gateway_time), now()).unwrap();
    let skew = out
        .iter()
        .find_map(|o| match o {
            Output::ClockOff(ms) => Some(*ms),
            _ => None,
        })
        .expect("ClockOff");
    assert!((44_000..=46_000).contains(&skew), "{skew}");
    assert!((44_000..=46_000).contains(&sent(&out)["payload"]["clock_skew_ms"].as_i64().unwrap()));

    let mut gw = Gw::new("gw-1", "gw-1");
    let mut s = Session::new(
        node_key(),
        enrolled_state(&gw, node_id),
        Start::Hello { resume: vec![] },
    );
    let out = s
        .on_frame(&gw.challenge(now() - Duration::seconds(10)), now())
        .unwrap();
    assert!(
        !out.iter().any(|o| matches!(o, Output::ClockOff(_))),
        "10 s is within tolerance"
    );
}

#[test]
fn gateway_messages_pass_through_and_others_stop() {
    let mut gw = Gw::new("gw-1", "gw-1");
    let node_id = Uuid::new_v4();
    let mut s = ready(&mut gw, node_id);
    let payload = json!({"outbox_id": Uuid::new_v4(), "reason": "SUPERSEDED"});
    let out = s
        .on_frame(
            &gw.frame("outbox.cleared", node_id, payload.clone(), now()),
            now(),
        )
        .unwrap();
    assert_eq!(
        out,
        vec![Output::Frame {
            kind: "outbox.cleared".into(),
            payload
        }]
    );
    // A message for another node.
    let stop = s
        .on_frame(&gw.frame("ping", Uuid::new_v4(), json!({}), now()), now())
        .unwrap_err();
    assert_eq!(stop.reason, "wrong_node");

    // Node-only kinds, or a second welcome, never come from the gateway.
    for kind in ["hello", "utterance", "ack", "welcome", "challenge"] {
        let mut gw = Gw::new("gw-1", "gw-1");
        let mut s = ready(&mut gw, node_id);
        let stop = s
            .on_frame(&gw.frame(kind, node_id, json!({}), now()), now())
            .unwrap_err();
        assert_eq!(stop.reason, "unexpected_kind", "{kind}");
    }
}

#[test]
fn send_before_ready_is_refused() {
    let (mut gw, mut s) = enrolling();
    assert_eq!(s.send("ping", json!({}), now()), Err(SendError::NotReady));
    s.on_frame(&gw.challenge(now()), now()).unwrap();
    assert_eq!(s.send("ping", json!({}), now()), Err(SendError::NotReady));
}
