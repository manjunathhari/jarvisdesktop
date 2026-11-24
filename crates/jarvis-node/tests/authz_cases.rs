//! P2-S4 (Rust half, part 1): the shared node-side authorisation cases.
//!
//! `node_action` and `keyset` here; `challenge`, `enrolment_welcome` and `seq` come with the
//! gateway connection (Task 4).

mod support;

use std::collections::BTreeMap;

use jarvis_node::rules::{check_authz, check_keyset, check_node_action, CoreKeys, Rejected};
use jarvis_node::wire::{parse_authz, parse_ts, AuthzKind};
use serde_json::{json, Value};
use uuid::Uuid;

fn keys(v: &Value) -> CoreKeys {
    let mut out = BTreeMap::new();
    for (key_id, hex_key) in v.as_object().unwrap() {
        let raw: [u8; 32] = hex::decode(hex_key.as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        out.insert(key_id.clone(), raw);
    }
    out
}

fn run(case: &Value) -> Result<Value, Rejected> {
    let trusted = keys(&case["trusted_keys"]);
    let me = Uuid::parse_str(case["receiving_node_id"].as_str().unwrap()).unwrap();
    let now = parse_ts(case["now"].as_str().unwrap()).unwrap();
    match case["check"].as_str().unwrap() {
        "node_action" => check_node_action(&case["authz"], &trusted, me, now).map(|o| o.raw),
        "keyset" => {
            check_keyset(&case["payload"], &case["authz"], &trusted, me, now).map(|o| o.raw)
        }
        other => unreachable!("{other}"),
    }
}

#[test]
fn node_action_and_keyset_cases() {
    let cases: Vec<Value> = support::cases()
        .into_iter()
        .filter(|c| matches!(c["check"].as_str(), Some("node_action" | "keyset")))
        .collect();
    assert!(cases.len() >= 26, "the committed cases are missing");
    for case in &cases {
        let name = case["name"].as_str().unwrap();
        let got = run(case);
        match case["expect"].as_str().unwrap() {
            "accept" => {
                assert!(case["reason"].is_null(), "{name}");
                let obj = got.unwrap_or_else(|r| panic!("{name}: rejected {}", r.0));
                assert_eq!(
                    obj, case["authz"]["object"],
                    "{name}: returns the signed object"
                );
            }
            "reject" => {
                let reason = case["reason"].as_str().unwrap();
                match got {
                    Err(Rejected(r)) => assert_eq!(r, reason, "{name}"),
                    Ok(_) => panic!("{name}: accepted, expected {reason}"),
                }
            }
            other => panic!("{name}: expect {other}"),
        }
    }
}

#[test]
fn an_accepted_gateway_keyset_names_the_gateway_role() {
    // Which pin set it joins is the trust store's job (Task 5); the signed object says gateway.
    let case = support::case("accept-keyset-gateway-role");
    let obj = run(&case).unwrap();
    assert_eq!(obj["args"]["role"], "gateway");
    assert_eq!(case["pins_as"], "gateway");
}

#[test]
fn keyset_rejects_any_payload_field_that_differs() {
    let case = support::case("accept-keyset");
    let other = json!({"role": "gateway", "key_id": "core-3", "public_key": "A".repeat(43)});
    for field in ["role", "key_id", "public_key"] {
        let mut payload = case["payload"].clone();
        payload[field] = other[field].clone();
        let mut c = case.clone();
        c["payload"] = payload;
        assert_eq!(run(&c), Err(Rejected("keyset_mismatch")), "{field}");
    }
}

#[test]
fn keyset_payload_with_extra_fields_is_malformed() {
    // The check gets the payload without its unsigned outbox_id and its authz (R12-26).
    let case = support::case("accept-keyset");
    let mut c = case.clone();
    c["payload"]["outbox_id"] = json!("6f1c2b9e-3d4a-4e5f-9a8b-7c6d5e4f3a2b");
    assert_eq!(run(&c), Err(Rejected("malformed")));
}

#[test]
fn keyset_rejects_an_action_authz() {
    let action = support::case("accept-valid");
    let mut keyset = support::case("accept-keyset");
    keyset["authz"] = action["authz"].clone();
    assert_eq!(run(&keyset), Err(Rejected("wrong_kind")));
}

#[test]
fn check_authz_reasons() {
    let case = support::case("accept-valid");
    let trusted = keys(&case["trusted_keys"]);
    let obj = check_authz(&case["authz"], &trusted).unwrap();
    assert_eq!(obj.kind, AuthzKind::NodeAction);
    assert_eq!(
        check_authz(&case["authz"], &BTreeMap::new()),
        Err(Rejected("untrusted_key_id"))
    );
    assert_eq!(
        check_authz(&json!("x"), &trusted),
        Err(Rejected("malformed"))
    );
    let mut extra = case["authz"].clone();
    extra["extra"] = json!(1);
    assert_eq!(check_authz(&extra, &trusted), Err(Rejected("malformed")));
    let mut missing = case["authz"].clone();
    missing["object"].as_object_mut().unwrap().remove("summary");
    assert_eq!(check_authz(&missing, &trusted), Err(Rejected("malformed")));
}

#[test]
fn authz_examples_match_the_contract() {
    let valid = support::example("authz.valid.json");
    support::assert_conforms("authz", &valid);
    assert!(parse_authz(&valid).is_some());
    let invalid = support::example("authz.invalid.json");
    assert!(!support::conforms("authz", &invalid));
    assert!(parse_authz(&invalid).is_none());
}

#[test]
fn every_case_input_with_a_valid_authz_conforms() {
    // The parser and the schema agree on which shared authz objects are well formed.
    for case in support::cases() {
        let Some(authz) = case.get("authz") else {
            continue;
        };
        let name = case["name"].as_str().unwrap();
        assert_eq!(
            parse_authz(authz).is_some(),
            support::conforms("authz", authz),
            "{name}: parser and schema disagree"
        );
    }
}

#[test]
fn times_need_an_explicit_offset() {
    assert!(parse_ts("2026-11-22T10:02:00+05:30").is_some());
    assert!(parse_ts("2026-11-22T10:02:00Z").is_some());
    assert!(parse_ts("2026-11-22T10:02:00").is_none());
    assert!(parse_ts("2026-11-22").is_none());
    assert!(parse_ts("not a time").is_none());
    // A leap second is refused, as the server's models refuse it.
    assert!(parse_ts("2026-12-31T23:59:60Z").is_none());
    assert!(parse_ts("2026-12-31T23:59:59.999999999Z").is_some());
}
