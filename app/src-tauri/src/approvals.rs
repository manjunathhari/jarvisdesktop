//! Approval cards and server notices in the window (plan B Task 7).
//!
//! The node is started by the ServerBrain cutover (Task 9); until then `NodeSlot` is empty,
//! no card can arrive, and "approve" / "deny" reach the coordinator as before.
//! - `forward` relays the node's `UiEvent`s to the window: `jarvis://approval-card`,
//!   `jarvis://approval-cleared`, `jarvis://notice`, `jarvis://notice-cleared`.
//! - `decide_approval` is the Approve / Deny buttons' command.
//! - `intercept` takes a spoken "approve" or "deny" before it reaches the coordinator, when
//!   exactly one card can still be decided; with two or more it asks the person to tap. The
//!   swallowed phrase leaves the coordinator listening until its own timer ends, as silence
//!   would.
//! - A card ends in the window on `jarvis://approval-cleared`: reason `SENT` (this node's
//!   decision went out, by tap or voice) shows "Sent" briefly; any other reason removes it.

use std::sync::{Arc, Mutex};

use jarvis_node::approval::{pending_voice_decision, ApprovalCard, Decision, VoiceDecision};
use jarvis_node::node::{DecideError, NodeHandle, UiEvent as NodeEvent};
use serde::Serialize;
use tauri::{AppHandle, Emitter, State};
use tokio::sync::mpsc;
use uuid::Uuid;

/// The running node, once Task 9 starts it.
pub type NodeSlot = Arc<Mutex<Option<NodeHandle>>>;

pub const ASK_TO_TAP: &str = "More than one approval is open. Tap the one you mean.";
pub const NOT_SENT_EXPIRED: &str = "That approval has expired, so nothing was sent.";
pub const NOT_SENT_GONE: &str = "That approval is no longer open.";
pub const NOT_SENT_OFFLINE: &str = "I can't reach the Jarvis server, so nothing was sent.";

/// What the window draws for a card. The countdown runs from `valid_until`.
#[allow(dead_code)] // reached through `forward`, which the Task 9 cutover starts
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CardView {
    pub outbox_id: String,
    pub approval_id: String,
    pub summary: String,
    pub valid_until: String,
}

#[allow(dead_code)] // reached through `forward`, which the Task 9 cutover starts
pub fn card_view(card: &ApprovalCard) -> CardView {
    CardView {
        outbox_id: card.outbox_id.to_string(),
        approval_id: card.approval_id.to_string(),
        summary: card.summary.clone(),
        valid_until: card.valid_until.to_rfc3339(),
    }
}

#[allow(dead_code)] // reached through `forward`, which the Task 9 cutover starts
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Cleared {
    pub outbox_id: String,
    pub reason: String,
}

#[allow(dead_code)] // reached through `forward`, which the Task 9 cutover starts
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NoticeView {
    pub outbox_id: String,
    pub priority: String,
    pub kind: String,
    pub body: String,
}

/// What to do with a spoken phrase.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Intercept {
    /// Not a decision: it goes to the coordinator as usual.
    Pass,
    Decide(Uuid, Decision),
    AskToTap,
}

pub fn intercept(text: &str, pending: usize, only: Option<Uuid>) -> Intercept {
    match (pending_voice_decision(text, pending), only) {
        (VoiceDecision::Decide(d), Some(card)) => Intercept::Decide(card, d),
        (VoiceDecision::AskToTap, _) => Intercept::AskToTap,
        _ => Intercept::Pass,
    }
}

/// Words for a decision that wasn't sent.
pub fn refusal_text(e: DecideError) -> &'static str {
    match e {
        DecideError::Expired => NOT_SENT_EXPIRED,
        DecideError::Gone => NOT_SENT_GONE,
        DecideError::NotConnected => NOT_SENT_OFFLINE,
    }
}

fn node(slot: &NodeSlot) -> Option<NodeHandle> {
    slot.lock().ok().and_then(|n| n.clone())
}

/// Handle `text` here if it is a decision on an open card; `say` speaks a line locally.
/// Returns whether the phrase was taken (and must not reach the coordinator).
pub fn take_spoken_decision(
    slot: &NodeSlot,
    text: &str,
    say: impl Fn(&str) + Send + 'static,
) -> bool {
    let Some(node) = node(slot) else {
        return false;
    };
    let (pending, only) = node.voice_target();
    match intercept(text, pending, only) {
        Intercept::Pass => false,
        Intercept::AskToTap => {
            say(ASK_TO_TAP);
            true
        }
        Intercept::Decide(card, d) => {
            tauri::async_runtime::spawn(async move {
                if let Err(e) = node.decide(card, d).await {
                    say(refusal_text(e));
                }
            });
            true
        }
    }
}

#[tauri::command]
pub async fn decide_approval(
    outbox_id: String,
    approve: bool,
    node_slot: State<'_, NodeSlot>,
) -> Result<(), String> {
    let id = Uuid::parse_str(&outbox_id).map_err(|_| NOT_SENT_GONE.to_owned())?;
    let node = node(&node_slot).ok_or_else(|| NOT_SENT_OFFLINE.to_owned())?;
    let d = if approve {
        Decision::Approve
    } else {
        Decision::Deny
    };
    node.decide(id, d)
        .await
        .map_err(|e| refusal_text(e).to_owned())
}

/// Relay the node's events to the window until the node stops. Notices are acknowledged
/// once they have been handed to the window (`NodeHandle::shown`).
#[allow(dead_code)] // reached through `forward`, which the Task 9 cutover starts
pub async fn forward(app: AppHandle, slot: NodeSlot, mut events: mpsc::Receiver<NodeEvent>) {
    while let Some(ev) = events.recv().await {
        let r = match ev {
            NodeEvent::ApprovalCard(card) => app.emit("jarvis://approval-card", card_view(&card)),
            NodeEvent::ApprovalCleared { outbox_id, reason } => app.emit(
                "jarvis://approval-cleared",
                Cleared {
                    outbox_id: outbox_id.to_string(),
                    reason,
                },
            ),
            NodeEvent::Notice {
                outbox_id,
                priority,
                kind,
                body,
            } => {
                let r = app.emit(
                    "jarvis://notice",
                    NoticeView {
                        outbox_id: outbox_id.to_string(),
                        priority,
                        kind,
                        body,
                    },
                );
                if r.is_ok() {
                    if let Some(node) = node(&slot) {
                        let _ = node.shown(outbox_id).await;
                    }
                }
                r
            }
            NodeEvent::NoticeCleared { outbox_id, reason } => app.emit(
                "jarvis://notice-cleared",
                Cleared {
                    outbox_id: outbox_id.to_string(),
                    reason,
                },
            ),
            // Link status, the reconnect summary and clock warnings: Task 9's screen.
            NodeEvent::Summary(_) | NodeEvent::Link(_) | NodeEvent::ClockOff(_) => Ok(()),
        };
        if let Err(e) = r {
            tracing::warn!(error = %e, "emit failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spoken_decisions_need_exactly_one_open_card() {
        let card = Uuid::new_v4();
        assert_eq!(intercept("approve", 0, None), Intercept::Pass);
        assert_eq!(
            intercept("Jarvis, approve it.", 1, Some(card)),
            Intercept::Decide(card, Decision::Approve)
        );
        assert_eq!(
            intercept("deny", 1, Some(card)),
            Intercept::Decide(card, Decision::Deny)
        );
        assert_eq!(intercept("approve", 2, None), Intercept::AskToTap);
        assert_eq!(
            intercept("approve the news", 1, Some(card)),
            Intercept::Pass
        );
        assert_eq!(
            intercept("bring me the news", 1, Some(card)),
            Intercept::Pass
        );
    }

    #[test]
    fn no_node_means_nothing_is_taken() {
        let slot: NodeSlot = Arc::default();
        assert!(!take_spoken_decision(&slot, "approve", |_| panic!(
            "nothing to say"
        )));
    }

    #[test]
    fn the_window_gets_the_fields_it_draws() {
        let view = CardView {
            outbox_id: Uuid::nil().to_string(),
            approval_id: Uuid::nil().to_string(),
            summary: "Delete test file `fictional.txt`?".into(),
            valid_until: "2026-11-25T06:00:00+00:00".into(),
        };
        let json = serde_json::to_value(&view).unwrap();
        let mut keys: Vec<&str> = json
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(keys, ["approval_id", "outbox_id", "summary", "valid_until"]);
    }

    #[test]
    fn every_refusal_has_words() {
        for e in [
            DecideError::Expired,
            DecideError::Gone,
            DecideError::NotConnected,
        ] {
            assert!(!refusal_text(e).is_empty());
        }
    }
}
