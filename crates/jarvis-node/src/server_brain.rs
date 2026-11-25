//! `ServerBrain`: the desktop's existing `Brain` trait, answered by the Jarvis server through
//! the node link (spec §13). The rest of the desktop (panels, voice, modules) is unchanged.
//!
//! - `think` sends `utterance {text, screen_summary}` and nothing else: the server owns the
//!   history and the tools.
//! - It follows one reply: the first whose first event after the send has `seq` 1, and never
//!   one it followed before. A reply resumed after a reconnect starts past 1, and a late chunk
//!   of an earlier reply is already known, so neither can hijack this turn. Replies stream to
//!   every device of the person (R12-9) and `reply_id` is not tied to the utterance, so a
//!   reply to another of the person's devices that starts in the same moment could still be
//!   followed; that needs a server-side link from utterance to reply.
//! - SPEECH becomes `BrainEvent::Speech` at once; `final` ends the turn.
//! - A live `module.*` / `screen.*` action (args `{tool, input}`) whose tool `SERVER_TOOLS`
//!   maps to that kind becomes `BrainEvent::Action`, then the receipt `OK` (D-2: OK means the
//!   action was verified and handed to the coordinator, **not** that the panel rendered).
//!   Anything else gets `REFUSED` with the failed check. `pc.*` actions are the executor's
//!   (Task 8), so they get nothing here.
//! - No event of the followed reply for `SILENCE` fails the turn, as does losing events to a
//!   lagging broadcast (they can't be recovered).

use std::collections::{HashSet, VecDeque};
use std::sync::{Arc, Mutex};
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
/// How many followed reply ids are remembered.
const REMEMBERED: usize = 256;

const UNREACHABLE: &str = "I can't reach the Jarvis server";
const NO_ANSWER: &str = "the Jarvis server didn't answer";
const LOST: &str = "lost part of the Jarvis server's reply";

/// What `ServerBrain` needs from the node.
#[async_trait]
pub trait Link: Send + Sync {
    fn connected(&self) -> bool;
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent>;
    async fn send(&self, kind: &'static str, payload: Value) -> Result<(), NotConnected>;
}

#[async_trait]
impl Link for NodeHandle {
    fn connected(&self) -> bool {
        *self.status().borrow() == LinkStatus::Connected
    }
    fn subscribe(&self) -> broadcast::Receiver<ReplyEvent> {
        self.subscribe_replies()
    }
    async fn send(&self, kind: &'static str, payload: Value) -> Result<(), NotConnected> {
        NodeHandle::send(self, kind, payload).await
    }
}

pub struct ServerBrain {
    link: Arc<dyn Link>,
    followed: Mutex<Followed>,
}

#[derive(Default)]
struct Followed {
    set: HashSet<Uuid>,
    order: VecDeque<Uuid>,
}

impl Followed {
    fn add(&mut self, id: Uuid) {
        if self.set.insert(id) {
            self.order.push_back(id);
            if self.order.len() > REMEMBERED {
                if let Some(old) = self.order.pop_front() {
                    self.set.remove(&old);
                }
            }
        }
    }
}

impl ServerBrain {
    pub fn new(link: Arc<dyn Link>) -> Self {
        Self {
            link,
            followed: Mutex::default(),
        }
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
        if kind.starts_with("pc.") {
            return; // the executor's (Task 8)
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
                    tracing::info!(%reply_id, seq, %tool, "live action OK: verified and handed to the coordinator (not proof the panel rendered)");
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
        if self.link.send("utterance", utterance).await.is_err() {
            return Err(BrainError::Unavailable(UNREACHABLE.into()));
        }

        let mut following: Option<Uuid> = None;
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
            let (reply_id, seq) = match &ev {
                ReplyEvent::Speech { reply_id, seq, .. } => (*reply_id, *seq),
                ReplyEvent::Action { seq, authz } => match authz.reply_id {
                    Some(id) => (id, *seq),
                    None => continue, // the node only broadcasts live actions
                },
            };
            match following {
                Some(id) if id == reply_id => {}
                Some(_) => continue,
                None => {
                    let mut followed = self.followed.lock().expect("followed lock");
                    if seq != 1 || followed.set.contains(&reply_id) {
                        continue;
                    }
                    followed.add(reply_id);
                    following = Some(reply_id);
                }
            }
            deadline = Instant::now() + SILENCE;
            match ev {
                ReplyEvent::Speech { text, final_, .. } => {
                    if !text.is_empty() && tx.send(BrainEvent::Speech(text)).await.is_err() {
                        return Ok(()); // the caller has gone
                    }
                    if final_ {
                        return Ok(());
                    }
                }
                ReplyEvent::Action { seq, authz } => {
                    self.on_action(reply_id, seq, authz, &tx).await;
                }
            }
        }
    }
}
