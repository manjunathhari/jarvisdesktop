//! A scripted fake gateway: builds signed `gw-transport` frames with the shared test keys.
#![allow(dead_code)]

use chrono::{DateTime, Duration, FixedOffset, SecondsFormat};
use ed25519_dalek::SigningKey;
use jarvis_node::sign::{b64url, fingerprint, sign_envelope, Purpose};
use serde_json::{json, Value};
use uuid::Uuid;

pub const NIL: Uuid = Uuid::nil();

pub struct Gw {
    pub key: SigningKey,
    pub key_id: String,
    pub seq: i64,
    pub nonce: String,
}

impl Gw {
    /// A gateway signing under the vector key `label`, announced as `key_id`.
    pub fn new(label: &str, key_id: &str) -> Gw {
        Gw {
            key: super::test_key(label),
            key_id: key_id.to_owned(),
            seq: 0,
            nonce: b64url(&[7u8; 32]),
        }
    }

    pub fn public(&self) -> [u8; 32] {
        self.key.verifying_key().to_bytes()
    }

    /// A signed frame with the next `seq`.
    pub fn frame(
        &mut self,
        kind: &str,
        node_id: Uuid,
        payload: Value,
        now: DateTime<FixedOffset>,
    ) -> String {
        self.seq += 1;
        let seq = self.seq;
        self.frame_with_seq(kind, node_id, payload, now, seq)
    }

    pub fn frame_with_seq(
        &self,
        kind: &str,
        node_id: Uuid,
        payload: Value,
        now: DateTime<FixedOffset>,
        seq: i64,
    ) -> String {
        let mut env = json!({
            "version": 1,
            "message_id": Uuid::new_v4(),
            "node_id": node_id,
            "seq": seq,
            "sent_at": now.to_rfc3339_opts(SecondsFormat::Secs, true),
            "kind": kind,
            "payload": payload,
        });
        sign_envelope(&self.key, Purpose::GwTransport, &self.key_id, &mut env).unwrap();
        env.to_string()
    }

    pub fn challenge(&mut self, now: DateTime<FixedOffset>) -> String {
        let payload = json!({
            "nonce": self.nonce,
            "issued_at": now.to_rfc3339_opts(SecondsFormat::Secs, true),
            "expires_at": (now + Duration::seconds(30)).to_rfc3339_opts(SecondsFormat::Secs, true),
            "key_id": self.key_id,
            "public_key": b64url(&self.public()),
        });
        self.frame("challenge", NIL, payload, now)
    }

    /// `welcome`, with an enrolment block when `core` (key_id, public key) is given.
    pub fn welcome(
        &mut self,
        node_id: Uuid,
        core: Option<(&str, [u8; 32])>,
        now: DateTime<FixedOffset>,
    ) -> String {
        let mut payload = json!({"session_id": null, "summary": []});
        if let Some((key_id, public)) = core {
            payload["enrolment"] = json!({
                "node_id": node_id,
                "core_key_id": key_id,
                "core_public_key": b64url(&public),
            });
        }
        self.frame("welcome", node_id, payload, now)
    }
}

/// An enrolment code for these keys: a fixed token, the core and gateway fingerprints.
pub fn code_for(core: &[u8; 32], gw: &[u8; 32]) -> String {
    format!(
        "{}.{}.{}",
        "a".repeat(26),
        fingerprint(core),
        fingerprint(gw)
    )
}

pub fn now() -> DateTime<FixedOffset> {
    chrono::Utc::now().fixed_offset()
}
