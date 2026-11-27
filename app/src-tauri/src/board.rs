//! The Board window (Board slice 1, Task 6): a second window that draws what the node's
//! `BoardStore` holds.
//!
//! - The window is declared in `tauri.conf.json` (label `board`, page `board.html`), built
//!   hidden by [`create`] when the app starts (with the seal its Canvas pages need), and shown
//!   by `ensure_open`: when a panel arrives and on F9 in the
//!   main window. With two or more monitors it goes full screen on the largest one the main
//!   window is not on, opens there when the node starts, and hands the keyboard back to the
//!   main window; with one monitor it is an ordinary window that waits for a panel or F9
//!   (`jarvis_node::board::view::pick_placement`, tested there).
//! - Closing the Board hides it; JARVIS, the voice and the main window carry on. Closing the
//!   main window still ends the app, as it did when it was the only window.
//! - `on_event` passes each of the node's Board events to the window as `jarvis://board`. The
//!   panel in it is already in the store. The window draws it and calls `board_rendered`,
//!   which is what lets the node send its `RENDERED` receipt. Events that come before the
//!   page has asked for its first snapshot are held and passed on then: an event sent to a
//!   page that is not listening yet would be lost.
//! - The commands are the window's only way to change anything: `board_set_state` (a click),
//!   `board_clear_closed` (after the person confirms), `board_snapshot`, `board_open` and
//!   `board_open_link` (an `https:` link in a Text panel, opened in the system browser).
//! - **Canvas panels.** Their pages are untrusted and are never served by the app itself: the
//!   window gets an address on the loopback Canvas server (`board_canvas_address`) for a
//!   sandboxed frame, and asks `board_canvas_served` before reporting one drawn.
//!   Without a node (the app is not in server mode) there is no Board: the snapshot is empty
//!   and the other commands refuse.
//! - The store does file work under a mutex, so it is only touched through `spawn_blocking`.

use jarvis_node::board::view::{
    openable_link, pick_placement, snapshot, BoardSnapshot, MonitorInfo, Placement,
};
use jarvis_node::board::{BoardEvent, PanelState};
use jarvis_node::node::NodeHandle;
use std::sync::Mutex;
use tauri::{
    AppHandle, Emitter, Manager, State, WebviewWindow, WebviewWindowBuilder, Window, WindowEvent,
};
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

use crate::approvals::NodeSlot;

pub const WINDOW: &str = "board";
pub const EVENT: &str = "jarvis://board";
const NO_BOARD: &str = "The Board needs the Jarvis server.";

/// Events held until the page asks for its first snapshot (`Some`); after that, `None`.
static HELD: Mutex<Option<Vec<BoardEvent>>> = Mutex::new(Some(Vec::new()));
/// More than this before the page is ready and the oldest wait for their timeout instead.
const HELD_MAX: usize = 64;

fn node(slot: &NodeSlot) -> Option<NodeHandle> {
    slot.lock().ok().and_then(|n| n.clone())
}

/// Build the Board window, hidden. It is declared in `tauri.conf.json` with `create: false`
/// and built here because it shows Canvas pages, which are untrusted, and so needs what the
/// configuration file cannot give it (`jarvis_node::canvas` says why):
/// - its own browser data folder, so it can run with its own browser arguments (no outside
///   network at all, no WebRTC outside a proxy, no prerendering, no autoplay), while the main
///   window keeps the network its news wall needs;
/// - a script that runs first in every frame and removes WebRTC, sound and speech there.
pub fn create(app: &AppHandle) {
    let Some(config) = app
        .config()
        .app
        .windows
        .iter()
        .find(|w| w.label == WINDOW)
        .cloned()
    else {
        tracing::warn!("the Board window is not in the configuration");
        return;
    };
    let data = match app.path().app_local_data_dir() {
        Ok(dir) => dir.join("board-webview"),
        Err(e) => {
            tracing::warn!(error = %e, "no data folder for the Board window");
            return;
        }
    };
    let built = WebviewWindowBuilder::from_config(app, &config).and_then(|builder| {
        builder
            .data_directory(data)
            .initialization_script_for_all_frames(jarvis_node::canvas::SEAL_SCRIPT)
            .build()
    });
    if let Err(e) = built {
        tracing::warn!(error = %e, "couldn't build the Board window");
    }
}

/// Where the Board belongs now, and the monitor to fill if that is a whole one.
fn wanted(app: &AppHandle, board: &WebviewWindow) -> (Placement, Option<MonitorInfo>) {
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
    let placement = pick_placement(&monitors, main);
    let target = match placement {
        Placement::Borderless { monitor } => monitors.get(monitor).copied(),
        Placement::Normal => None,
    };
    (placement, target)
}

/// Bring the Board into sight: placed if it was hidden (or if the monitor it filled has
/// gone), restored if it was minimised, shown if it was hidden. On a second monitor the
/// keyboard goes back to the main window, whose keys must keep working.
pub fn ensure_open(app: &AppHandle) {
    let Some(board) = app.get_webview_window(WINDOW) else {
        tracing::warn!("the Board window does not exist");
        return;
    };
    let visible = board.is_visible().unwrap_or(false);
    let filling = board.is_fullscreen().unwrap_or(false);
    let (_, target) = wanted(app, &board);
    match target {
        Some(at) if !visible => {
            // The middle of the monitor: a point that is certainly on it.
            let middle = tauri::PhysicalPosition::new(
                f64::from(at.x) + f64::from(at.width) / 2.0,
                f64::from(at.y) + f64::from(at.height) / 2.0,
            );
            let _ = board.set_fullscreen_on_monitor(middle);
        }
        None if filling => {
            let _ = board.set_fullscreen(false);
        }
        _ => {}
    }
    let _ = board.unminimize();
    if visible {
        return;
    }
    if let Err(e) = board.show() {
        tracing::warn!(error = %e, "couldn't show the Board window");
    }
    if target.is_some() {
        if let Some(main) = app.get_webview_window("main") {
            let _ = main.set_focus();
        }
    }
}

/// When the node starts: the Board opens by itself only where it has a monitor of its own.
/// On one monitor it would open over the main window, so it waits for a panel or F9.
pub fn open_at_start(app: &AppHandle) {
    let Some(board) = app.get_webview_window(WINDOW) else {
        return;
    };
    if wanted(app, &board).1.is_some() {
        ensure_open(app);
    }
}

/// A Board event from the node, for the window. A panel that arrives, or one Jarvis asks the
/// person to look at, opens the Board first.
pub fn on_event(app: &AppHandle, event: BoardEvent) -> tauri::Result<()> {
    if !matches!(event, BoardEvent::State { .. }) {
        ensure_open(app);
    }
    let mut held = HELD.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(waiting) = held.as_mut() {
        if waiting.len() < HELD_MAX {
            waiting.push(event);
        }
        return Ok(());
    }
    app.emit_to(WINDOW, EVENT, event)
}

/// The page is listening: pass on what was held, in order, and hold nothing from now on.
fn release_held(app: &AppHandle) {
    let mut held = HELD.lock().unwrap_or_else(|e| e.into_inner());
    for event in held.take().unwrap_or_default() {
        if let Err(e) = app.emit_to(WINDOW, EVENT, event) {
            tracing::warn!(error = %e, "couldn't pass a held Board event to the window");
        }
    }
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
pub async fn board_snapshot(
    app: AppHandle,
    node_slot: State<'_, NodeSlot>,
) -> Result<BoardSnapshot, String> {
    let Some(node) = node(&node_slot) else {
        release_held(&app);
        return Ok(BoardSnapshot::default());
    };
    let store = node.board();
    let taken = tauri::async_runtime::spawn_blocking(move || {
        let store = store.lock().unwrap_or_else(|e| e.into_inner());
        snapshot(&store, chrono::Utc::now().fixed_offset())
    })
    .await
    .map_err(|e| e.to_string());
    // The page asks only once it listens (and it queues events until this answer arrives).
    release_held(&app);
    taken
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

/// The address of a Canvas panel's page on the app's Canvas server (`jarvis_node::canvas`,
/// which says why the page is served from the loopback). The server starts on first use.
#[tauri::command]
pub async fn board_canvas_address(
    panel_id: String,
    revision: u32,
    node_slot: State<'_, NodeSlot>,
) -> Result<String, String> {
    let id = Uuid::parse_str(&panel_id).map_err(|_| "unknown_panel".to_owned())?;
    let node = node(&node_slot).ok_or_else(|| NO_BOARD.to_owned())?;
    let server = jarvis_node::canvas::shared(node.board())
        .await
        .ok_or_else(|| "canvas_unavailable".to_owned())?;
    Ok(server.url(id, revision))
}

/// Whether the Canvas server really handed out that panel's page at that revision. The window
/// asks before it reports a Canvas panel drawn.
#[tauri::command]
pub async fn board_canvas_served(
    panel_id: String,
    revision: u32,
    node_slot: State<'_, NodeSlot>,
) -> Result<bool, String> {
    let Ok(id) = Uuid::parse_str(&panel_id) else {
        return Ok(false);
    };
    let Some(node) = node(&node_slot) else {
        return Ok(false);
    };
    Ok(jarvis_node::canvas::shared(node.board())
        .await
        .is_some_and(|server| server.take_served(id, revision)))
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

/// The person clicked a link in a Text panel: open it in the system browser. Panel content is
/// untrusted, so the address is checked here whatever the page already checked.
#[tauri::command]
pub async fn board_open_link(app: AppHandle, url: String) -> Result<(), String> {
    let Some(address) = openable_link(&url) else {
        tracing::warn!("the Board asked to open an address that is not a plain https one");
        return Err("not_openable".to_owned());
    };
    app.opener()
        .open_url(address, None::<&str>)
        .map_err(|e| e.to_string())
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
