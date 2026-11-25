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
//! - `reply.chunk` and `node.action` carry `in_reply_to` (R12-37, unsigned): the utterance
//!   the reply answers, or null; it is passed on with each `ReplyEvent`.
//! - `node.action {authz, seq, in_reply_to}`: rules 2 to 4 of §7.1 (`check_node_action` under the trusted
//!   core keys), and it must be a live action (a `reply_id`, no `execution_key`: approved
//!   actions come in outbox items). `seq` is the chunk's, unsigned (R12-36), and only names
//!   the action in its live receipt. Accepted ones are broadcast. A core signature under a key this node doesn't trust asks
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

use crate::approval::{ApprovalCard, CardStore, Decision, CARDS_FILE};
use crate::client::{self, Client, ClientConfig, EnrolError, LinkStatus, NotConnected};
use crate::delivery::{Inbox, ItemFate};
use crate::folder::{Executor, FolderOps, Recovered, LISTING};
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
    /// Show this approval card (Task 7). Decide with `NodeHandle::decide`; it stays until
    /// `ApprovalCleared` for its `outbox_id`.
    ApprovalCard(ApprovalCard),
    /// The server cleared this card (decided, superseded or expired).
    ApprovalCleared {
        outbox_id: Uuid,
        reason: String,
    },
}

/// Why `NodeHandle::decide` sent nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum DecideError {
    #[error("the card has expired")]
    Expired,
    #[error("no such open card (cleared, already decided, or never shown)")]
    Gone,
    #[error("not connected to the Jarvis server")]
    NotConnected,
}

/// A verified piece of a reply, for `ServerBrain`.
#[derive(Debug, Clone, PartialEq)]
pub enum ReplyEvent {
    Speech {
        reply_id: Uuid,
        seq: i64,
        text: String,
        final_: bool,
        /// The utterance (envelope `message_id`) this reply answers; None if none (R12-37).
        in_reply_to: Option<Uuid>,
    },
    /// A live action that passed §7.1 rules 2 to 4; `seq` (unsigned, R12-36) is what its
    /// `action.receipt` names, with `authz.reply_id`.
    Action {
        seq: i64,
        in_reply_to: Option<Uuid>,
        authz: AuthzObject,
    },
}

pub struct NodeDeps {
    pub keystore: Arc<dyn KeyStore>,
    pub ui: mpsc::Sender<UiEvent>,
    /// The test folder (Task 8): `folder::windows` on the desktop, `folder::NoFolder` if none
    /// is configured.
    pub folder: Arc<dyn FolderOps>,
}

/// The app's handle on the node. Cheap to clone.
#[derive(Clone)]
pub struct NodeHandle {
    client: Client,
    replies: broadcast::Sender<ReplyEvent>,
    inbox: Inbox,
    cards: Arc<CardStore>,
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

    /// As `send`, returning the envelope's `message_id` (which replies name, R12-37).
    pub async fn send_tracked(&self, kind: &str, payload: Value) -> Result<Uuid, NotConnected> {
        self.client.send_tracked(kind, payload).await
    }

    pub async fn enrol(&self, code: &str) -> Result<(), EnrolError> {
        self.client.enrol(code).await
    }

    /// Send the person's decision on an open card (Task 7): `approval.decision`, then `ack`
    /// (journalled, like `shown`). Sent once: the card then waits for the server to clear it,
    /// and the server's answer is what shows next (P2-E11). Nothing is sent once
    /// `valid_until` has passed.
    pub async fn decide(&self, outbox_id: Uuid, d: Decision) -> Result<(), DecideError> {
        let card = self
            .cards
            .begin_decide(outbox_id)
            .ok_or(DecideError::Gone)?;
        let now = chrono::Utc::now().fixed_offset();
        let body = match card.decision(d, now) {
            Ok(body) => body,
            Err(_) => {
                self.cards.undo_decide(outbox_id);
                return Err(DecideError::Expired);
            }
        };
        if self.client.send("approval.decision", body).await.is_err() {
            self.cards.undo_decide(outbox_id);
            return Err(DecideError::NotConnected);
        }
        tracing::info!(%outbox_id, approval_id = %card.approval_id, ?d, "approval decision sent");
        if let Err(e) = self.inbox.mark_acked(outbox_id, now) {
            tracing::error!(error = %e, "couldn't journal the ack; sending it once");
        }
        let _ = self
            .client
            .send("ack", json!({"outbox_id": outbox_id}))
            .await;
        Ok(())
    }

    /// Open cards not yet decided.
    pub fn pending_cards(&self) -> usize {
        self.cards.pending()
    }

    /// The only undecided card, for a spoken "approve" or "deny".
    pub fn only_pending_card(&self) -> Option<Uuid> {
        self.cards.only_pending()
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
    let journal = Arc::new(Mutex::new(Journal::open(&cfg.data_dir.join(JOURNAL_FILE))?));
    let cfg_cards = cfg.data_dir.join(CARDS_FILE);
    let inbox = Inbox::new(journal.clone());
    // Before the first connection: finish or report whatever a crash interrupted (§9.4).
    let executor = Arc::new(Executor::new(deps.folder, journal));
    let recovered = executor.recover();
    let (frames_tx, frames) = mpsc::unbounded_channel();
    let client = client::spawn(cfg, deps.keystore, frames_tx);
    let (replies, _) = broadcast::channel(256);
    let cards = Arc::new(CardStore::open(&cfg_cards));
    let router = Router {
        inbox: inbox.clone(),
        client: client.clone(),
        replies: replies.clone(),
        ui: deps.ui,
        cards: cards.clone(),
        executor,
    };
    tokio::spawn(forward_status(client.status(), router.ui.clone()));
    tokio::spawn(router.run(frames, recovered));
    Ok(NodeHandle {
        client,
        replies,
        inbox,
        cards,
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
    cards: Arc<CardStore>,
    executor: Arc<Executor>,
}

/// `in_reply_to` (R12-37): must be present, a UUID or null. `Some(None)` is null.
fn answers(p: &Map<String, Value>) -> Option<Option<Uuid>> {
    match p.get("in_reply_to")? {
        Value::Null => Some(None),
        Value::String(s) => Uuid::parse_str(s).ok().map(Some),
        _ => None,
    }
}

fn uuid_field(map: &Map<String, Value>, key: &str) -> Option<Uuid> {
    Uuid::parse_str(map.get(key)?.as_str()?).ok()
}

impl Router {
    async fn run(self, mut frames: mpsc::UnboundedReceiver<Output>, recovered: Vec<Recovered>) {
        let mut cards_loaded = false;
        let mut recovered = Some(recovered);
        while let Some(output) = frames.recv().await {
            match output {
                Output::Ready { summary, .. } => {
                    if !cards_loaded {
                        // Cards kept from before a restart, re-verified under today's keys.
                        cards_loaded = true;
                        let state = self.client.state().borrow().clone();
                        if let Some(me) = state.node_id {
                            let now = chrono::Utc::now().fixed_offset();
                            for card in self.cards.load(&state.trust.core_keys(now), me, now) {
                                let _ = self.ui.send(UiEvent::ApprovalCard(card)).await;
                            }
                        }
                    }
                    // Receipts first: those recovered at start, then any not yet known to have
                    // arrived (the gateway won't re-send an item it has a `received` for).
                    let owed = match recovered.take() {
                        Some(at_start) => at_start,
                        None => self.executor.owed_receipts(),
                    };
                    for r in owed {
                        send_receipt(&self.client, &self.inbox, r.receipt, r.outbox_id).await;
                    }
                    // Owed acks next: the gateway won't re-send those items.
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
            if p["authz"]["object"]["kind"] == "approval.card" {
                return self.on_card(p, outbox_id).await;
            }
            return self.on_approved_action(p, outbox_id).await;
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

    /// An approved action item (Task 8): checked, run off the routing path, then `received`,
    /// the receipt and the ack. `received` comes after STARTED is journalled, so a crash before
    /// it leaves the gateway offering the item again, and the journal keeps it from running
    /// twice.
    async fn on_approved_action(&self, p: &Map<String, Value>, outbox_id: Uuid) {
        let state = self.client.state().borrow().clone();
        let Some(me) = state.node_id else { return };
        let now = chrono::Utc::now().fixed_offset();
        let obj = match check_node_action(&p["authz"], &state.trust.core_keys(now), me, now) {
            Err(Rejected("untrusted_key_id")) => {
                // R12-29: no `received`, so it is offered again after re-enrolment.
                Self::refused("outbox.item", "untrusted_key_id");
                return self.client.require_reenrol();
            }
            Err(Rejected(reason)) => {
                // Never runnable here: confirm transport so it isn't offered again.
                let _ = self
                    .client
                    .send("received", json!({"outbox_id": outbox_id}))
                    .await;
                return Self::refused("action.approved", reason);
            }
            Ok(obj) => obj,
        };
        let (client, inbox, executor) = (
            self.client.clone(),
            self.inbox.clone(),
            self.executor.clone(),
        );
        tokio::spawn(async move {
            let receipt =
                tokio::task::spawn_blocking(move || executor.delete(&obj, Some(outbox_id)))
                    .await
                    .unwrap_or_else(|e| {
                        tracing::error!(error = %e, "the delete task failed");
                        None
                    });
            let _ = client
                .send("received", json!({"outbox_id": outbox_id}))
                .await;
            if let Some(receipt) = receipt {
                send_receipt(&client, &inbox, receipt, Some(outbox_id)).await;
            }
        });
    }

    /// An approval card item (Task 7).
    async fn on_card(&self, p: &Map<String, Value>, outbox_id: Uuid) {
        let state = self.client.state().borrow().clone();
        let Some(me) = state.node_id else { return };
        let now = chrono::Utc::now().fixed_offset();
        let item = Value::Object(p.clone());
        let checked = ApprovalCard::from_item(&item, &state.trust.core_keys(now), me, now);
        if let Err(Rejected("untrusted_key_id")) = checked {
            // A core key this node doesn't trust (R12-29): no `received`, so it is offered
            // again once the node has re-enrolled.
            Self::refused("outbox.item", "untrusted_key_id");
            return self.client.require_reenrol();
        }
        let _ = self
            .client
            .send("received", json!({"outbox_id": outbox_id}))
            .await;
        let card = match checked {
            Ok(card) => card,
            Err(Rejected(reason)) => return Self::refused("approval.card", reason),
        };
        // Stored before the seen-list, so a crash between the two can't lose the card.
        let was_stored = self.cards.contains(outbox_id);
        if let Err(e) = self.cards.add(card.clone()) {
            tracing::error!(error = %e, "couldn't keep the approval card; it won't survive a restart");
        }
        match self.inbox.on_item(outbox_id) {
            Ok(ItemFate::Show) => {
                let _ = self.ui.send(UiEvent::ApprovalCard(card)).await;
            }
            Ok(ItemFate::AlreadySeen) if was_stored => {} // a re-send; still on screen
            Ok(ItemFate::AlreadySeen | ItemFate::Cleared) => {
                let _ = self.cards.remove(outbox_id);
                if self.inbox.ack_owed(outbox_id, now) {
                    let _ = self
                        .client
                        .send("ack", json!({"outbox_id": outbox_id}))
                        .await;
                }
            }
            Err(e) => {
                let _ = self.cards.remove(outbox_id);
                tracing::error!(error = %e, "journal write failed; the card is not shown");
            }
        }
    }

    async fn on_cleared(&self, p: &Map<String, Value>) {
        let (Some(outbox_id), Some(reason)) = (
            uuid_field(p, "outbox_id"),
            p.get("reason").and_then(|r| r.as_str()),
        ) else {
            return Self::refused("outbox.cleared", "malformed");
        };
        let was_card = match self.cards.remove(outbox_id) {
            Ok(was) => was,
            Err(e) => {
                tracing::error!(error = %e, "couldn't update the card store");
                true
            }
        };
        match self.inbox.on_cleared(outbox_id) {
            Ok(_) if was_card => {
                let _ = self
                    .ui
                    .send(UiEvent::ApprovalCleared {
                        outbox_id,
                        reason: reason.to_owned(),
                    })
                    .await;
            }
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
        let (Some(reply_id), Some(seq), Some(in_reply_to)) = (reply_id, seq, answers(p)) else {
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
                    in_reply_to,
                });
            }
            // Live actions come as node.action frames (G4.2), never as ACTION chunks.
            _ => Self::refused("reply.chunk", "not_a_speech_chunk"),
        }
    }

    async fn on_action(&self, p: &Map<String, Value>) {
        let (Some(authz), Some(seq), Some(in_reply_to), 3) = (
            p.get("authz"),
            p.get("seq").and_then(Value::as_i64).filter(|s| *s >= 1),
            answers(p),
            p.len(),
        ) else {
            return Self::refused("node.action", "malformed");
        };
        let state = self.client.state().borrow().clone();
        let Some(me) = state.node_id else { return };
        let now = chrono::Utc::now().fixed_offset();
        match check_node_action(authz, &state.trust.core_keys(now), me, now) {
            Ok(obj) if obj.reply_id.is_none() || obj.execution_key.is_some() => {
                Self::refused("node.action", "not_live");
            }
            Ok(obj) if obj.action_kind == LISTING => {
                // The live listing core asked for (§9.3 step 2), off the routing path.
                let reply_id = obj.reply_id.expect("checked live above");
                let (client, executor) = (self.client.clone(), self.executor.clone());
                tokio::spawn(async move {
                    let receipt =
                        tokio::task::spawn_blocking(move || executor.list_receipt(reply_id, seq))
                            .await;
                    match receipt {
                        Ok(receipt) => {
                            if client.send("action.receipt", receipt).await.is_err() {
                                tracing::warn!(%reply_id, "listing receipt not sent: not connected");
                            }
                        }
                        Err(e) => tracing::error!(error = %e, "the listing task failed"),
                    }
                });
            }
            Ok(obj) if obj.action_kind.starts_with("pc.") => {
                // No other live pc.* action exists: answer it, run nothing.
                let receipt = json!({
                    "reply_id": obj.reply_id, "seq": seq, "result": "REFUSED",
                    "evidence": {"check": "unknown_action_kind"},
                });
                let _ = self.client.send("action.receipt", receipt).await;
            }
            Ok(obj) => {
                let _ = self.replies.send(ReplyEvent::Action {
                    seq,
                    in_reply_to,
                    authz: obj,
                });
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

/// Send a delete receipt, then ack its item (journalled first, like `shown`).
async fn send_receipt(client: &Client, inbox: &Inbox, receipt: Value, outbox_id: Option<Uuid>) {
    if client.send("action.receipt", receipt).await.is_err() {
        tracing::warn!("receipt not sent: not connected; it is re-sent after reconnecting");
        return;
    }
    if let Some(outbox_id) = outbox_id {
        let now = chrono::Utc::now().fixed_offset();
        if let Err(e) = inbox.mark_acked(outbox_id, now) {
            tracing::error!(error = %e, "couldn't journal the ack; sending it once");
        }
        let _ = client.send("ack", json!({"outbox_id": outbox_id})).await;
    }
}
