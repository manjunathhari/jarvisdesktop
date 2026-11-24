//! Wire types for the core-signed authorisation object (spec §7.1,
//! `contracts/messages/authz.schema.json`).
//!
//! Parsing is strict and mirrors the server's contract models: every field present (null if
//! unused), no extra fields, and the same patterns. A value that fails here is `malformed`.

use chrono::{DateTime, FixedOffset};
use serde_json::{Map, Value};
use uuid::Uuid;

/// An RFC 3339 time with an explicit offset; `None` for one without (R6-4).
pub fn parse_ts(text: &str) -> Option<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(text).ok()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthzKind {
    NodeAction,
    ApprovalCard,
    Keyset,
}

impl AuthzKind {
    fn parse(text: &str) -> Option<Self> {
        match text {
            "node.action" => Some(Self::NodeAction),
            "approval.card" => Some(Self::ApprovalCard),
            "keyset" => Some(Self::Keyset),
            _ => None,
        }
    }
}

/// The signed `authz.object`, with `raw` kept exactly as received (signatures cover it).
#[derive(Debug, Clone, PartialEq)]
pub struct AuthzObject {
    pub kind: AuthzKind,
    pub action_kind: String,
    pub args: Map<String, Value>,
    pub target_node_id: Option<Uuid>,
    pub person_id: Option<Uuid>,
    pub approval_id: Option<Uuid>,
    pub execution_key: Option<Uuid>,
    pub reply_id: Option<Uuid>,
    pub summary: Option<String>,
    pub issued_at: DateTime<FixedOffset>,
    pub valid_until: DateTime<FixedOffset>,
    pub raw: Value,
}

const OBJECT_FIELDS: [&str; 12] = [
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
];

/// `{object, key_id, sig}`, parsed; the signature is not checked here.
#[derive(Debug, Clone, PartialEq)]
pub struct Authz {
    pub object: AuthzObject,
    pub key_id: String,
    pub sig: String,
}

/// Exactly these keys, no more, no fewer.
pub(crate) fn has_exactly(map: &Map<String, Value>, fields: &[&str]) -> bool {
    map.len() == fields.len() && fields.iter().all(|f| map.contains_key(*f))
}

/// `^[A-Za-z0-9_.:-]{1,64}$`
pub(crate) fn is_key_id(text: &str) -> bool {
    (1..=64).contains(&text.len())
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b':' | b'-'))
}

/// `^[A-Za-z0-9_-]{n}$`: base64url text of a fixed length (43 for a key, 86 for a signature).
pub(crate) fn is_b64url_text(text: &str, n: usize) -> bool {
    text.len() == n
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}

/// `^[a-z][a-z0-9_.]{0,63}$`
fn is_code(text: &str) -> bool {
    let mut bytes = text.bytes();
    matches!(bytes.next(), Some(b'a'..=b'z'))
        && text.len() <= 64
        && bytes.all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.'))
}

fn opt_uuid(v: &Value) -> Option<Option<Uuid>> {
    match v {
        Value::Null => Some(None),
        Value::String(s) => Uuid::parse_str(s).ok().map(Some),
        _ => None,
    }
}

fn time(v: &Value) -> Option<DateTime<FixedOffset>> {
    parse_ts(v.as_str()?)
}

/// The signed object, or `None` if it is malformed.
pub fn parse_authz_object(v: &Value) -> Option<AuthzObject> {
    let map = v.as_object()?;
    if !has_exactly(map, &OBJECT_FIELDS) || map["v"].as_u64() != Some(1) || !map["v"].is_u64() {
        return None;
    }
    let action_kind = map["action_kind"]
        .as_str()
        .filter(|s| is_code(s))?
        .to_owned();
    let summary = match &map["summary"] {
        Value::Null => None,
        Value::String(s) if (1..=500).contains(&s.chars().count()) => Some(s.clone()),
        _ => return None,
    };
    Some(AuthzObject {
        kind: AuthzKind::parse(map["kind"].as_str()?)?,
        action_kind,
        args: map["args"].as_object()?.clone(),
        target_node_id: opt_uuid(&map["target_node_id"])?,
        person_id: opt_uuid(&map["person_id"])?,
        approval_id: opt_uuid(&map["approval_id"])?,
        execution_key: opt_uuid(&map["execution_key"])?,
        reply_id: opt_uuid(&map["reply_id"])?,
        summary,
        issued_at: time(&map["issued_at"])?,
        valid_until: time(&map["valid_until"])?,
        raw: v.clone(),
    })
}

/// `{object, key_id, sig}`, or `None` if it is malformed.
pub fn parse_authz(v: &Value) -> Option<Authz> {
    let map = v.as_object()?;
    if !has_exactly(map, &["object", "key_id", "sig"]) {
        return None;
    }
    let key_id = map["key_id"].as_str().filter(|s| is_key_id(s))?.to_owned();
    let sig = map["sig"]
        .as_str()
        .filter(|s| is_b64url_text(s, 86))?
        .to_owned();
    Some(Authz {
        object: parse_authz_object(&map["object"])?,
        key_id,
        sig,
    })
}
