//! `ServerBrain`: the desktop's existing `Brain` trait, answered by the Jarvis server through
//! the node link (spec §13). The rest of the desktop (panels, voice, modules) is unchanged.
//!
//! - `think` sends `utterance {text, screen_summary}` and nothing else: the server owns the
//!   history and the tools.
//! - It follows the reply that answers that utterance: the first whose `in_reply_to` is the
//!   utterance envelope's `message_id` (R12-37); every other reply (another device's, core's
//!   own `speak()` results, re-sent old ones) is ignored. The first event seen may be past
//!   seq 1 (a dropped ACTION chunk). Within it, an event at or below the last `seq` seen is a
//!   re-send (a lost `received`, a gateway resync) and is dropped: never spoken or run twice.
//!   `in_reply_to` is unsigned, so a faulty gateway could only hide the answer, or point the
//!   node at another reply of the same person (its speech and its core-signed actions for this
//!   node); nothing unsigned authorises an action. Replies that answer no current utterance
//!   (null: core's `speak()` results) are not voiced here.
//! - SPEECH text is joined into whole sentences ([`Sentences`]), each becoming one
//!   `BrainEvent::Speech`; what is left goes out before an action and at `final`, which ends
//!   the turn.
//! - A live `module.*` / `screen.*` action (args `{tool, input}`) whose tool `SERVER_TOOLS`
//!   maps to that kind becomes `BrainEvent::Action`, then the receipt `OK` (D-2: OK means the
//!   action was verified and handed to the coordinator, **not** that the panel rendered).
//!   Anything else gets `REFUSED` with the failed check. `pc.*` actions are the executor's
//!   (Task 8), so they get nothing here. Actions of a reply that isn't followed are never run
//!   and get no receipt (they are read-only; core expires them). A receipt that can't be sent
//!   because the link is down is logged, not retried.
//! - No event of the followed reply for `SILENCE` fails the turn, as does losing events to a
//!   lagging broadcast (they can't be recovered), even when the lost ones were another reply's.
//! - The desktop runtime has two limits, both measured from the send: the first event
//!   (speech or action) must come within 8 s (`jarvis-core` `BRAIN_FIRST_EVENT_TIMEOUT`), and
//!   the whole `think` must return within 30 s (`BRAIN_TIMEOUT`), so in the app a reply whose
//!   `final` comes later than 30 s after the send is cut there.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use jarvis_core::action::Action;
use jarvis_core::brain::{Brain, BrainError, BrainEvent, BrainRequest};
use serde_json::{json, Map, Value};
use tokio::sync::{broadcast, mpsc};
use tokio::time::{timeout_at, Instant};
use uuid::Uuid;

use crate::client::{LinkStatus, NotConnected};
use crate::node::{NodeHandle, ReplyEvent};
use crate::speech::Sentences;
use crate::wire::AuthzObject;

/// The desktop tools the server may call, and the action kind each must arrive under:
/// the `module.` and `screen.` entries of `TOOL_ACTIONS` in `server/.../core/prompt.py`.
pub const SERVER_TOOLS: &[(&str, &str)] = &[
    ("show_news", "module.news"),
    ("open_story", "module.news"),
    ("summarise_story", "module.news"),
    ("play_live_tv", "module.live_tv"),
    ("next_channel", "module.live_tv"),
    ("close_panel", "screen.close_panel"),
    ("clear_screen", "screen.clear"),
];

/// How long a turn waits for the next event of its reply.
pub const SILENCE: Duration = Duration::from_secs(20);
/// The `utterance` contract's limits, in characters.
pub const MAX_TEXT: usize = 4000;
pub const MAX_SCREEN: usize = 2000;

const UNREACHABLE: &str = "I can't reach the Jarvis server";
const NO_ANSWER: &str = "the Jarvis server didn't answer";
const LOST: &str = "lost part of the Jarvis server's reply";

/// What `ServerBrain` needs from the node.
#[async_trait]
pub trait Link: Send + Sync {
    fn connected(&self) -> bool;
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent>;
    /// Send a signed message; the envelope's `message_id` on success.
    async fn send(&self, kind: &'static str, payload: Value) -> Result<Uuid, NotConnected>;
}

#[async_trait]
impl Link for NodeHandle {
    fn connected(&self) -> bool {
        *self.status().borrow() == LinkStatus::Connected
    }
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent> {
        self.subscribe_replies()
    }
    async fn send(&self, kind: &'static str, payload: Value) -> Result<Uuid, NotConnected> {
        self.send_tracked(kind, payload).await
    }
}

pub struct ServerBrain {
    link: Arc<dyn Link>,
}

impl ServerBrain {
    pub fn new(link: Arc<dyn Link>) -> Self {
        Self { link }
    }

    /// Answer one live action of the followed reply.
    async fn on_action(
        &self,
        reply_id: Uuid,
        seq: i64,
        authz: AuthzObject,
        tx: &mpsc::Sender<BrainEvent>,
    ) {
        let kind = authz.action_kind.as_str();
        if kind.starts_with("pc.") || kind.starts_with("board.") {
            return; // the executor's (Task 8); the node's own Board handling (`node::on_board`)
        }
        let check = if !(kind.starts_with("module.") || kind.starts_with("screen.")) {
            Err("unknown_action_kind")
        } else {
            tool_call(&authz.args, kind)
        };
        let (result, evidence) = match check {
            Ok(action) => {
                let tool = action.name.clone();
                if tx.send(BrainEvent::Action(action)).await.is_ok() {
                    // D-2: OK = verified and handed to the coordinator, not that the panel rendered.
                    tracing::info!(%reply_id, seq, %tool, "readonly_action handed_to_coordinator");
                    ("OK", json!({}))
                } else {
                    ("REFUSED", json!({"check": "not_delivered"}))
                }
            }
            Err(check) => {
                tracing::warn!(%reply_id, seq, action_kind = kind, check, "live action refused");
                ("REFUSED", json!({"check": check}))
            }
        };
        let receipt =
            json!({"reply_id": reply_id, "seq": seq, "result": result, "evidence": evidence});
        if self.link.send("action.receipt", receipt).await.is_err() {
            tracing::warn!(%reply_id, seq, "couldn't send the live receipt: not connected");
        }
    }
}

/// The desktop action for `{tool, input}` under `kind`, or the check it fails.
fn tool_call(args: &Map<String, Value>, kind: &str) -> Result<Action, &'static str> {
    let allowed = |k: &String| k == "tool" || k == "input";
    let (Some(tool), true) = (
        args.get("tool").and_then(Value::as_str),
        args.keys().all(allowed),
    ) else {
        return Err("malformed_args");
    };
    let input = match args.get("input") {
        None | Some(Value::Null) => Value::Object(Map::new()),
        Some(v @ Value::Object(_)) => v.clone(),
        Some(_) => return Err("malformed_args"),
    };
    if SERVER_TOOLS.contains(&(tool, kind)) {
        Ok(Action::new(tool, input))
    } else {
        Err("unknown_tool")
    }
}

fn clip(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}

#[async_trait]
impl Brain for ServerBrain {
    async fn think(
        &self,
        req: BrainRequest,
        tx: mpsc::Sender<BrainEvent>,
    ) -> Result<(), BrainError> {
        if !self.link.connected() {
            return Err(BrainError::Unavailable(UNREACHABLE.into()));
        }
        let text = req.utterance.trim();
        if text.is_empty() {
            return Err(BrainError::Protocol("an empty utterance".into()));
        }
        let screen = req.screen_summary.trim();
        let screen = (!screen.is_empty()).then(|| clip(screen, MAX_SCREEN));
        // Subscribe before sending, so nothing of the reply can slip past.
        let mut rx = self.link.subscribe();
        let utterance = json!({"text": clip(text, MAX_TEXT), "screen_summary": screen});
        let Ok(asked) = self.link.send("utterance", utterance).await else {
            return Err(BrainError::Unavailable(UNREACHABLE.into()));
        };

        let mut following: Option<(Uuid, i64)> = None; // the reply, and its last seq seen
        let mut sentences = Sentences::default();
        let mut deadline = Instant::now() + SILENCE;
        loop {
            let ev = match timeout_at(deadline, rx.recv()).await {
                Err(_) => return Err(BrainError::Unavailable(NO_ANSWER.into())),
                Ok(Err(broadcast::error::RecvError::Lagged(n))) => {
                    tracing::warn!(lost = n, "ServerBrain lagged behind the reply");
                    return Err(BrainError::Unavailable(LOST.into()));
                }
                Ok(Err(broadcast::error::RecvError::Closed)) => {
                    return Err(BrainError::Unavailable(UNREACHABLE.into()))
                }
                Ok(Ok(ev)) => ev,
            };
            let (reply_id, seq, answers) = match &ev {
                ReplyEvent::Speech {
                    reply_id,
                    seq,
                    in_reply_to,
                    ..
                } => (*reply_id, *seq, *in_reply_to),
                ReplyEvent::Action {
                    seq,
                    in_reply_to,
                    authz,
                } => match authz.reply_id {
                    Some(id) => (id, *seq, *in_reply_to),
                    None => continue, // the node only broadcasts live actions
                },
            };
            match &mut following {
                Some((id, last)) if *id == reply_id => {
                    if seq <= *last {
                        tracing::debug!(%reply_id, seq, "re-sent reply event dropped");
                        continue;
                    }
                    *last = seq;
                }
                Some(_) => continue,
                None if answers == Some(asked) => following = Some((reply_id, seq)),
                None => continue,
            }
            deadline = Instant::now() + SILENCE;
            match ev {
                ReplyEvent::Speech { text, final_, .. } => {
                    let mut say = sentences.push(&text);
                    if final_ {
                        say.extend(sentences.finish());
                    }
                    for s in say {
                        if tx.send(BrainEvent::Speech(s)).await.is_err() {
                            return Ok(()); // the caller has gone
                        }
                    }
                    if final_ {
                        return Ok(());
                    }
                }
                ReplyEvent::Action { seq, authz, .. } => {
                    // Words before the action are said before it, as they came.
                    if let Some(s) = sentences.finish() {
                        if tx.send(BrainEvent::Speech(s)).await.is_err() {
                            return Ok(());
                        }
                    }
                    self.on_action(reply_id, seq, authz, &tx).await;
                }
            }
        }
    }
}
