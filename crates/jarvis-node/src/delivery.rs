//! The outbox seen-list (spec §8.2, P2-E04, P2-E05, P2-E13): each `outbox_id` is shown at most
//! once on this node, even across restarts, and never after the server cleared it.
//!
//! The decision is made from the durable journal, and `seen` is journalled (and fsync'd)
//! before `Show` is returned, so a crash after showing can never show the item again.

use std::sync::{Arc, Mutex};

use chrono::{DateTime, Duration, FixedOffset};
use uuid::Uuid;

use crate::journal::{Journal, JournalError, Record};

/// What to do with an arriving outbox item.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ItemFate {
    /// First time here: show it (it is now recorded as seen).
    Show,
    /// Already shown on this node: show nothing.
    AlreadySeen,
    /// The server already cleared it: show nothing.
    Cleared,
}

/// How long an owed `ack` keeps being re-sent after reconnects.
pub const ACK_RETRY_WINDOW: Duration = Duration::days(7);

/// The seen-list over the shared journal.
#[derive(Clone)]
pub struct Inbox {
    journal: Arc<Mutex<Journal>>,
}

impl Inbox {
    pub fn new(journal: Arc<Mutex<Journal>>) -> Self {
        Self { journal }
    }

    /// Decide an item's fate; a new item is journalled as seen first.
    pub fn on_item(&self, outbox_id: Uuid) -> Result<ItemFate, JournalError> {
        let mut journal = self.journal.lock().expect("journal lock");
        let state = journal.state();
        if state.cleared.contains(&outbox_id) {
            return Ok(ItemFate::Cleared);
        }
        if state.seen.contains(&outbox_id) {
            return Ok(ItemFate::AlreadySeen);
        }
        journal.append(&Record::Seen { outbox_id })?;
        Ok(ItemFate::Show)
    }

    /// Record that the person saw this item, before its `ack` is sent, so the ack is owed
    /// durably (a crash or a dropped link before it reaches the server can't lose it).
    pub fn mark_acked(
        &self,
        outbox_id: Uuid,
        now: DateTime<FixedOffset>,
    ) -> Result<(), JournalError> {
        let mut journal = self.journal.lock().expect("journal lock");
        if journal.state().acked.contains_key(&outbox_id) {
            return Ok(());
        }
        journal.append(&Record::Acked { outbox_id, at: now })
    }

    /// Whether this item's ack is owed (recorded, not cleared here, within the retry window).
    pub fn ack_owed(&self, outbox_id: Uuid, now: DateTime<FixedOffset>) -> bool {
        let journal = self.journal.lock().expect("journal lock");
        let state = journal.state();
        !state.cleared.contains(&outbox_id)
            && state
                .acked
                .get(&outbox_id)
                .is_some_and(|at| now - *at <= ACK_RETRY_WINDOW)
    }

    /// Every ack still owed at `now`, oldest first, to re-send after a reconnect.
    pub fn owed_acks(&self, now: DateTime<FixedOffset>) -> Vec<Uuid> {
        let journal = self.journal.lock().expect("journal lock");
        let state = journal.state();
        let mut owed: Vec<(DateTime<FixedOffset>, Uuid)> = state
            .acked
            .iter()
            .filter(|(id, at)| !state.cleared.contains(id) && now - **at <= ACK_RETRY_WINDOW)
            .map(|(id, at)| (*at, *id))
            .collect();
        owed.sort();
        owed.into_iter().map(|(_, id)| id).collect()
    }

    /// Record that the server cleared an item; returns whether it had been shown here.
    pub fn on_cleared(&self, outbox_id: Uuid) -> Result<bool, JournalError> {
        let mut journal = self.journal.lock().expect("journal lock");
        let shown = journal.state().seen.contains(&outbox_id);
        if !journal.state().cleared.contains(&outbox_id) {
            journal.append(&Record::Cleared { outbox_id })?;
        }
        Ok(shown)
    }
}
