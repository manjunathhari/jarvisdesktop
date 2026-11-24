//! The node runtime: the connection driver plus the routing of every verified gateway message
//! (spec §8.2, §8.3, §13).
//!
//! - `outbox.item`: `received`, then shown once per `outbox_id` (the journal's seen-list).
//!   SILENT items are history only and acked at once; others become a `Notice` and are acked
//!   when the app says they were shown (`NodeHandle::shown`). Every ack is journalled first and
//!   re-sent after each reconnect until the server clears the item here (or for a week): once
//!   an item is `received`, the gateway never re-sends it, so an ack lost to a crash or a
//!   dropped link would otherwise never arrive. Items carrying an `authz` (approval cards,
//!   approved actions) get no `received` until Tasks 7 and 8 handle them, so the gateway's
//!   sweep keeps offering them instead of marking them delivered.
//! - `outbox.cleared`: recorded, so the item never shows afterwards; the app is told.
//! - `reply.chunk` (SPEECH): `received` with its `seq`, then broadcast to `subscribe_replies`.
//!   Live actions arrive as `node.action` frames, never as ACTION chunks (Phase 2 ruling G4.2),
//!   so an ACTION chunk is refused.
//! - `node.action`: rules 2 to 4 of §7.1 (`check_node_action` under the trusted core keys);
//!   accepted ones are broadcast. A core signature under a key this node doesn't trust asks
//!   for re-enrolment (R12-29). Rule 5 and execution are Task 8's.
//! - `welcome` summary: shown as lines, display only, so a summary line never marks an
//!   approval card seen. The gateway sends the summary (every open item) and then, as
//!   `outbox.item`, the items never sent to this node, so one id can appear in both: the app
//!   collapses a summary entry when a `Notice` for one of its `outbox_ids` arrives, and may
//!   call `shown` for summary ids it displayed.
//! - A `node.action` under a core `key_id` this node doesn't trust asks for re-enrolment even
//!   before its signature is checked (R12-29). That needs a valid gateway envelope, so only a
//!   faulty gateway, not an outsider, can cause it.
//! - Reply chunks are `received` before they are broadcast; a subscriber that lags behind the
//!   broadcast buffer (256) loses chunks for good, so `ServerBrain` treats `Lagged` as a
//!   failed reply.

use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};
use tokio::sync::{broadcast, mpsc, watch};
use uuid::Uuid;

use crate::client::{self, Client, ClientConfig, EnrolError, LinkStatus, NotConnected};
use crate::delivery::{Inbox, ItemFate};
use crate::journal::{Journal, JournalError};
use crate::keystore::KeyStore;
use crate::rules::{check_node_action, Rejected};
use crate::session::Output;
use crate::state::NodeState;
use crate::wire::AuthzObject;

/// The journal's file inside the data folder.
pub const JOURNAL_FILE: &str = "journal.jsonl";

/// What the app should show or change.
#[derive(Debug, Clone, PartialEq)]
pub enum UiEvent {
    /// Show this outbox item; call `NodeHandle::shown` once it is on screen or spoken.
    Notice {
        outbox_id: Uuid,
        priority: String,
        kind: String,
        body: String,
    },
    /// The server cleared an item (acknowledged elsewhere, superseded or expired).
    NoticeCleared {
        outbox_id: Uuid,
        reason: String,
    },
    /// The reconnect summary groups (§8.3), in the server's order; display only.
    Summary(Vec<Value>),
    Link(LinkStatus),
    /// This device's clock differs from the server's by this many milliseconds.
    ClockOff(i64),
}

/// A verified piece of a reply, for `ServerBrain`.
#[derive(Debug, Clone, PartialEq)]
pub enum ReplyEvent {
    Speech {
        reply_id: Uuid,
        seq: i64,
        text: String,
        final_: bool,
    },
    /// A live action that passed §7.1 rules 2 to 4.
    Action(AuthzObject),
}

pub struct NodeDeps {
    pub keystore: Arc<dyn KeyStore>,
    pub ui: mpsc::Sender<UiEvent>,
}

/// The app's handle on the node. Cheap to clone.
#[derive(Clone)]
pub struct NodeHandle {
    client: Client,
    replies: broadcast::Sender<ReplyEvent>,
    inbox: Inbox,
}

impl NodeHandle {
    pub fn status(&self) -> watch::Receiver<LinkStatus> {
        self.client.status()
    }

    pub fn state(&self) -> watch::Receiver<NodeState> {
        self.client.state()
    }

    pub async fn send(&self, kind: &str, payload: Value) -> Result<(), NotConnected> {
        self.client.send(kind, payload).await
    }

    pub async fn enrol(&self, code: &str) -> Result<(), EnrolError> {
        self.client.enrol(code).await
    }

    /// The app showed (or spoke) this item: acknowledge it (§8.2). The ack is journalled
    /// first, so it is re-sent after a reconnect if it doesn't reach the server now; a
    /// `NotConnected` here therefore loses nothing.
    pub async fn shown(&self, outbox_id: Uuid) -> Result<(), NotConnected> {
        let now = chrono::Utc::now().fixed_offset();
        if let Err(e) = self.inbox.mark_acked(outbox_id, now) {
            tracing::error!(error = %e, "couldn't journal the ack; sending it once");
        }
        self.client
            .send("ack", json!({"outbox_id": outbox_id}))
            .await
    }

    /// Every verified reply chunk and live action from now on.
    pub fn subscribe_replies(&self) -> broadcast::Receiver<ReplyEvent> {
        self.replies.subscribe()
    }
}

/// Open the journal and start the node on the current tokio runtime.
pub fn start(cfg: ClientConfig, deps: NodeDeps) -> Result<NodeHandle, JournalError> {
    let journal = Journal::open(&cfg.data_dir.join(JOURNAL_FILE))?;
    let inbox = Inbox::new(Arc::new(Mutex::new(journal)));
    let (frames_tx, frames) = mpsc::unbounded_channel();
    let client = client::spawn(cfg, deps.keystore, frames_tx);
    let (replies, _) = broadcast::channel(256);
    let router = Router {
        inbox: inbox.clone(),
        client: client.clone(),
        replies: replies.clone(),
        ui: deps.ui,
    };
    tokio::spawn(forward_status(client.status(), router.ui.clone()));
    tokio::spawn(router.run(frames));
    Ok(NodeHandle {
        client,
        replies,
        inbox,
    })
}

async fn forward_status(mut status: watch::Receiver<LinkStatus>, ui: mpsc::Sender<UiEvent>) {
    loop {
        let now = status.borrow_and_update().clone();
        if ui.send(UiEvent::Link(now)).await.is_err() || status.changed().await.is_err() {
            return;
        }
    }
}

struct Router {
    inbox: Inbox,
    client: Client,
    replies: broadcast::Sender<ReplyEvent>,
    ui: mpsc::Sender<UiEvent>,
}

fn uuid_field(map: &Map<String, Value>, key: &str) -> Option<Uuid> {
    Uuid::parse_str(map.get(key)?.as_str()?).ok()
}

impl Router {
    async fn run(self, mut frames: mpsc::UnboundedReceiver<Output>) {
        while let Some(output) = frames.recv().await {
            match output {
                Output::Ready { summary, .. } => {
                    // Owed acks first: the gateway won't re-send those items.
                    let now = chrono::Utc::now().fixed_offset();
                    for outbox_id in self.inbox.owed_acks(now) {
                        let _ = self
                            .client
                            .send("ack", json!({"outbox_id": outbox_id}))
                            .await;
                    }
                    let _ = self.ui.send(UiEvent::Summary(summary)).await;
                }
                Output::ClockOff(ms) => {
                    let _ = self.ui.send(UiEvent::ClockOff(ms)).await;
                }
                Output::Frame { kind, payload } => {
                    let Value::Object(payload) = payload else {
                        continue;
                    };
                    match kind.as_str() {
                        "outbox.item" => self.on_item(&payload).await,
                        "outbox.cleared" => self.on_cleared(&payload).await,
                        "reply.chunk" => self.on_chunk(&payload).await,
                        "node.action" => self.on_action(&payload).await,
                        "error" => tracing::warn!(?payload, "the gateway reported an error"),
                        other => tracing::warn!(kind = other, "unhandled gateway message"),
                    }
                }
                // Handled by the driver.
                Output::Send(_) | Output::TrustChanged(_) | Output::Refused { .. } => {}
            }
        }
    }

    fn refused(kind: &str, reason: &str) {
        tracing::warn!(kind, reason, "refused a gateway message");
    }

    async fn on_item(&self, p: &Map<String, Value>) {
        let Some(outbox_id) = uuid_field(p, "outbox_id") else {
            return Self::refused("outbox.item", "malformed");
        };
        let field = |k: &str| p.get(k).and_then(|v| v.as_str()).map(str::to_owned);
        let (Some(priority), Some(kind), Some(body)) =
            (field("priority"), field("kind"), field("body"))
        else {
            return Self::refused("outbox.item", "malformed");
        };
        if p.get("authz").is_some_and(|a| !a.is_null()) {
            // Approval cards and approved actions: Tasks 7 and 8. No `received`, so the
            // gateway keeps offering them; not marked seen.
            return Self::refused("outbox.item", "authz_not_handled_yet");
        }
        // The transport confirmation comes first, whatever happens next (§8.2).
        let _ = self
            .client
            .send("received", json!({"outbox_id": outbox_id}))
            .await;
        let now = chrono::Utc::now().fixed_offset();
        match self.inbox.on_item(outbox_id) {
            Ok(ItemFate::Show) if priority == "SILENT" => {
                // History only: never shown, acknowledged at once (durably).
                if let Err(e) = self.inbox.mark_acked(outbox_id, now) {
                    tracing::error!(error = %e, "couldn't journal the ack");
                }
                let _ = self
                    .client
                    .send("ack", json!({"outbox_id": outbox_id}))
                    .await;
            }
            Ok(ItemFate::AlreadySeen) if self.inbox.ack_owed(outbox_id, now) => {
                // A re-send of an item we acked: the ack may not have arrived.
                let _ = self
                    .client
                    .send("ack", json!({"outbox_id": outbox_id}))
                    .await;
            }
            Ok(ItemFate::Show) => {
                let _ = self
                    .ui
                    .send(UiEvent::Notice {
                        outbox_id,
                        priority,
                        kind,
                        body,
                    })
                    .await;
            }
            Ok(ItemFate::AlreadySeen | ItemFate::Cleared) => {}
            Err(e) => tracing::error!(error = %e, "journal write failed; the item is not shown"),
        }
    }

    async fn on_cleared(&self, p: &Map<String, Value>) {
        let (Some(outbox_id), Some(reason)) = (
            uuid_field(p, "outbox_id"),
            p.get("reason").and_then(|r| r.as_str()),
        ) else {
            return Self::refused("outbox.cleared", "malformed");
        };
        match self.inbox.on_cleared(outbox_id) {
            Ok(_) => {
                let _ = self
                    .ui
                    .send(UiEvent::NoticeCleared {
                        outbox_id,
                        reason: reason.to_owned(),
                    })
                    .await;
            }
            Err(e) => tracing::error!(error = %e, "journal write failed for outbox.cleared"),
        }
    }

    async fn on_chunk(&self, p: &Map<String, Value>) {
        let reply_id = uuid_field(p, "reply_id");
        let seq = p.get("seq").and_then(|s| s.as_i64()).filter(|s| *s >= 1);
        let (Some(reply_id), Some(seq)) = (reply_id, seq) else {
            return Self::refused("reply.chunk", "malformed");
        };
        let _ = self
            .client
            .send("received", json!({"reply_id": reply_id, "seq": seq}))
            .await;
        match (
            p.get("type").and_then(|t| t.as_str()),
            p.get("text").and_then(|t| t.as_str()),
            p.get("final").and_then(|f| f.as_bool()),
        ) {
            (Some("SPEECH"), Some(text), Some(final_)) if p.get("authz") == Some(&Value::Null) => {
                let _ = self.replies.send(ReplyEvent::Speech {
                    reply_id,
                    seq,
                    text: text.to_owned(),
                    final_,
                });
            }
            // Live actions come as node.action frames (G4.2), never as ACTION chunks.
            _ => Self::refused("reply.chunk", "not_a_speech_chunk"),
        }
    }

    async fn on_action(&self, p: &Map<String, Value>) {
        let (Some(authz), 1) = (p.get("authz"), p.len()) else {
            return Self::refused("node.action", "malformed");
        };
        let state = self.client.state().borrow().clone();
        let Some(me) = state.node_id else { return };
        let now = chrono::Utc::now().fixed_offset();
        match check_node_action(authz, &state.trust.core_keys(now), me, now) {
            Ok(obj) => {
                let _ = self.replies.send(ReplyEvent::Action(obj));
            }
            Err(Rejected("untrusted_key_id")) => {
                // A core key_id this node never pinned or no longer trusts (R12-29).
                Self::refused("node.action", "untrusted_key_id");
                self.client.require_reenrol();
            }
            Err(Rejected(reason)) => Self::refused("node.action", reason),
        }
    }
}
