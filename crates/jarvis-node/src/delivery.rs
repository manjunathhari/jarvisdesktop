//! The outbox seen-list (spec §8.2, P2-E04, P2-E05, P2-E13): each `outbox_id` is shown at most
//! once on this node, even across restarts, and never after the server cleared it.
//!
//! The decision is made from the durable journal, and `seen` is journalled (and fsync'd)
//! before `Show` is returned, so a crash after showing can never show the item again.

use std::sync::{Arc, Mutex};

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
