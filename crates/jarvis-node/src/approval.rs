//! Approval cards on the desktop (spec §9.3, plan B Task 7).
//!
//! - A card arrives as an `outbox.item` whose `authz` is a core-signed `approval.card` for this
//!   node. `ApprovalCard::from_item` checks it (§7.1 rules 2 to 4, plus an `approval_id`).
//! - The person decides by tapping or by voice ("approve", "deny"). The decision carries the
//!   card's `authz.sig` byte for byte and is sent only while `now < valid_until`; after that the
//!   buttons are off. The server decides what happens: the card stays as "Sent — waiting for
//!   Jarvis" until the server clears it, and what shows next is the server's answer (P2-E11).
//! - `CardStore` keeps open cards in `cards.json` (the signed items, nothing secret), so a card
//!   received before a restart is shown again after it: the gateway never re-sends an item it
//!   has a `received` for. Each stored card is re-verified against the trusted keys on load.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{DateTime, FixedOffset};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::rules::{check_authz, CoreKeys, Rejected, CLOCK_TOLERANCE};
use crate::state::write_atomic;
use crate::wire::AuthzKind;

/// The file in the node's data folder that keeps open cards.
pub const CARDS_FILE: &str = "cards.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Approve,
    Deny,
}

/// The card's time is up: nothing is sent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Expired;

/// A verified approval card.
#[derive(Debug, Clone, PartialEq)]
pub struct ApprovalCard {
    pub outbox_id: Uuid,
    pub approval_id: Uuid,
    pub summary: String,
    pub valid_until: DateTime<FixedOffset>,
    pub card_sig: String,
    /// The `outbox.item` as received, for the card store.
    item: Value,
}

impl ApprovalCard {
    /// Check a card item. Reasons, in order: `malformed`, `untrusted_key_id`,
    /// `bad_signature`, `wrong_kind`, `wrong_target`, `malformed` (no `approval_id`),
    /// `expired` (`now` > `valid_until` + 30 s).
    pub fn from_item(
        item: &Value,
        trusted: &CoreKeys,
        me: Uuid,
        now: DateTime<FixedOffset>,
    ) -> Result<Self, Rejected> {
        let outbox_id = item
            .get("outbox_id")
            .and_then(Value::as_str)
            .and_then(|s| Uuid::parse_str(s).ok())
            .ok_or(Rejected("malformed"))?;
        let authz = item.get("authz").ok_or(Rejected("malformed"))?;
        let obj = check_authz(authz, trusted)?;
        if obj.kind != AuthzKind::ApprovalCard {
            return Err(Rejected("wrong_kind"));
        }
        if obj.target_node_id != Some(me) {
            return Err(Rejected("wrong_target"));
        }
        let approval_id = obj.approval_id.ok_or(Rejected("malformed"))?;
        if now > obj.valid_until + CLOCK_TOLERANCE {
            return Err(Rejected("expired"));
        }
        let card_sig = authz["sig"]
            .as_str()
            .ok_or(Rejected("malformed"))?
            .to_owned();
        let summary = obj
            .summary
            .clone()
            .or_else(|| item.get("body").and_then(Value::as_str).map(str::to_owned))
            .unwrap_or_default();
        Ok(Self {
            outbox_id,
            approval_id,
            summary,
            valid_until: obj.valid_until,
            card_sig,
            item: item.clone(),
        })
    }

    /// Whole seconds until `valid_until`; 0 at or after it.
    pub fn seconds_left(&self, now: DateTime<FixedOffset>) -> i64 {
        let left = self.valid_until - now;
        if left <= chrono::Duration::zero() {
            return 0;
        }
        // Round up, so the countdown shows 1 until the last moment, then 0.
        let ms = left.num_milliseconds();
        (ms + 999) / 1000
    }

    /// The `approval.decision` payload, only while `now < valid_until`.
    pub fn decision(&self, d: Decision, now: DateTime<FixedOffset>) -> Result<Value, Expired> {
        if now >= self.valid_until {
            return Err(Expired);
        }
        let word = match d {
            Decision::Approve => "APPROVED",
            Decision::Deny => "DENIED",
        };
        Ok(json!({"approval_id": self.approval_id, "decision": word, "card_sig": self.card_sig}))
    }
}

/// What a spoken phrase means while cards are open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceDecision {
    NotADecision,
    Decide(Decision),
    /// A decision, but more than one card is open: ask the person to tap the one they mean.
    AskToTap,
}

/// Exactly "approve" / "approve it" or "deny" / "deny it" (after the same clean-up the
/// coordinator applies), and only with exactly one open card.
pub fn pending_voice_decision(text: &str, pending: usize) -> VoiceDecision {
    let d = match normalise(text).as_str() {
        "approve" | "approve it" => Decision::Approve,
        "deny" | "deny it" => Decision::Deny,
        _ => return VoiceDecision::NotADecision,
    };
    match pending {
        0 => VoiceDecision::NotADecision,
        1 => VoiceDecision::Decide(d),
        _ => VoiceDecision::AskToTap,
    }
}

/// The coordinator's phrase clean-up (`jarvis-core` `coordinator::normalise`, which is private
/// and stays unchanged): lower case, punctuation to spaces, a leading "hey jarvis" / "jarvis"
/// and a trailing "please" then "jarvis" dropped.
fn normalise(text: &str) -> String {
    let lowered: String = text
        .to_lowercase()
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == ' ' || c == '\'' {
                c
            } else {
                ' '
            }
        })
        .collect();
    let mut words: Vec<&str> = lowered.split_whitespace().collect();
    if words.first() == Some(&"hey") && words.get(1) == Some(&"jarvis") {
        words.drain(..2);
    } else if words.first() == Some(&"jarvis") {
        words.remove(0);
    }
    if words.last() == Some(&"please") {
        words.pop();
    }
    if words.last() == Some(&"jarvis") {
        words.pop();
    }
    words.join(" ")
}

#[derive(Debug, Clone)]
struct Entry {
    card: ApprovalCard,
    decided: bool,
}

/// Open cards, kept on disk until the server clears them.
pub struct CardStore {
    path: PathBuf,
    entries: Mutex<Vec<Entry>>,
}

impl CardStore {
    /// Open the store; cards are only read (and re-verified) by `load`.
    pub fn open(path: &Path) -> Self {
        Self {
            path: path.to_owned(),
            entries: Mutex::default(),
        }
    }

    /// The stored cards that still verify under `trusted` for `me` at `now`; the others are
    /// dropped from the file. A missing or unreadable file means no cards.
    pub fn load(
        &self,
        trusted: &CoreKeys,
        me: Uuid,
        now: DateTime<FixedOffset>,
    ) -> Vec<ApprovalCard> {
        let stored: Vec<(Value, bool)> = std::fs::read(&self.path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .and_then(|v| v.get("cards").and_then(Value::as_array).cloned())
            .unwrap_or_default()
            .into_iter()
            .filter_map(|e| Some((e.get("item")?.clone(), e.get("decided")?.as_bool()?)))
            .collect();
        let mut entries = self.entries.lock().expect("cards lock");
        entries.clear();
        for (item, decided) in stored {
            match ApprovalCard::from_item(&item, trusted, me, now) {
                Ok(card) if !entries.iter().any(|e| e.card.outbox_id == card.outbox_id) => {
                    entries.push(Entry { card, decided })
                }
                Ok(_) => {}
                Err(Rejected(reason)) => tracing::info!(reason, "stored approval card dropped"),
            }
        }
        if let Err(e) = self.save(&entries) {
            tracing::warn!(error = %e, "couldn't rewrite the card store");
        }
        entries.iter().map(|e| e.card.clone()).collect()
    }

    /// Keep a card (replacing one with the same `outbox_id`).
    pub fn add(&self, card: ApprovalCard) -> io::Result<()> {
        let mut entries = self.entries.lock().expect("cards lock");
        entries.retain(|e| e.card.outbox_id != card.outbox_id);
        entries.push(Entry {
            card,
            decided: false,
        });
        self.save(&entries)
    }

    /// Forget a card; whether it was there.
    pub fn remove(&self, outbox_id: Uuid) -> io::Result<bool> {
        let mut entries = self.entries.lock().expect("cards lock");
        let before = entries.len();
        entries.retain(|e| e.card.outbox_id != outbox_id);
        if entries.len() == before {
            return Ok(false);
        }
        self.save(&entries).map(|()| true)
    }

    pub fn contains(&self, outbox_id: Uuid) -> bool {
        self.entries
            .lock()
            .expect("cards lock")
            .iter()
            .any(|e| e.card.outbox_id == outbox_id)
    }

    /// Claim a card for one decision: None if it is unknown or already decided. Undo with
    /// `undo_decide` if the decision couldn't be sent.
    pub fn begin_decide(&self, outbox_id: Uuid) -> Option<ApprovalCard> {
        let mut entries = self.entries.lock().expect("cards lock");
        let entry = entries
            .iter_mut()
            .find(|e| e.card.outbox_id == outbox_id && !e.decided)?;
        entry.decided = true;
        let card = entry.card.clone();
        if let Err(e) = self.save(&entries) {
            tracing::warn!(error = %e, "couldn't record the decision in the card store");
        }
        Some(card)
    }

    pub fn undo_decide(&self, outbox_id: Uuid) {
        let mut entries = self.entries.lock().expect("cards lock");
        if let Some(e) = entries.iter_mut().find(|e| e.card.outbox_id == outbox_id) {
            e.decided = false;
            let _ = self.save(&entries);
        }
    }

    /// Open cards not yet decided (what a voice decision would apply to).
    pub fn pending(&self) -> usize {
        self.entries
            .lock()
            .expect("cards lock")
            .iter()
            .filter(|e| !e.decided)
            .count()
    }

    /// The only undecided card, if exactly one is open.
    pub fn only_pending(&self) -> Option<Uuid> {
        let entries = self.entries.lock().expect("cards lock");
        let mut open = entries.iter().filter(|e| !e.decided);
        match (open.next(), open.next()) {
            (Some(e), None) => Some(e.card.outbox_id),
            _ => None,
        }
    }

    fn save(&self, entries: &[Entry]) -> io::Result<()> {
        let cards: Vec<Value> = entries
            .iter()
            .map(|e| json!({"item": e.card.item, "decided": e.decided}))
            .collect();
        let bytes = serde_json::to_vec(&json!({"v": 1, "cards": cards}))?;
        write_atomic(&self.path, &bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::normalise;

    #[test]
    fn normalise_matches_the_coordinators_rules() {
        for (said, want) in [
            ("Hey Jarvis, approve it please!", "approve it"),
            ("Jarvis deny", "deny"),
            ("deny it, Jarvis", "deny it"),
            ("APPROVE.", "approve"),
            ("approve please jarvis", "approve please"), // "please" is checked first, as in jarvis-core
            ("jarvis's news", "jarvis's news"),
        ] {
            assert_eq!(normalise(said), want, "{said}");
        }
    }
}
