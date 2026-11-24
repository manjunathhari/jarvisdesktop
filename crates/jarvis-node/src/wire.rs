//! Wire types for the core-signed authorisation object (spec §7.1,
//! `contracts/messages/authz.schema.json`).
//!
//! Parsing is strict and mirrors the server's contract models: every field present (null if
//! unused), no extra fields, and the same patterns. A value that fails here is `malformed`.

use chrono::{DateTime, Datelike as _, FixedOffset, Timelike as _};
use serde_json::{Map, Value};
use uuid::Uuid;

/// An RFC 3339 time with an explicit offset; `None` for one without (R6-4).
///
/// A leap second (`:60`) and the year 0000 are refused, as the server's contract models refuse
/// them: chrono would accept both, and the node must never accept a time the server would not.
pub fn parse_ts(text: &str) -> Option<DateTime<FixedOffset>> {
    let t = DateTime::parse_from_rfc3339(text).ok()?;
    (t.nanosecond() < 1_000_000_000 && t.year() >= 1).then_some(t)
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

// ---------------------------------------------------------------- envelopes (§7.1)

/// The nil UUID: the envelope `node_id` before the node is known (`challenge`, `enrol`).
pub const NIL_UUID: Uuid = Uuid::nil();

/// Every message kind `contracts/node-envelope.schema.json` allows.
pub const MESSAGE_KINDS: [&str; 17] = [
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
    "keyset",
    "ping",
    "pong",
    "error",
];

/// A parsed envelope. Its signature is checked separately, over the raw JSON.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    pub message_id: Uuid,
    pub node_id: Uuid,
    pub session_id: Option<Uuid>,
    pub seq: i64,
    pub sent_at: DateTime<FixedOffset>,
    pub kind: String,
    pub payload: Map<String, Value>,
    pub sig: String,
}

fn uuid(v: &Value) -> Option<Uuid> {
    Uuid::parse_str(v.as_str()?).ok()
}

/// The envelope, or `None` if it is malformed: every field present (`session_id` may be left
/// out), no extras, `version` 1, `seq` ≥ 1, a known `kind`, and the nil `node_id` only on
/// `challenge` and `enrol`.
pub fn parse_envelope(v: &Value) -> Option<Envelope> {
    let map = v.as_object()?;
    let required = [
        "version",
        "message_id",
        "node_id",
        "seq",
        "sent_at",
        "kind",
        "payload",
        "sig",
    ];
    // Exactly the required fields, plus `session_id` at most.
    let allowed = required.len() + usize::from(map.contains_key("session_id"));
    if map.len() != allowed || !required.iter().all(|f| map.contains_key(*f)) {
        return None;
    }
    if map["version"].as_u64() != Some(1) {
        return None;
    }
    let session_id = match map.get("session_id") {
        None | Some(Value::Null) => None,
        Some(v) => Some(uuid(v)?),
    };
    let seq = map["seq"].as_i64().filter(|s| *s >= 1)?;
    let kind = map["kind"].as_str().filter(|k| MESSAGE_KINDS.contains(k))?;
    let node_id = uuid(&map["node_id"])?;
    if node_id == NIL_UUID && !matches!(kind, "challenge" | "enrol") {
        return None;
    }
    Some(Envelope {
        message_id: uuid(&map["message_id"])?,
        node_id,
        session_id,
        seq,
        sent_at: time(&map["sent_at"])?,
        kind: kind.to_owned(),
        payload: map["payload"].as_object()?.clone(),
        sig: map["sig"]
            .as_str()
            .filter(|s| is_b64url_text(s, 86))?
            .to_owned(),
    })
}

/// The `challenge` payload (§6.1 step 2).
#[derive(Debug, Clone, PartialEq)]
pub struct Challenge {
    pub nonce: String,
    pub issued_at: DateTime<FixedOffset>,
    pub expires_at: DateTime<FixedOffset>,
    pub key_id: String,
    pub public_key: String,
}

pub fn parse_challenge(payload: &Value) -> Option<Challenge> {
    let map = payload.as_object()?;
    if !has_exactly(
        map,
        &["nonce", "issued_at", "expires_at", "key_id", "public_key"],
    ) {
        return None;
    }
    Some(Challenge {
        nonce: map["nonce"]
            .as_str()
            .filter(|s| is_b64url_text(s, 43))?
            .to_owned(),
        issued_at: time(&map["issued_at"])?,
        expires_at: time(&map["expires_at"])?,
        key_id: map["key_id"].as_str().filter(|s| is_key_id(s))?.to_owned(),
        public_key: map["public_key"]
            .as_str()
            .filter(|s| is_b64url_text(s, 43))?
            .to_owned(),
    })
}

/// The `enrolment` block of a `welcome` after an enrolment (§6.1 step 7).
#[derive(Debug, Clone, PartialEq)]
pub struct EnrolmentBlock {
    pub node_id: Uuid,
    pub core_key_id: String,
    pub core_public_key: String,
}

/// The `welcome` payload.
#[derive(Debug, Clone, PartialEq)]
pub struct Welcome {
    pub session_id: Option<Uuid>,
    /// The reconnect summary groups (§8.3), as received.
    pub summary: Vec<Value>,
    pub enrolment: Option<EnrolmentBlock>,
}

fn text_between(v: &Value, min: usize, max: usize) -> bool {
    v.as_str()
        .is_some_and(|s| (min..=max).contains(&s.chars().count()))
}

fn summary_group_ok(v: &Value) -> bool {
    let Some(map) = v.as_object() else {
        return false;
    };
    has_exactly(map, &["priority", "group_key", "outbox_ids", "line"])
        && matches!(
            map["priority"].as_str(),
            Some("URGENT" | "TODAY" | "DIGEST")
        )
        && (map["group_key"].is_null() || text_between(&map["group_key"], 1, 200))
        && map["outbox_ids"]
            .as_array()
            .is_some_and(|ids| ids.iter().all(|id| uuid(id).is_some()))
        && (map["line"].is_null() || text_between(&map["line"], 1, 500))
}

pub fn parse_welcome(payload: &Value) -> Option<Welcome> {
    let map = payload.as_object()?;
    let with_block = map.contains_key("enrolment");
    let fields: &[&str] = if with_block {
        &["session_id", "summary", "enrolment"]
    } else {
        &["session_id", "summary"]
    };
    if !has_exactly(map, fields) {
        return None;
    }
    let session_id = match &map["session_id"] {
        Value::Null => None,
        v => Some(uuid(v)?),
    };
    let summary = map["summary"].as_array()?;
    if !summary.iter().all(summary_group_ok) {
        return None;
    }
    let enrolment = if with_block {
        // May be left out, but never sent as null.
        let block = map["enrolment"].as_object()?;
        if !has_exactly(block, &["node_id", "core_key_id", "core_public_key"]) {
            return None;
        }
        Some(EnrolmentBlock {
            node_id: uuid(&block["node_id"])?,
            core_key_id: block["core_key_id"]
                .as_str()
                .filter(|s| is_key_id(s))?
                .to_owned(),
            core_public_key: block["core_public_key"]
                .as_str()
                .filter(|s| is_b64url_text(s, 43))?
                .to_owned(),
        })
    } else {
        None
    };
    Some(Welcome {
        session_id,
        summary: summary.clone(),
        enrolment,
    })
}
