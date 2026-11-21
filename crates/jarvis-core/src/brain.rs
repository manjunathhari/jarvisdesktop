//! The swappable brain (ADR-002). Claude today; a local model later.

use crate::action::{Action, ActionSpec};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Turn {
    pub role: Role,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BrainRequest {
    pub utterance: String,
    pub screen_summary: String,
    pub history: Vec<Turn>,
    pub tools: Vec<ActionSpec>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum BrainEvent {
    /// A piece of the spoken reply (a sentence or so), streamed as it arrives.
    Speech(String),
    /// An action the brain wants taken.
    Action(Action),
}

#[derive(Debug, thiserror::Error)]
pub enum BrainError {
    #[error("brain unavailable: {0}")]
    Unavailable(String),
    #[error("brain returned something unreadable: {0}")]
    Protocol(String),
}

#[async_trait]
pub trait Brain: Send + Sync {
    /// Stream events into `tx`; return when the reply is complete.
    async fn think(
        &self,
        req: BrainRequest,
        tx: mpsc::Sender<BrainEvent>,
    ) -> Result<(), BrainError>;
}
