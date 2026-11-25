//! The ServerBrain cutover (plan B Task 9): which brain answers, starting the node in server
//! mode, the enrolment commands, `forget_claude_key`, and the per-turn timing log.
//!
//! In server mode the Anthropic key is never read (`key::load` is not called): the Jarvis
//! server holds the only Claude key. Without `node.toml` nothing changes from today.

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use jarvis_core::brain::Brain;
use jarvis_node::client::{ClientConfig, LinkStatus};
use jarvis_node::folder::{FolderOps, NoFolder};
use jarvis_node::node::{self, NodeDeps};
use jarvis_node::server_brain::ServerBrain;
use tauri::{AppHandle, State};
use tokio::sync::mpsc;

use crate::approvals::{self, NodeSlot};
use crate::node_config::{BrainChoice, NodeConfig};

/// What this run of the app is answering with, for the UI and `needs_key`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mode {
    pub brain: BrainChoice,
    /// `node.toml` exists but is wrong, or the node couldn't start: shown, never a fallback.
    pub error: Option<String>,
}

impl Mode {
    pub fn server(&self) -> bool {
        self.brain == BrainChoice::Server
    }
}

/// Start the node from `cfg` (server mode) and return the brain that talks to it.
pub fn start(
    app: &AppHandle,
    cfg: &NodeConfig,
    data_dir: &Path,
    slot: &NodeSlot,
) -> Result<Arc<dyn Brain>, String> {
    let gateway = cfg.gateway.clone().ok_or("server mode needs a gateway")?;
    let keystore = keystore(data_dir)?;
    let folder = folder(cfg);
    let (ui_tx, ui_rx) = mpsc::channel(256);
    let deps = NodeDeps {
        keystore,
        ui: ui_tx,
        folder,
    };
    // node::start spawns its tasks on the runtime Tauri runs.
    let _runtime = tauri::async_runtime::handle().inner().enter();
    let handle = node::start(ClientConfig::new(gateway, data_dir.join("node")), deps)
        .map_err(|e| format!("couldn't open the node's journal: {e}"))?;
    if let Ok(mut s) = slot.lock() {
        *s = Some(handle.clone());
    }
    tauri::async_runtime::spawn(approvals::forward(app.clone(), slot.clone(), ui_rx));
    Ok(Arc::new(ServerBrain::new(Arc::new(handle))))
}

#[cfg(windows)]
fn keystore(data_dir: &Path) -> Result<Arc<dyn jarvis_node::keystore::KeyStore>, String> {
    use jarvis_node::keystore::DpapiKeyStore;
    std::fs::create_dir_all(data_dir.join("node")).map_err(|e| e.to_string())?;
    Ok(Arc::new(DpapiKeyStore::new(
        data_dir.join("node").join(DpapiKeyStore::FILE_NAME),
    )))
}

#[cfg(not(windows))]
fn keystore(_: &Path) -> Result<Arc<dyn jarvis_node::keystore::KeyStore>, String> {
    Err("server mode needs Windows (the node key is kept with DPAPI)".into())
}

fn folder(cfg: &NodeConfig) -> Arc<dyn FolderOps> {
    #[cfg(windows)]
    if let Some(path) = &cfg.test_folder {
        return Arc::new(jarvis_node::folder::windows::WindowsFolder::new(
            path.clone(),
        ));
    }
    let _ = cfg;
    Arc::new(NoFolder)
}

pub fn link_text(s: &LinkStatus) -> String {
    match s {
        LinkStatus::NotEnrolled => "not_enrolled".into(),
        LinkStatus::Connecting => "connecting".into(),
        LinkStatus::Connected => "connected".into(),
        LinkStatus::NeedsReenrol => "needs_reenrol".into(),
        LinkStatus::Failed(why) => format!("failed: {why}"),
    }
}

/// `claude`, `error: …`, or the node's link (`not_enrolled`, `connecting`, `connected`,
/// `needs_reenrol`, `failed: …`).
#[tauri::command]
pub fn node_status(mode: State<'_, Mode>, node: State<'_, NodeSlot>) -> String {
    if let Some(e) = &mode.error {
        return format!("error: {e}");
    }
    if !mode.server() {
        return "claude".into();
    }
    match node.lock().ok().and_then(|n| n.clone()) {
        Some(n) => link_text(&n.status().borrow()),
        None => "error: the node isn't running".into(),
    }
}

/// Enrol with the code from `jarvis-core node enrol`. The code is never logged.
#[tauri::command]
pub async fn enrol(code: String, node: State<'_, NodeSlot>) -> Result<(), String> {
    let n = node
        .lock()
        .ok()
        .and_then(|n| n.clone())
        .ok_or("Server mode isn't on (set brain = \"server\" in node.toml).")?;
    n.enrol(code.trim()).await.map_err(|e| e.to_string())
}

/// Remove the Anthropic key from Credential Manager (once Phase 2 is accepted).
#[tauri::command]
pub fn forget_claude_key() -> Result<(), String> {
    crate::key::delete()
}

/// When the last utterance was handed over, for the timing log.
#[derive(Clone, Default)]
pub struct TurnClock(Arc<Mutex<Option<Instant>>>);

impl TurnClock {
    /// The person's words were handed to the coordinator now.
    pub fn heard(&self) {
        if let Ok(mut t) = self.0.lock() {
            *t = Some(Instant::now());
        }
    }

    /// The first sentence of the answer is handed to the voice now: log once per turn.
    ///
    /// `transcript_to_speak_ms` runs from the coordinator receiving the transcript to the
    /// first `Speak` event. It leaves out two things the §6.2 target (3 s from the end of
    /// speech to the first audio) includes: the recogniser's time (end of speech to
    /// transcript) and the voice's time to make the first sentence's audio. Measuring those
    /// needs timestamps from jarvis-ears and jarvis-voice, which stay unchanged.
    pub fn speaking(&self) {
        let started = self.0.lock().ok().and_then(|mut t| t.take());
        if let Some(t) = started {
            let ms = t.elapsed().as_millis();
            tracing::info!(transcript_to_speak_ms = ms as u64, "turn_timing");
        }
    }

    /// Back to idle with nothing said (a silent local command): no turn to time.
    pub fn cancel(&self) {
        if let Ok(mut t) = self.0.lock() {
            *t = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_clock_logs_once_per_turn() {
        let c = TurnClock::default();
        c.speaking(); // no turn yet: nothing
        c.heard();
        assert!(c.0.lock().unwrap().is_some());
        c.speaking();
        assert!(c.0.lock().unwrap().is_none(), "taken by the first sentence");
        c.heard();
        c.cancel();
        assert!(
            c.0.lock().unwrap().is_none(),
            "a silent turn isn't timed later"
        );
    }

    #[test]
    fn link_states_have_stable_names() {
        assert_eq!(link_text(&LinkStatus::NeedsReenrol), "needs_reenrol");
        assert_eq!(link_text(&LinkStatus::Failed("x".into())), "failed: x");
    }
}
