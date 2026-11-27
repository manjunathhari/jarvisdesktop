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
//! - The files outlive builds. Fields this build does not know are ignored, not refused, so a
//!   file written by a newer build still loads after a roll-back. A `board.json` that cannot be
//!   parsed is set aside under a name of its own (never overwritten) and the Board starts
//!   empty. One in a newer format (`v` above 1), or one that could not be read or set aside, is
//!   left exactly as it is and the store is read-only: every change is `store_failed`.
//! - What `apply` decides:
//!   - a `Show` or `Update` at a revision older than the one held is `stale_revision`; so is the
//!     held revision again with other content. The same revision and content again is a
//!     re-send and changes nothing;
//!   - a `Show` or `Update` of a panel in view keeps its state. One that is minimised or
//!     closed comes back, open: the person asked for the change, and a panel that is not on
//!     screen could never be reported drawn. An `Update` for a panel the store does not hold
//!     adds it, open: an update carries the whole panel;
//!   - `State` sets the state given, whatever it was (so "minimise" on a closed panel brings it
//!     back, minimised);
//!   - `Focus` brings a minimised or closed panel back to open; one already in view keeps its
//!     state;
//!   - `State` or `Focus` for a panel the store does not hold is `unknown_panel`.

use std::fs::OpenOptions;
use std::io::{self, Read, Seek, SeekFrom, Write};
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

/// What a verified Board action asks of the Board. For the window it serialises with a
/// `type` of `show`, `update`, `state` or `focus` beside the panel's or the event's fields.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
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
pub struct ConversationEntry {
    pub at: DateTime<FixedOffset>,
    pub who: Who,
    pub text: String,
    pub how: How,
    pub panels: Vec<Uuid>,
}

#[derive(Serialize, Deserialize)]
struct BoardFile {
    v: u32,
    panels: Vec<Panel>,
}

/// The desktop's copy of the panels (in the order they first arrived) and the conversation.
pub struct BoardStore {
    dir: PathBuf,
    panels: Vec<Panel>,
    conversation: Vec<ConversationEntry>,
    /// `board.json` is there but was not taken in: it must not be replaced.
    read_only: bool,
}

/// A name that no earlier set-aside file has.
fn set_aside_path(dir: &Path) -> PathBuf {
    let first = dir.join(format!("{BOARD_FILE}.unreadable"));
    if !first.exists() {
        return first;
    }
    (2u32..)
        .map(|n| dir.join(format!("{BOARD_FILE}.unreadable-{n}")))
        .find(|p| !p.exists())
        .expect("some name is free")
}

impl BoardStore {
    /// Open the store in the app's data folder (see the module comment for a file that cannot
    /// be used). The server still holds every panel.
    pub fn open(dir: &Path) -> Self {
        let path = dir.join(BOARD_FILE);
        let mut read_only = false;
        let panels = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<BoardFile>(&bytes) {
                Ok(file) if file.v == 1 => file.panels,
                Ok(file) => {
                    tracing::error!(v = file.v, "board.json is from a newer build; left alone");
                    read_only = true;
                    Vec::new()
                }
                Err(_) => {
                    tracing::error!("board.json is unreadable; set aside, starting empty");
                    if let Err(e) = std::fs::rename(&path, set_aside_path(dir)) {
                        tracing::error!(error = %e, "couldn't set board.json aside; left alone");
                        read_only = true;
                    }
                    Vec::new()
                }
            },
            Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
            Err(e) => {
                tracing::error!(error = %e, "couldn't read board.json; left alone");
                read_only = true;
                Vec::new()
            }
        };
        // By bytes and by line: a line torn by a crash, even inside a character, is skipped
        // and the lines before it are kept.
        let mut conversation: Vec<ConversationEntry> = std::fs::read(dir.join(CONVERSATION_FILE))
            .unwrap_or_default()
            .split(|b| *b == b'\n')
            .filter_map(|line| serde_json::from_slice(line).ok())
            .collect();
        let extra = conversation.len().saturating_sub(CONVERSATION_LOADED);
        conversation.drain(..extra);
        Self {
            dir: dir.to_owned(),
            panels,
            conversation,
            read_only,
        }
    }

    pub fn panels(&self) -> Vec<Panel> {
        self.panels.clone()
    }

    pub fn panel(&self, panel_id: Uuid) -> Option<&Panel> {
        self.panels.iter().find(|p| p.panel_id == panel_id)
    }

    fn save(&self, panels: &[Panel]) -> io::Result<()> {
        if self.read_only {
            return Err(io::Error::other(
                "board.json was not taken in; not replacing it",
            ));
        }
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
                let state = match held.state {
                    PanelState::Minimised | PanelState::Closed => PanelState::Open,
                    in_view => in_view,
                };
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
        if self.read_only {
            return Err(io::Error::other(
                "board.json was not taken in; not replacing it",
            ));
        }
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
        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .append(true)
            .open(&path)?;
        // A torn last line (a crash mid-write) must not swallow this one: look at the last byte.
        let mut last = *b"\n";
        if file.seek(SeekFrom::End(0))? > 0 {
            file.seek(SeekFrom::End(-1))?;
            file.read_exact(&mut last)?;
        }
        let torn = last[0] != b'\n';
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

/// What the Board window needs from the store, kept here (not in the app) so it is tested on
/// every platform: where the window goes, today's spend, and the snapshot it draws at start.
pub mod view {
    use chrono::{DateTime, FixedOffset};
    use serde::Serialize;

    use super::{BoardStore, ConversationEntry, Panel};

    /// A monitor's place and size on the desktop, in physical pixels.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct MonitorInfo {
        pub x: i32,
        pub y: i32,
        pub width: u32,
        pub height: u32,
    }

    /// Where the Board window goes.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Placement {
        /// Full screen, without a border, on `monitors[monitor]`.
        Borderless { monitor: usize },
        /// An ordinary window: there is no second monitor to give it.
        Normal,
    }

    /// With two or more monitors, the Board takes the largest one that the main window is not
    /// on (`main` is its index, if known; otherwise the first monitor is taken to be its). With
    /// one monitor, or none reported, it is an ordinary window.
    pub fn pick_placement(monitors: &[MonitorInfo], main: Option<usize>) -> Placement {
        if monitors.len() < 2 {
            return Placement::Normal;
        }
        let main = main.filter(|i| *i < monitors.len()).unwrap_or(0);
        let area = |m: &MonitorInfo| u64::from(m.width) * u64::from(m.height);
        let best = monitors
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != main)
            // The largest; of equals, the first.
            .max_by(|(i, a), (j, b)| area(a).cmp(&area(b)).then(j.cmp(i)))
            .map(|(i, _)| i);
        match best {
            Some(monitor) => Placement::Borderless { monitor },
            None => Placement::Normal,
        }
    }

    /// India's offset: "today" on the Board is the day in India (plan P-7).
    fn ist() -> FixedOffset {
        FixedOffset::east_opt(5 * 3600 + 30 * 60).expect("a valid offset")
    }

    /// What today's panels have cost: the sum of `cost_paise` of the panels created today. An
    /// update made today to an older panel is counted on that panel's day.
    pub fn spend_today_paise(panels: &[Panel], now: DateTime<FixedOffset>) -> u64 {
        let today = now.with_timezone(&ist()).date_naive();
        panels
            .iter()
            .filter(|p| p.created_at.with_timezone(&ist()).date_naive() == today)
            .map(|p| p.cost_paise)
            .sum()
    }

    /// Everything the Board window draws when it opens.
    #[derive(Debug, Clone, Default, PartialEq, Serialize)]
    pub struct BoardSnapshot {
        pub panels: Vec<Panel>,
        pub conversation: Vec<ConversationEntry>,
        pub spend_today_paise: u64,
    }

    pub fn snapshot(store: &BoardStore, now: DateTime<FixedOffset>) -> BoardSnapshot {
        let panels = store.panels();
        BoardSnapshot {
            spend_today_paise: spend_today_paise(&panels, now),
            conversation: store.conversation(),
            panels,
        }
    }

    /// The longest address the Board will hand to the system browser.
    pub const LINK_MAX: usize = 2048;

    /// The address to open for a link clicked in a panel, or `None`. Panel content is
    /// untrusted, and "open" on Windows runs whatever a string names (a program, a file, a
    /// settings page), so only a plain web address passes. The page checks first with the
    /// same rules (`text.ts` `linkTarget`; both are tested against `link-vectors.json`):
    /// - at most 2,048 bytes, all printable ASCII, none of `" ' < > | ^` backtick or
    ///   backslash, and every `%` followed by two hex digits;
    /// - `https://` in lower case, then a host that starts with a letter, digit or `[`;
    /// - no `@` and no `%` before the path (no name, no password, no disguised host);
    /// - and a URL parser agrees it is an https address with a host.
    pub fn openable_link(raw: &str) -> Option<String> {
        if raw.len() > LINK_MAX || !raw.bytes().all(|b| (0x21..=0x7e).contains(&b)) {
            return None;
        }
        if raw.contains(['"', '\'', '<', '>', '|', '^', '`', '\\']) {
            return None;
        }
        let bytes = raw.as_bytes();
        for (at, _) in raw.match_indices('%') {
            let hex = |i: usize| bytes.get(i).is_some_and(u8::is_ascii_hexdigit);
            if !(hex(at + 1) && hex(at + 2)) {
                return None;
            }
        }
        let rest = raw.strip_prefix("https://")?;
        let first = *rest.as_bytes().first()?;
        if !(first.is_ascii_alphanumeric() || first == b'[') {
            return None;
        }
        let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
        if authority.contains(['@', '%']) {
            return None;
        }
        let parsed = url::Url::parse(raw).ok()?;
        let plain = parsed.scheme() == "https"
            && parsed.host_str().is_some_and(|h| !h.is_empty())
            && parsed.username().is_empty()
            && parsed.password().is_none();
        plain.then(|| raw.to_owned())
    }
}
