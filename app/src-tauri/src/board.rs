//! The Board window (Board slice 1, Task 6): a second window that draws what the node's
//! `BoardStore` holds.
//!
//! - The window is declared in `tauri.conf.json` (label `board`, page `board.html`), created
//!   hidden with the app, and shown by `ensure_open`: when the node starts, when a panel
//!   arrives, and on F9 in the main window. With two or more monitors it goes full screen on
//!   the largest one the main window is not on; with one it is an ordinary window
//!   (`jarvis_node::board::view::pick_placement`, tested there).
//! - Closing the Board hides it; JARVIS, the voice and the main window carry on. Closing the
//!   main window still ends the app, as it did when it was the only window.
//! - `on_event` passes each of the node's Board events to the window as `jarvis://board`. The
//!   panel in it is already in the store. The window draws it and calls `board_rendered`,
//!   which is what lets the node send its `RENDERED` receipt.
//! - The commands are the window's only way to change anything: `board_set_state` (a click),
//!   `board_clear_closed` (after the person confirms), `board_snapshot` and `board_open`.
//!   Without a node (the app is not in server mode) there is no Board: the snapshot is empty
//!   and the other commands refuse.
//! - The store does file work under a mutex, so it is only touched through `spawn_blocking`.

use jarvis_node::board::view::{pick_placement, snapshot, BoardSnapshot, MonitorInfo, Placement};
use jarvis_node::board::{BoardEvent, PanelState};
use jarvis_node::node::NodeHandle;
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow, Window, WindowEvent};
use uuid::Uuid;

use crate::approvals::NodeSlot;

pub const WINDOW: &str = "board";
pub const EVENT: &str = "jarvis://board";
const NO_BOARD: &str = "The Board needs the Jarvis server.";

fn node(slot: &NodeSlot) -> Option<NodeHandle> {
    slot.lock().ok().and_then(|n| n.clone())
}

/// Show the Board window if it is hidden, placing it first.
pub fn ensure_open(app: &AppHandle) {
    let Some(board) = app.get_webview_window(WINDOW) else {
        tracing::warn!("the Board window does not exist");
        return;
    };
    if board.is_visible().unwrap_or(false) {
        return;
    }
    place(app, &board);
    if let Err(e) = board.show() {
        tracing::warn!(error = %e, "couldn't show the Board window");
    }
}

/// Full screen on the other monitor, or an ordinary window if there is only one.
fn place(app: &AppHandle, board: &WebviewWindow) {
    let monitors: Vec<MonitorInfo> = board
        .available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| MonitorInfo {
            x: m.position().x,
            y: m.position().y,
            width: m.size().width,
            height: m.size().height,
        })
        .collect();
    let main = app
        .get_webview_window("main")
        .and_then(|w| w.current_monitor().ok().flatten())
        .and_then(|on| {
            monitors
                .iter()
                .position(|m| m.x == on.position().x && m.y == on.position().y)
        });
    // Leave full screen before moving: a full-screen window does not change monitor.
    let _ = board.set_fullscreen(false);
    if let Placement::Borderless { monitor } = pick_placement(&monitors, main) {
        let at = monitors[monitor];
        let _ = board.set_position(tauri::PhysicalPosition::new(at.x, at.y));
        let _ = board.set_fullscreen(true);
    }
}

/// A Board event from the node, for the window. A panel that arrives, or one Jarvis asks the
/// person to look at, opens the Board first.
pub fn on_event(app: &AppHandle, event: BoardEvent) -> tauri::Result<()> {
    if !matches!(event, BoardEvent::State { .. }) {
        ensure_open(app);
    }
    app.emit_to(WINDOW, EVENT, event)
}

/// Closing the Board hides it. Closing the main window ends the app, as before the Board (a
/// hidden second window would otherwise keep it running).
pub fn on_window_event(window: &Window, event: &WindowEvent) {
    match (window.label(), event) {
        (WINDOW, WindowEvent::CloseRequested { api, .. }) => {
            api.prevent_close();
            let _ = window.hide();
        }
        ("main", WindowEvent::Destroyed) => window.app_handle().exit(0),
        _ => {}
    }
}

fn state_from(word: &str) -> Option<PanelState> {
    match word {
        "open" => Some(PanelState::Open),
        "minimised" => Some(PanelState::Minimised),
        "maximised" => Some(PanelState::Maximised),
        "closed" => Some(PanelState::Closed),
        _ => None,
    }
}

/// F9 in the main window, or the Board's own request: show the Board.
#[tauri::command]
pub fn board_open(app: AppHandle) {
    ensure_open(&app);
}

/// What the window draws when it opens: the panels, the conversation and today's spend.
#[tauri::command]
pub async fn board_snapshot(node_slot: State<'_, NodeSlot>) -> Result<BoardSnapshot, String> {
    let Some(node) = node(&node_slot) else {
        return Ok(BoardSnapshot::default());
    };
    let store = node.board();
    tauri::async_runtime::spawn_blocking(move || {
        let store = store.lock().unwrap_or_else(|e| e.into_inner());
        snapshot(&store, chrono::Utc::now().fixed_offset())
    })
    .await
    .map_err(|e| e.to_string())
}

/// The person minimised, maximised, restored, closed or reopened a panel by hand.
#[tauri::command]
pub async fn board_set_state(
    panel_id: String,
    state: String,
    node_slot: State<'_, NodeSlot>,
) -> Result<(), String> {
    let id = Uuid::parse_str(&panel_id).map_err(|_| "unknown_panel".to_owned())?;
    let state = state_from(&state).ok_or_else(|| "unknown_state".to_owned())?;
    let node = node(&node_slot).ok_or_else(|| NO_BOARD.to_owned())?;
    node.board_state(id, state).await.map_err(str::to_owned)
}

/// The window drew this panel at this revision (`ok`), or could not.
#[tauri::command]
pub async fn board_rendered(
    panel_id: String,
    revision: u32,
    ok: bool,
    node_slot: State<'_, NodeSlot>,
) -> Result<(), String> {
    let id = Uuid::parse_str(&panel_id).map_err(|_| "unknown_panel".to_owned())?;
    let node = node(&node_slot).ok_or_else(|| NO_BOARD.to_owned())?;
    node.board_rendered(id, revision, ok).await;
    Ok(())
}

/// Remove the closed panels from this desktop's copy (the person confirmed). The server keeps
/// them. Returns how many went.
#[tauri::command]
pub async fn board_clear_closed(node_slot: State<'_, NodeSlot>) -> Result<usize, String> {
    let node = node(&node_slot).ok_or_else(|| NO_BOARD.to_owned())?;
    let store = node.board();
    tauri::async_runtime::spawn_blocking(move || {
        let mut store = store.lock().unwrap_or_else(|e| e.into_inner());
        store.clear_closed().map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_words_are_the_contracts() {
        for state in [
            PanelState::Open,
            PanelState::Minimised,
            PanelState::Maximised,
            PanelState::Closed,
        ] {
            assert_eq!(state_from(state.as_str()), Some(state));
        }
        assert_eq!(state_from("hidden"), None);
    }
}
