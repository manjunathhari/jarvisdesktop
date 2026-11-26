//! The Board on the desktop (Board slice 1, Task 5): what a `board.*` action says, the local
//! copy of the panels, and the conversation.
//!
//! - `parse` reads the args of a core-signed Board action against the contract
//!   (`contracts/messages/board.*.args`) and names the check a bad one fails: `too_large`
//!   (content over 30,000 bytes of UTF-8), `hash_mismatch` (the hash is not the content's),
//!   `unknown_kind` (not `text` or `canvas`), `malformed` (anything else), and
//!   `unknown_action_kind`. Limits are counted as the server counts them: a title in Unicode
//!   code points, content in bytes.
//! - `BoardStore` is the desktop's own copy (plan P-5): the server is the record, and this is
//!   what the Board window draws and restores after a restart. `board.json` holds the panels
//!   and is rewritten whole on each change (temp file, sync, rename, like the node's state);
//!   `conversation.jsonl` is appended to. Nothing is deleted except closed panels, by
//!   `clear_closed`, which the Board calls only after the person confirms.
//! - What `apply` decides:
//!   - a `Show` or `Update` at a revision older than the one held is `stale_revision`; so is the
//!     held revision again with other content. The same revision and content again is a
//!     re-send and changes nothing;
//!   - an `Update` keeps the panel's state (a closed panel stays closed; its content is new
//!     when it is reopened). An `Update` for a panel the store does not hold adds it, open: an
//!     update carries the whole panel;
//!   - `State` sets the state given, whatever it was (so "minimise" on a closed panel brings it
//!     back, minimised);
//!   - `Focus` brings a minimised or closed panel back to open; one already in view keeps its
//!     state;
//!   - `State` or `Focus` for a panel the store does not hold is `unknown_panel`.

use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::state::write_atomic;

pub const TITLE_MAX: usize = 120;
pub const CONTENT_MAX_BYTES: usize = 30_000;
/// `revision` and `cost_paise` are at most this (the contract's, a Postgres integer).
pub const INT_MAX: u64 = 2_147_483_647;
/// The conversation entries loaded at start; the file keeps them all.
pub const CONVERSATION_LOADED: usize = 2_000;
pub const BOARD_FILE: &str = "board.json";
pub const CONVERSATION_FILE: &str = "conversation.jsonl";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PanelKind {
    Text,
    Canvas,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PanelState {
    Open,
    Minimised,
    Maximised,
    Closed,
}

impl PanelState {
    /// The contract's word for the state.
    pub fn as_str(self) -> &'static str {
        match self {
            PanelState::Open => "open",
            PanelState::Minimised => "minimised",
            PanelState::Maximised => "maximised",
            PanelState::Closed => "closed",
        }
    }

    fn from_word(word: &str) -> Option<Self> {
        match word {
            "open" => Some(PanelState::Open),
            "minimised" => Some(PanelState::Minimised),
            "maximised" => Some(PanelState::Maximised),
            "closed" => Some(PanelState::Closed),
            _ => None,
        }
    }
}

/// One panel at its current revision. `cost_paise` is what it has cost so far.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Panel {
    pub panel_id: Uuid,
    pub kind: PanelKind,
    pub title: String,
    pub content: String,
    pub content_sha256: String,
    pub revision: u32,
    pub state: PanelState,
    pub cost_paise: u64,
    pub created_at: DateTime<FixedOffset>,
}

/// What a verified Board action asks of the Board.
#[derive(Debug, Clone, PartialEq)]
pub enum BoardEvent {
    Show(Panel),
    Update(Panel),
    State { panel_id: Uuid, state: PanelState },
    Focus { panel_id: Uuid },
}

impl BoardEvent {
    pub fn panel_id(&self) -> Uuid {
        match self {
            BoardEvent::Show(p) | BoardEvent::Update(p) => p.panel_id,
            BoardEvent::State { panel_id, .. } | BoardEvent::Focus { panel_id } => *panel_id,
        }
    }
}

/// SHA-256 of the content's UTF-8 bytes, lower-case hex: a panel's `content_sha256`.
pub fn content_sha256(content: &str) -> String {
    Sha256::digest(content.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// A title's control characters, as the server refuses them (`contracts.py`): C0 and DEL.
fn title_control(c: char) -> bool {
    (c as u32) < 0x20 || c as u32 == 0x7f
}

/// Content's: C0 except tab, line feed and carriage return.
fn content_control(c: char) -> bool {
    (c as u32) < 0x20 && !matches!(c, '\t' | '\n' | '\r')
}

fn panel_id_of(args: &Map<String, Value>) -> Option<Uuid> {
    Uuid::parse_str(args.get("panel_id")?.as_str()?).ok()
}

fn only_keys(args: &Map<String, Value>, keys: &[&str]) -> bool {
    args.len() == keys.len() && keys.iter().all(|k| args.contains_key(*k))
}

const PANEL_KEYS: [&str; 8] = [
    "panel_id",
    "revision",
    "kind",
    "title",
    "content",
    "content_sha256",
    "cost_paise",
    "created_at",
];

fn parse_panel(args: &Map<String, Value>) -> Result<Panel, &'static str> {
    let text = |k: &str| args.get(k).and_then(Value::as_str);
    let whole = |k: &str| {
        args.get(k)
            .and_then(Value::as_u64)
            .filter(|n| *n <= INT_MAX)
    };
    if !only_keys(args, &PANEL_KEYS) {
        return Err("malformed");
    }
    let (Some(panel_id), Some(revision), Some(kind), Some(title), Some(content)) = (
        panel_id_of(args),
        whole("revision").filter(|n| *n >= 1),
        text("kind"),
        text("title"),
        text("content"),
    ) else {
        return Err("malformed");
    };
    let (Some(claimed), Some(cost_paise), Some(created_at)) = (
        text("content_sha256"),
        whole("cost_paise"),
        text("created_at").and_then(|t| DateTime::parse_from_rfc3339(t).ok()),
    ) else {
        return Err("malformed");
    };
    let kind = match kind {
        "text" => PanelKind::Text,
        "canvas" => PanelKind::Canvas,
        _ => return Err("unknown_kind"),
    };
    let chars = title.chars().count();
    let blank = title.chars().all(|c| c == ' ');
    if chars > TITLE_MAX || blank || title.chars().any(title_control) {
        return Err("malformed");
    }
    if content.len() > CONTENT_MAX_BYTES {
        return Err("too_large");
    }
    if content.is_empty() || content.chars().any(content_control) {
        return Err("malformed");
    }
    let content_sha256 = content_sha256(content);
    if claimed != content_sha256 {
        return Err("hash_mismatch");
    }
    Ok(Panel {
        panel_id,
        kind,
        title: title.to_owned(),
        content: content.to_owned(),
        content_sha256,
        revision: revision as u32,
        state: PanelState::Open,
        cost_paise,
        created_at,
    })
}

/// Read the args of a Board action, or name the check they fail.
pub fn parse(action_kind: &str, args: &Map<String, Value>) -> Result<BoardEvent, &'static str> {
    match action_kind {
        "board.show" => parse_panel(args).map(BoardEvent::Show),
        "board.update" => parse_panel(args).map(BoardEvent::Update),
        "board.set_state" => {
            let state = args
                .get("state")
                .and_then(Value::as_str)
                .and_then(PanelState::from_word);
            match (panel_id_of(args), state) {
                (Some(panel_id), Some(state)) if only_keys(args, &["panel_id", "state"]) => {
                    Ok(BoardEvent::State { panel_id, state })
                }
                _ => Err("malformed"),
            }
        }
        "board.focus" => match panel_id_of(args) {
            Some(panel_id) if only_keys(args, &["panel_id"]) => Ok(BoardEvent::Focus { panel_id }),
            _ => Err("malformed"),
        },
        _ => Err("unknown_action_kind"),
    }
}

/// The evidence of a receipt for args that failed `check`: what the node could still read
/// from them, and null for the rest (`board.show.receipt`). The hash is the one computed here.
pub fn refused_evidence(action_kind: &str, args: &Map<String, Value>, check: &str) -> Value {
    let panel_id = panel_id_of(args);
    if matches!(action_kind, "board.show" | "board.update") {
        let revision = args
            .get("revision")
            .and_then(Value::as_u64)
            .filter(|n| (1..=INT_MAX).contains(n));
        let hash = args
            .get("content")
            .and_then(Value::as_str)
            .map(content_sha256);
        serde_json::json!({
            "panel_id": panel_id, "revision": revision, "content_sha256": hash, "check": check,
        })
    } else {
        serde_json::json!({"panel_id": panel_id, "check": check})
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Who {
    Person,
    Jarvis,
}

/// How a line came about: heard, typed, or spoken by Jarvis.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum How {
    Voice,
    Typed,
    Spoken,
}

/// One line of the conversation; `panels` are those made in that reply.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConversationEntry {
    pub at: DateTime<FixedOffset>,
    pub who: Who,
    pub text: String,
    pub how: How,
    pub panels: Vec<Uuid>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BoardFile {
    v: u32,
    panels: Vec<Panel>,
}

/// The desktop's copy of the panels (in the order they first arrived) and the conversation.
pub struct BoardStore {
    dir: PathBuf,
    panels: Vec<Panel>,
    conversation: Vec<ConversationEntry>,
}

impl BoardStore {
    /// Open the store in the app's data folder. A `board.json` that cannot be read is set
    /// aside as `board.json.unreadable` (never deleted) and the Board starts empty: the server
    /// still holds every panel.
    pub fn open(dir: &Path) -> Self {
        let path = dir.join(BOARD_FILE);
        let panels = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<BoardFile>(&bytes) {
                Ok(file) if file.v == 1 => file.panels,
                _ => {
                    tracing::error!("board.json is unreadable; set aside, starting empty");
                    let _ = std::fs::rename(&path, dir.join(format!("{BOARD_FILE}.unreadable")));
                    Vec::new()
                }
            },
            Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
            Err(e) => {
                tracing::error!(error = %e, "couldn't read board.json; starting empty");
                Vec::new()
            }
        };
        let mut conversation: Vec<ConversationEntry> =
            std::fs::read_to_string(dir.join(CONVERSATION_FILE))
                .unwrap_or_default()
                .lines()
                .filter_map(|line| serde_json::from_str(line).ok()) // a torn last line is skipped
                .collect();
        let extra = conversation.len().saturating_sub(CONVERSATION_LOADED);
        conversation.drain(..extra);
        Self {
            dir: dir.to_owned(),
            panels,
            conversation,
        }
    }

    pub fn panels(&self) -> Vec<Panel> {
        self.panels.clone()
    }

    pub fn panel(&self, panel_id: Uuid) -> Option<&Panel> {
        self.panels.iter().find(|p| p.panel_id == panel_id)
    }

    fn save(&self, panels: &[Panel]) -> io::Result<()> {
        let file = BoardFile {
            v: 1,
            panels: panels.to_vec(),
        };
        let bytes = serde_json::to_vec(&file).map_err(io::Error::other)?;
        write_atomic(&self.dir.join(BOARD_FILE), &bytes)
    }

    /// Apply a verified event: saved first, so the copy in memory never runs ahead of the
    /// file. An error names the check (`stale_revision`, `unknown_panel`, `store_failed`) and
    /// nothing has changed.
    pub fn apply(&mut self, event: &BoardEvent) -> Result<(), &'static str> {
        let mut next = self.panels.clone();
        let at = next.iter().position(|p| p.panel_id == event.panel_id());
        match (event, at) {
            (BoardEvent::Show(new) | BoardEvent::Update(new), Some(i)) => {
                let held = &next[i];
                let same = held.revision == new.revision
                    && held.content_sha256 == new.content_sha256
                    && held.title == new.title;
                if same {
                    return Ok(()); // a re-send
                }
                if new.revision <= held.revision {
                    return Err("stale_revision");
                }
                let state = held.state;
                next[i] = Panel {
                    state,
                    ..new.clone()
                };
            }
            (BoardEvent::Show(new) | BoardEvent::Update(new), None) => next.push(new.clone()),
            (BoardEvent::State { state, .. }, Some(i)) => next[i].state = *state,
            (BoardEvent::Focus { .. }, Some(i)) => {
                if matches!(next[i].state, PanelState::Minimised | PanelState::Closed) {
                    next[i].state = PanelState::Open;
                }
            }
            (BoardEvent::State { .. } | BoardEvent::Focus { .. }, None) => {
                return Err("unknown_panel")
            }
        }
        if next != self.panels {
            self.save(&next).map_err(|e| {
                tracing::error!(error = %e, "couldn't save board.json");
                "store_failed"
            })?;
            self.panels = next;
        }
        Ok(())
    }

    /// Remove the closed panels from this desktop's copy; how many went. The Board asks the
    /// person first. The server keeps them.
    pub fn clear_closed(&mut self) -> io::Result<usize> {
        let kept: Vec<Panel> = self
            .panels
            .iter()
            .filter(|p| p.state != PanelState::Closed)
            .cloned()
            .collect();
        let cleared = self.panels.len() - kept.len();
        if cleared > 0 {
            self.save(&kept)?;
            self.panels = kept;
        }
        Ok(cleared)
    }

    pub fn append_turn(&mut self, entry: ConversationEntry) -> io::Result<()> {
        let mut line = serde_json::to_string(&entry).map_err(io::Error::other)?;
        line.push('\n');
        let path = self.dir.join(CONVERSATION_FILE);
        // A torn last line (a crash mid-write) must not swallow this one.
        let torn = std::fs::read(&path).is_ok_and(|b| b.last().is_some_and(|c| *c != b'\n'));
        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        if torn {
            file.write_all(b"\n")?;
        }
        file.write_all(line.as_bytes())?;
        file.sync_data()?;
        self.conversation.push(entry);
        Ok(())
    }

    pub fn conversation(&self) -> Vec<ConversationEntry> {
        self.conversation.clone()
    }
}
