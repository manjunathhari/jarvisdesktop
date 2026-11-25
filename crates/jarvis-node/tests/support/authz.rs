//! Core-signed authz objects for tests, signed with the shared test keys.
#![allow(dead_code)]

use chrono::{DateTime, Duration, FixedOffset, SecondsFormat};
use jarvis_node::sign::{b64url, sign, Purpose};
use serde_json::{json, Value};
use uuid::Uuid;

fn ts(t: DateTime<FixedOffset>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// `{object, key_id, sig}`, signed `core-authz` by the test key `label` under `key_id`.
pub fn signed(label: &str, key_id: &str, object: Value) -> Value {
    let sig = sign(&super::test_key(label), Purpose::CoreAuthz, key_id, &object).unwrap();
    json!({"object": object, "key_id": key_id, "sig": sig})
}

/// A live read-only action for `target`, valid for 2 minutes from `now`.
pub fn live_action(target: Uuid, reply_id: Uuid, now: DateTime<FixedOffset>) -> Value {
    json!({
        "v": 1, "kind": "node.action", "action_kind": "module.news",
        "args": {"tool": "show_news", "input": {}},
        "target_node_id": target, "person_id": Uuid::new_v4(),
        "approval_id": null, "execution_key": null, "reply_id": reply_id, "summary": null,
        "issued_at": ts(now), "valid_until": ts(now + Duration::minutes(2)),
    })
}

/// A keyset object announcing (`role`, `key_id`, test key `label`) for `target`.
pub fn keyset_object(
    role: &str,
    key_id: &str,
    label: &str,
    target: Option<Uuid>,
    issued_at: DateTime<FixedOffset>,
) -> Value {
    let public = b64url(&super::test_key(label).verifying_key().to_bytes());
    json!({
        "v": 1, "kind": "keyset", "action_kind": "keyset.add",
        "args": {"role": role, "key_id": key_id, "public_key": public},
        "target_node_id": target, "person_id": target,
        "approval_id": null, "execution_key": null, "reply_id": null, "summary": null,
        "issued_at": ts(issued_at), "valid_until": ts(issued_at + Duration::hours(24)),
    })
}

/// The `keyset` message payload for a signed keyset object.
pub fn keyset_payload(outbox_id: Uuid, authz: &Value) -> Value {
    let args = &authz["object"]["args"];
    json!({
        "outbox_id": outbox_id, "role": args["role"], "key_id": args["key_id"],
        "public_key": args["public_key"], "authz": authz,
    })
}

/// An approval card object for `target`, expiring at `valid_until`.
pub fn card_object(
    target: Uuid,
    approval_id: Uuid,
    now: DateTime<FixedOffset>,
    valid_until: DateTime<FixedOffset>,
) -> Value {
    json!({
        "v": 1, "kind": "approval.card", "action_kind": "pc.delete_test_file",
        "args": {"name": "fictional.txt", "file_id": format!("{}:{}", "a".repeat(16), "b".repeat(32)),
                 "before_sha256": "c".repeat(64), "size": 12},
        "target_node_id": target, "person_id": Uuid::new_v4(),
        "approval_id": approval_id, "execution_key": null, "reply_id": null,
        "summary": "Delete test file `fictional.txt`?",
        "issued_at": ts(now), "valid_until": ts(valid_until),
    })
}

/// The `outbox.item` payload for a signed card.
pub fn card_item(outbox_id: Uuid, authz: &Value) -> Value {
    json!({
        "outbox_id": outbox_id, "priority": "URGENT", "kind": "approval.requested",
        "body": "Delete test file `fictional.txt`?", "expires_at": authz["object"]["valid_until"],
        "authz": authz,
    })
}
