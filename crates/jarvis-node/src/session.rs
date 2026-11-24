//! One connection to the gateway, as a sans-IO state machine (spec §6.1, §7.1, §7.2).
//!
//! The driver (`client`) feeds it every text frame and writes what it returns. The session:
//! - insists the first frame is a valid `challenge`, checked against the pinned GATEWAY keys,
//!   or, while enrolling, against the gateway fingerprint in the enrolment code;
//! - answers with a fully signed `hello` or `enrol` (`node-msg`, under the node's own
//!   fingerprint, R12-6), and counts its own `seq` from 1;
//! - after an enrolment, pins the core key only if `welcome.enrolment` matches the code's
//!   core fingerprint, and only then sets the node ID and both pins (§7.2 step 5);
//! - checks every later frame: the peer's `seq`, that the challenge's key is still a trusted
//!   gateway key, the `gw-transport` signature under that key, the envelope's shape, and that
//!   it is addressed to this node;
//! - answers `ping` with `pong`, and passes every other gateway message on as a `Frame`.
//!
//! A failure gives `Stop`. `reenrol` is true only when the gateway's key is one this node never
//! pinned or no longer trusts, or when it holds no core key (R12-29): a bad signature under a
//! still-trusted key means close and reconnect. Nothing core-authorised is judged here; the
//! runtime checks `authz` with the core keys from `state()`.

use std::time::Duration;

use chrono::{DateTime, FixedOffset, SecondsFormat};
use ed25519_dalek::SigningKey;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::code::EnrolmentCode;
use crate::rules::{check_challenge, check_enrolment_welcome, GatewayKeys, SeqChecker};
use crate::sign::{b64url, b64url_decode, fingerprint, sign_envelope, verify_envelope, Purpose};
use crate::state::NodeState;
use crate::trust::{PinnedKey, Trust};
use crate::wire::{parse_envelope, parse_welcome, NIL_UUID};

/// The close code that means "re-enrol" (a STALE node, R6-6).
pub const REENROL_CLOSE: u16 = 4409;

/// Clock skew beyond this is worth showing ("this device's clock is off"); display only.
pub const CLOCK_SKEW_LIMIT_MS: i64 = 30_000;

/// How this connection starts.
#[derive(Debug, Clone)]
pub enum Start {
    /// A new device with an enrolment code (path B).
    Enrol(EnrolmentCode),
    /// An enrolled node; `resume` is the `hello` resume list (§6.2 step 6).
    Hello { resume: Vec<Value> },
}

/// What the driver must do with a frame's result.
#[derive(Debug, Clone, PartialEq)]
pub enum Output {
    /// Write this text frame to the gateway.
    Send(String),
    /// The handshake is complete. After an enrolment (`enrolled`), save the key and `state()`.
    Ready {
        session_id: Option<Uuid>,
        summary: Vec<Value>,
        enrolled: bool,
    },
    /// A verified gateway message for the runtime.
    Frame { kind: String, payload: Value },
    /// The gateway's clock differs from this device's by this many milliseconds (display only).
    ClockOff(i64),
}

/// The connection must close. Nothing more is sent on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stop {
    pub reason: &'static str,
    /// Ask for re-enrolment instead of reconnecting.
    pub reenrol: bool,
}

/// What a close from the gateway means (R12-29).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AfterClose {
    Reconnect,
    Reenrol,
}

/// Reconnect after any close except 4409; 4401 with any reason means reconnect.
pub fn after_close(code: u16) -> AfterClose {
    if code == REENROL_CLOSE {
        AfterClose::Reenrol
    } else {
        AfterClose::Reconnect
    }
}

/// The wait before reconnect attempt `attempt` (0-based): 1, 2, 4, 8, 16, then 30 s.
pub fn backoff(attempt: u32) -> Duration {
    Duration::from_secs(if attempt >= 5 { 30 } else { 1 << attempt })
}

/// Why `send` can't send.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    #[error("the connection is not ready")]
    NotReady,
    #[error("the payload can't be signed (an integer outside the JCS range)")]
    Unsignable,
}

#[derive(Debug, Clone)]
enum Phase {
    AwaitChallenge,
    AwaitWelcome {
        gw_key_id: String,
        gw_public: [u8; 32],
    },
    Ready {
        gw_key_id: String,
        gw_public: [u8; 32],
    },
    Stopped,
}

/// Gateway messages a node may receive after the handshake; everything else is refused.
const FROM_GATEWAY: [&str; 9] = [
    "reply.chunk",
    "outbox.item",
    "outbox.cleared",
    "node.action",
    "keyset",
    "ping",
    "pong",
    "error",
    "welcome",
];

pub struct Session {
    key: SigningKey,
    key_id: String,
    public: [u8; 32],
    state: NodeState,
    start: Start,
    phase: Phase,
    out_seq: i64,
    peer: SeqChecker,
}

impl Session {
    /// A new connection. For `Start::Enrol`, `state` should be empty and `key` freshly made.
    pub fn new(key: SigningKey, state: NodeState, start: Start) -> Session {
        let public = key.verifying_key().to_bytes();
        Session {
            key_id: fingerprint(&public),
            public,
            key,
            state,
            start,
            phase: Phase::AwaitChallenge,
            out_seq: 0,
            peer: SeqChecker::new(),
        }
    }

    /// The node's state: after an enrolment, the new node ID and pins.
    pub fn state(&self) -> &NodeState {
        &self.state
    }

    pub fn is_ready(&self) -> bool {
        matches!(self.phase, Phase::Ready { .. })
    }

    fn stop(&mut self, reason: &'static str, reenrol: bool) -> Stop {
        self.phase = Phase::Stopped;
        Stop { reason, reenrol }
    }

    /// Handle one text frame from the gateway.
    pub fn on_frame(
        &mut self,
        text: &str,
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<Output>, Stop> {
        let Ok(env) = serde_json::from_str::<Value>(text) else {
            return Err(self.stop("malformed", false));
        };
        match self.phase.clone() {
            Phase::AwaitChallenge => self.on_challenge(&env, now),
            Phase::AwaitWelcome {
                gw_key_id,
                gw_public,
            } => self.on_welcome(&env, &gw_key_id, &gw_public, now),
            Phase::Ready {
                gw_key_id,
                gw_public,
            } => self.on_message(&env, &gw_key_id, &gw_public, now),
            Phase::Stopped => Err(Stop {
                reason: "stopped",
                reenrol: false,
            }),
        }
    }

    fn on_challenge(
        &mut self,
        env: &Value,
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<Output>, Stop> {
        let hello = matches!(self.start, Start::Hello { .. });
        let pinned: Option<GatewayKeys> = hello.then(|| {
            self.state
                .trust
                .gateway
                .keys()
                .filter_map(|id| Some((id.clone(), self.state.trust.gateway_key(id, now)?)))
                .collect()
        });
        let enrol_fp = match &self.start {
            Start::Enrol(code) => Some(code.gateway_fp.clone()),
            Start::Hello { .. } => None,
        };
        let challenge = match check_challenge(env, pinned.as_ref(), enrol_fp.as_deref()) {
            Ok(c) => c,
            // A gateway key this enrolled node never pinned (or no longer trusts): re-enrol.
            Err(r) => return Err(self.stop(r.0, hello && r.0 == "unpinned_key")),
        };
        if let Err(r) = self.peer.expect(env["seq"].as_i64().unwrap_or(0)) {
            return Err(self.stop(r.0, false));
        }
        let gw_public: [u8; 32] = b64url_decode(&challenge.public_key, 32)
            .and_then(|k| k.try_into().ok())
            .expect("check_challenge validated the key");

        let mut out = Vec::new();
        let skew_ms = (now - challenge.issued_at).num_milliseconds();
        if skew_ms.abs() > CLOCK_SKEW_LIMIT_MS {
            out.push(Output::ClockOff(skew_ms));
        }
        let (kind, payload, node_id) = match &self.start {
            Start::Hello { resume } => {
                let Some(node_id) = self.state.node_id else {
                    return Err(self.stop("not_enrolled", true));
                };
                let payload = json!({
                    "nonce": challenge.nonce,
                    "resume": resume,
                    "clock_skew_ms": skew_ms,
                });
                ("hello", payload, node_id)
            }
            Start::Enrol(code) => {
                let payload = json!({
                    "token": code.token,
                    "public_key": b64url(&self.public),
                    "nonce": challenge.nonce,
                });
                ("enrol", payload, NIL_UUID)
            }
        };
        let Ok(frame) = self.envelope(kind, payload, node_id, now) else {
            return Err(self.stop("unsignable", false));
        };
        out.push(Output::Send(frame));
        self.phase = Phase::AwaitWelcome {
            gw_key_id: challenge.key_id,
            gw_public,
        };
        Ok(out)
    }

    /// `seq`, then the signature under the challenge's key.
    fn transport(
        &mut self,
        env: &Value,
        gw_key_id: &str,
        gw_public: &[u8; 32],
    ) -> Result<(), Stop> {
        if let Err(r) = self.peer.expect(env["seq"].as_i64().unwrap_or(0)) {
            return Err(self.stop(r.0, false));
        }
        if !verify_envelope(gw_public, Purpose::GwTransport, gw_key_id, env) {
            return Err(self.stop("bad_signature", false));
        }
        Ok(())
    }

    fn on_welcome(
        &mut self,
        env: &Value,
        gw_key_id: &str,
        gw_public: &[u8; 32],
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<Output>, Stop> {
        self.transport(env, gw_key_id, gw_public)?;
        let Some(envelope) = parse_envelope(env) else {
            return Err(self.stop("malformed", false));
        };
        if envelope.kind != "welcome" {
            return Err(self.stop("expected_welcome", false));
        }
        let payload = &env["payload"];
        let enrolled = match &self.start {
            Start::Enrol(code) => {
                let core_fp = code.core_fp.clone();
                let (node_id, core_key_id, core_public) =
                    match check_enrolment_welcome(payload, &core_fp) {
                        Ok(pin) => pin,
                        Err(r) => return Err(self.stop(r.0, false)),
                    };
                if envelope.node_id != node_id {
                    return Err(self.stop("node_id_mismatch", false));
                }
                let mut trust = Trust::default();
                trust.core.insert(core_key_id, PinnedKey::new(core_public));
                trust
                    .gateway
                    .insert(gw_key_id.to_owned(), PinnedKey::new(*gw_public));
                self.state = NodeState {
                    node_id: Some(node_id),
                    node_key_id: Some(self.key_id.clone()),
                    trust,
                };
                true
            }
            Start::Hello { .. } => {
                if Some(envelope.node_id) != self.state.node_id {
                    return Err(self.stop("wrong_node", false));
                }
                // A node that holds no trusted core key can't accept anything: re-enrol.
                if self.state.trust.core_keys(now).is_empty() {
                    return Err(self.stop("no_core_key", true));
                }
                false
            }
        };
        // An enrolment block outside an enrolment is never used to pin anything.
        let Some(welcome) = parse_welcome(payload) else {
            return Err(self.stop("malformed", false));
        };
        self.phase = Phase::Ready {
            gw_key_id: gw_key_id.to_owned(),
            gw_public: *gw_public,
        };
        Ok(vec![Output::Ready {
            session_id: welcome.session_id,
            summary: welcome.summary,
            enrolled,
        }])
    }

    fn on_message(
        &mut self,
        env: &Value,
        gw_key_id: &str,
        gw_public: &[u8; 32],
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<Output>, Stop> {
        if let Err(r) = self.peer.expect(env["seq"].as_i64().unwrap_or(0)) {
            return Err(self.stop(r.0, false));
        }
        // The connection's gateway key must still be trusted (its overlap may have ended).
        if self.state.trust.gateway_key(gw_key_id, now) != Some(*gw_public) {
            return Err(self.stop("unpinned_key", true));
        }
        if !verify_envelope(gw_public, Purpose::GwTransport, gw_key_id, env) {
            return Err(self.stop("bad_signature", false));
        }
        let Some(envelope) = parse_envelope(env) else {
            return Err(self.stop("malformed", false));
        };
        if Some(envelope.node_id) != self.state.node_id {
            return Err(self.stop("wrong_node", false));
        }
        let kind = envelope.kind.as_str();
        if !FROM_GATEWAY.contains(&kind) || kind == "welcome" {
            return Err(self.stop("unexpected_kind", false));
        }
        match kind {
            "ping" => {
                let node_id = self.state.node_id.expect("ready means enrolled");
                match self.envelope("pong", json!({}), node_id, now) {
                    Ok(frame) => Ok(vec![Output::Send(frame)]),
                    Err(_) => Err(self.stop("unsignable", false)),
                }
            }
            "pong" => Ok(vec![]),
            _ => Ok(vec![Output::Frame {
                kind: kind.to_owned(),
                payload: Value::Object(envelope.payload),
            }]),
        }
    }

    /// A signed message for the gateway, once the handshake is complete.
    pub fn send(
        &mut self,
        kind: &str,
        payload: Value,
        now: DateTime<FixedOffset>,
    ) -> Result<String, SendError> {
        let Phase::Ready { .. } = self.phase else {
            return Err(SendError::NotReady);
        };
        let node_id = self.state.node_id.ok_or(SendError::NotReady)?;
        self.envelope(kind, payload, node_id, now)
            .map_err(|_| SendError::Unsignable)
    }

    /// A fully signed envelope with the next `seq` (consumed only if signing succeeds).
    fn envelope(
        &mut self,
        kind: &str,
        payload: Value,
        node_id: Uuid,
        now: DateTime<FixedOffset>,
    ) -> Result<String, crate::sign::SignError> {
        let mut env = json!({
            "version": 1,
            "message_id": Uuid::new_v4(),
            "node_id": node_id,
            "seq": self.out_seq + 1,
            "sent_at": now.to_rfc3339_opts(SecondsFormat::Millis, true),
            "kind": kind,
            "payload": payload,
        });
        sign_envelope(&self.key, Purpose::NodeMsg, &self.key_id, &mut env)?;
        self.out_seq += 1;
        Ok(env.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn after_close_reenrols_only_on_4409() {
        assert_eq!(after_close(4409), AfterClose::Reenrol);
        for code in [4401, 1000, 1001, 1005, 1006, 1011, 4400, 4408, 4410] {
            assert_eq!(after_close(code), AfterClose::Reconnect, "{code}");
        }
    }

    #[test]
    fn backoff_is_1_2_4_8_16_then_30() {
        let secs: Vec<u64> = (0..8).map(|a| backoff(a).as_secs()).collect();
        assert_eq!(secs, [1, 2, 4, 8, 16, 30, 30, 30]);
        assert_eq!(backoff(u32::MAX).as_secs(), 30);
    }
}
