//! `jarvis-node-probe`: the real `jarvis-node` runtime (the same `node::start`, session,
//! journal, inbox, approval and executor code the app uses) driven from the command line, for
//! the end-to-end tests against the Python gateway and core (plan B Task 9, D-3). Only the UI
//! (one JSON line per event on stdout), the test folder (in memory) and the key store (a file
//! in the state folder, test-only) are replaced. It has no protocol code of its own.
//!
//! ```text
//! jarvis-node-probe --gateway HOST:PORT --state-dir DIR [--memory-folder NAME=TEXT]...
//!                   (--enrol CODE | --say TEXT | --approve-next | --render-next
//!                    | --refuse-render | --set-state STATE | --wait SECS)...
//! ```
//! The Board (slice 1): every Board event is printed, and the panels the store holds at start
//! (`board_restored`). `--render-next` waits for the next shown or updated panel and reports
//! it drawn, as the Board window would; `--refuse-render` reports that it could not be drawn;
//! `--set-state` changes the newest panel's state as a click would.
//! Steps run in order; `--memory-folder` files exist from the start. Logs go to stderr. The
//! enrolment code is never printed.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use ed25519_dalek::SigningKey;
use jarvis_core::brain::{Brain, BrainEvent, BrainRequest};
use jarvis_node::approval::{ApprovalCard, Decision};
use jarvis_node::board::{BoardEvent, Panel, PanelState};
use jarvis_node::client::{ClientConfig, LinkStatus};
use jarvis_node::folder::memory::MemoryFolder;
use jarvis_node::keystore::{KeyStore, KeyStoreError};
use jarvis_node::node::{self, NodeDeps, ReplyEvent, UiEvent};
use jarvis_node::server_brain::ServerBrain;
use serde_json::{json, Value};
use tokio::sync::mpsc;

enum Step {
    Enrol(String),
    Say(String),
    ApproveNext,
    RenderNext(bool),
    SetState(PanelState),
    Wait(u64),
}

struct Args {
    gateway: String,
    state_dir: PathBuf,
    files: Vec<(String, String)>,
    steps: Vec<Step>,
}

fn parse() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let (mut gateway, mut state_dir, mut files, mut steps) = (None, None, vec![], vec![]);
    while let Some(flag) = it.next() {
        let mut value = || it.next().ok_or(format!("{flag} needs a value"));
        match flag.as_str() {
            "--gateway" => gateway = Some(value()?),
            "--state-dir" => state_dir = Some(PathBuf::from(value()?)),
            "--memory-folder" => {
                let v = value()?;
                let (name, text) = v.split_once('=').ok_or("--memory-folder NAME=TEXT")?;
                files.push((name.to_owned(), text.to_owned()));
            }
            "--enrol" => steps.push(Step::Enrol(value()?)),
            "--say" => steps.push(Step::Say(value()?)),
            "--approve-next" => steps.push(Step::ApproveNext),
            "--render-next" => steps.push(Step::RenderNext(true)),
            "--refuse-render" => steps.push(Step::RenderNext(false)),
            "--set-state" => steps.push(Step::SetState(match value()?.as_str() {
                "open" => PanelState::Open,
                "minimised" => PanelState::Minimised,
                "maximised" => PanelState::Maximised,
                "closed" => PanelState::Closed,
                _ => return Err("--set-state open|minimised|maximised|closed".into()),
            })),
            "--wait" => steps.push(Step::Wait(value()?.parse().map_err(|_| "--wait SECS")?)),
            other => return Err(format!("unknown argument {other}")),
        }
    }
    Ok(Args {
        gateway: gateway.ok_or("--gateway is required")?,
        state_dir: state_dir.ok_or("--state-dir is required")?,
        files,
        steps,
    })
}

/// Test-only: the node key as a plain file in the state folder, so a restarted probe is the
/// same node. The app uses DPAPI.
struct FileKeyStore(PathBuf);

impl KeyStore for FileKeyStore {
    fn load(&self) -> Result<Option<SigningKey>, KeyStoreError> {
        match std::fs::read(&self.0) {
            Ok(b) => {
                let seed: [u8; 32] = b.try_into().map_err(|_| KeyStoreError::Corrupt)?;
                Ok(Some(SigningKey::from_bytes(&seed)))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }
    fn save(&self, key: &SigningKey) -> Result<(), KeyStoreError> {
        Ok(std::fs::write(&self.0, key.to_bytes())?)
    }
    fn delete(&self) -> Result<(), KeyStoreError> {
        match std::fs::remove_file(&self.0) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        }
    }
}

fn say(v: Value) {
    println!("{v}");
}

fn link(s: &LinkStatus) -> String {
    match s {
        LinkStatus::NotEnrolled => "not_enrolled".into(),
        LinkStatus::Connecting => "connecting".into(),
        LinkStatus::Connected => "connected".into(),
        LinkStatus::NeedsReenrol => "needs_reenrol".into(),
        LinkStatus::Failed(why) => format!("failed: {why}"),
    }
}

fn panel_line(event: &str, p: &Panel) -> Value {
    json!({
        "event": event, "panel_id": p.panel_id, "revision": p.revision, "kind": p.kind,
        "title": p.title, "state": p.state, "content_sha256": p.content_sha256,
        "cost_paise": p.cost_paise,
    })
}

/// Print every UI event; pass cards on for `--approve-next` and panels for `--render-next`.
async fn print_ui(
    mut ui: mpsc::Receiver<UiEvent>,
    cards: mpsc::UnboundedSender<ApprovalCard>,
    panels: mpsc::UnboundedSender<Panel>,
) {
    while let Some(ev) = ui.recv().await {
        match ev {
            UiEvent::Notice {
                outbox_id,
                priority,
                kind,
                body,
            } => say(json!({
                "event": "notice", "outbox_id": outbox_id, "priority": priority, "kind": kind,
                "body": body,
            })),
            UiEvent::NoticeCleared { outbox_id, reason } => {
                say(json!({"event": "notice_cleared", "outbox_id": outbox_id, "reason": reason}))
            }
            UiEvent::Summary(groups) => say(json!({"event": "summary", "groups": groups})),
            UiEvent::Link(s) => say(json!({"event": "link", "status": link(&s)})),
            UiEvent::ClockOff(ms) => say(json!({"event": "clock_off", "ms": ms})),
            UiEvent::ApprovalCard(card) => {
                say(json!({
                    "event": "card", "outbox_id": card.outbox_id, "approval_id": card.approval_id,
                    "summary": card.summary, "valid_until": card.valid_until.to_rfc3339(),
                }));
                let _ = cards.send(card);
            }
            UiEvent::ApprovalCleared { outbox_id, reason } => {
                say(json!({"event": "card_cleared", "outbox_id": outbox_id, "reason": reason}))
            }
            UiEvent::Board(BoardEvent::Show(p)) => {
                say(panel_line("board_show", &p));
                let _ = panels.send(p);
            }
            UiEvent::Board(BoardEvent::Update(p)) => {
                say(panel_line("board_update", &p));
                let _ = panels.send(p);
            }
            UiEvent::Board(BoardEvent::State { panel_id, state }) => {
                say(json!({"event": "board_state", "panel_id": panel_id, "state": state}))
            }
            UiEvent::Board(BoardEvent::Focus { panel_id }) => {
                say(json!({"event": "board_focus", "panel_id": panel_id}))
            }
        }
    }
}

/// Every reply event the node accepts, whoever it answers.
async fn print_replies(mut rx: tokio::sync::broadcast::Receiver<ReplyEvent>) {
    while let Ok(ev) = rx.recv().await {
        match ev {
            ReplyEvent::Speech {
                reply_id,
                seq,
                text,
                final_,
                in_reply_to,
            } => say(json!({
                "event": "reply", "reply_id": reply_id, "seq": seq, "text": text,
                "final": final_, "in_reply_to": in_reply_to,
            })),
            ReplyEvent::Action {
                seq,
                in_reply_to,
                authz,
            } => say(json!({
                "event": "reply_action", "reply_id": authz.reply_id, "seq": seq,
                "action_kind": authz.action_kind, "in_reply_to": in_reply_to,
            })),
        }
    }
}

async fn run(args: Args) -> Result<(), String> {
    std::fs::create_dir_all(&args.state_dir).map_err(|e| e.to_string())?;
    let folder = Arc::new(MemoryFolder::new());
    for (name, text) in &args.files {
        folder.put(name, text.as_bytes());
    }
    let (ui_tx, ui) = mpsc::channel(256);
    let deps = NodeDeps {
        keystore: Arc::new(FileKeyStore(args.state_dir.join("probe-node-key"))),
        ui: ui_tx,
        folder: folder.clone(),
    };
    let handle = node::start(ClientConfig::new(args.gateway, &args.state_dir), deps)
        .map_err(|e| e.to_string())?;
    let (cards_tx, mut cards) = mpsc::unbounded_channel();
    let (panels_tx, mut panels) = mpsc::unbounded_channel();
    let restored = handle
        .board()
        .lock()
        .map_err(|_| "the Board store is unusable")?
        .panels();
    for p in &restored {
        say(panel_line("board_restored", p));
    }
    tokio::spawn(print_ui(ui, cards_tx, panels_tx));
    tokio::spawn(print_replies(handle.subscribe_replies()));
    let brain = Arc::new(ServerBrain::new(Arc::new(handle.clone())));
    for step in args.steps {
        match step {
            Step::Enrol(code) => {
                handle
                    .enrol(&code)
                    .await
                    .map_err(|e| format!("enrol: {e}"))?;
                // The saved state is published just after the welcome is verified.
                let mut state = handle.state();
                let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
                while state.borrow().node_id.is_none() {
                    if tokio::time::timeout_at(deadline, state.changed())
                        .await
                        .is_err()
                    {
                        return Err("enrolled, but no node ID was saved".into());
                    }
                }
                let node_id = state.borrow().node_id;
                say(json!({"event": "enrolled", "node_id": node_id}));
            }
            Step::Say(text) => {
                let mut status = handle.status();
                let up = jarvis_node::client::wait_for(
                    &mut status,
                    |s| *s == LinkStatus::Connected,
                    Duration::from_secs(20),
                )
                .await;
                if !up {
                    return Err("not connected".into());
                }
                let (tx, mut rx) = mpsc::channel(64);
                let request = BrainRequest {
                    utterance: text,
                    screen_summary: String::new(),
                    history: vec![],
                    tools: vec![],
                };
                let b = brain.clone();
                let turn = tokio::spawn(async move { b.think(request, tx).await });
                while let Some(ev) = rx.recv().await {
                    match ev {
                        BrainEvent::Speech(s) => say(json!({"event": "speech", "text": s})),
                        BrainEvent::Action(a) => {
                            say(json!({"event": "action", "tool": a.name, "input": a.args}))
                        }
                    }
                }
                match turn.await.map_err(|e| e.to_string())? {
                    Ok(()) => say(json!({"event": "turn_done"})),
                    Err(e) => say(json!({"event": "turn_failed", "error": e.to_string()})),
                }
            }
            Step::ApproveNext => {
                let card = tokio::time::timeout(Duration::from_secs(60), cards.recv())
                    .await
                    .map_err(|_| "no approval card arrived")?
                    .ok_or("the node stopped")?;
                handle
                    .decide(card.outbox_id, Decision::Approve)
                    .await
                    .map_err(|e| format!("decide: {e}"))?;
                say(
                    json!({"event": "decided", "outbox_id": card.outbox_id, "decision": "APPROVED"}),
                );
            }
            Step::RenderNext(ok) => {
                let p = tokio::time::timeout(Duration::from_secs(60), panels.recv())
                    .await
                    .map_err(|_| "no panel arrived")?
                    .ok_or("the node stopped")?;
                handle.board_rendered(p.panel_id, p.revision, ok).await;
                say(json!({
                    "event": "board_rendered", "panel_id": p.panel_id, "revision": p.revision,
                    "ok": ok,
                }));
            }
            Step::SetState(state) => {
                let newest = handle
                    .board()
                    .lock()
                    .map_err(|_| "the Board store is unusable")?
                    .panels()
                    .last()
                    .map(|p| p.panel_id)
                    .ok_or("no panel to change")?;
                handle
                    .board_state(newest, state)
                    .await
                    .map_err(|e| format!("set-state: {e}"))?;
                say(json!({"event": "board_state_set", "panel_id": newest, "state": state}));
            }
            Step::Wait(secs) => tokio::time::sleep(Duration::from_secs(secs)).await,
        }
    }
    Ok(())
}

fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    let args = match parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("usage error: {e}");
            std::process::exit(2);
        }
    };
    let rt = tokio::runtime::Runtime::new().expect("a tokio runtime");
    let result = rt.block_on(run(args));
    // Stop the printers first, so `done` (or `error`) is always the last line.
    rt.shutdown_timeout(Duration::from_secs(2));
    match result {
        Ok(()) => say(json!({"event": "done"})),
        Err(e) => {
            say(json!({"event": "error", "error": e}));
            std::process::exit(1);
        }
    }
}
