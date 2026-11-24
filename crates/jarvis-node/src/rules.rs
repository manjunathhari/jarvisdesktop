//! Node-side authorisation rules (spec §7.1), as the Python reference
//! `server/tests/fakes/node_rules.py` defines them, reason strings and order included.
//!
//! `trusted` only ever holds CORE keys, so a gateway key is never accepted as a core one
//! (R6-3). Rule 1 (a valid `gw-transport` envelope on the authenticated connection) belongs to
//! the session, and rule 5 (an unused `execution_key`) to the journal.

use std::collections::BTreeMap;

use chrono::{DateTime, Duration, FixedOffset};
use serde_json::Value;
use uuid::Uuid;

use crate::sign::{verify, Purpose};
use crate::wire::{has_exactly, is_b64url_text, is_key_id, parse_authz, AuthzKind, AuthzObject};

/// Clock tolerance for every validity check.
pub const CLOCK_TOLERANCE: Duration = Duration::seconds(30);

/// Trusted CORE keys: `key_id` → raw 32-byte Ed25519 public key.
pub type CoreKeys = BTreeMap<String, [u8; 32]>;

/// Why something was refused; the reason strings are exactly the Python reference's.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct Rejected(pub &'static str);

/// The signed object, if `authz` is well formed, its `key_id` is a trusted core key and its
/// `core-authz` signature verifies over `authz.object` exactly as received.
///
/// Reasons, in order: `malformed`, `untrusted_key_id`, `bad_signature`.
pub fn check_authz(authz: &Value, trusted: &CoreKeys) -> Result<AuthzObject, Rejected> {
    let parsed = parse_authz(authz).ok_or(Rejected("malformed"))?;
    let key = trusted
        .get(&parsed.key_id)
        .ok_or(Rejected("untrusted_key_id"))?;
    if !verify(
        key,
        Purpose::CoreAuthz,
        &parsed.key_id,
        &authz["object"],
        &parsed.sig,
    ) {
        return Err(Rejected("bad_signature"));
    }
    Ok(parsed.object)
}

fn expired(obj: &AuthzObject, now: DateTime<FixedOffset>) -> bool {
    now > obj.valid_until + CLOCK_TOLERANCE
}

/// Rules 2 to 4 for a `node.action`.
///
/// Reasons, in order: `malformed`, `untrusted_key_id`, `bad_signature`, `wrong_kind`,
/// `wrong_target`, `expired` (`now` > `valid_until` + 30 s).
pub fn check_node_action(
    authz: &Value,
    trusted: &CoreKeys,
    me: Uuid,
    now: DateTime<FixedOffset>,
) -> Result<AuthzObject, Rejected> {
    let obj = check_authz(authz, trusted)?;
    if obj.kind != AuthzKind::NodeAction {
        return Err(Rejected("wrong_kind"));
    }
    if obj.target_node_id != Some(me) {
        return Err(Rejected("wrong_target"));
    }
    if expired(&obj, now) {
        return Err(Rejected("expired"));
    }
    Ok(obj)
}

const KEYSET_FIELDS: [&str; 3] = ["role", "key_id", "public_key"];

/// A well-formed keyset payload: exactly `role` (core or gateway), `key_id` and `public_key`.
fn keyset_payload_ok(payload: &Value) -> bool {
    let Some(map) = payload.as_object() else {
        return false;
    };
    has_exactly(map, &KEYSET_FIELDS)
        && matches!(map["role"].as_str(), Some("core" | "gateway"))
        && map["key_id"].as_str().is_some_and(is_key_id)
        && map["public_key"]
            .as_str()
            .is_some_and(|k| is_b64url_text(k, 43))
}

/// A `keyset` announcement (R6-2, R12-26, R12-27). `payload` is the message payload without
/// its unsigned `outbox_id` and its `authz`; `authz` comes separately. Validity comes only from
/// the signed object. Which pin set the new key joins is the trust store's job.
///
/// Reasons, in order: `malformed`, `untrusted_key_id`, `bad_signature`, `wrong_kind`,
/// `wrong_target` (the signed `target_node_id` is neither null nor `me`), `expired`,
/// `keyset_mismatch` (the payload's role, key_id or public_key differs from `authz.object.args`).
pub fn check_keyset(
    payload: &Value,
    authz: &Value,
    trusted: &CoreKeys,
    me: Uuid,
    now: DateTime<FixedOffset>,
) -> Result<AuthzObject, Rejected> {
    if !keyset_payload_ok(payload) || parse_authz(authz).is_none() {
        return Err(Rejected("malformed"));
    }
    let obj = check_authz(authz, trusted)?;
    if obj.kind != AuthzKind::Keyset {
        return Err(Rejected("wrong_kind"));
    }
    if obj.target_node_id.is_some_and(|t| t != me) {
        return Err(Rejected("wrong_target"));
    }
    if expired(&obj, now) {
        return Err(Rejected("expired"));
    }
    let same = obj.args.len() == KEYSET_FIELDS.len()
        && KEYSET_FIELDS
            .iter()
            .all(|f| obj.args.get(*f) == payload.get(*f));
    if !same {
        return Err(Rejected("keyset_mismatch"));
    }
    Ok(obj)
}
