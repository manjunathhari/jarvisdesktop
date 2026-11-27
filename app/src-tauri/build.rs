/// Every command of the app, so that each gets its own permission (`allow-<name>`, with
/// dashes) and a window can call only the ones its capability lists
/// (`capabilities/default.json` for the main window, `capabilities/board.json` for the Board).
/// Without this list, any page of the app could call every command.
///
/// Keep it the same as `generate_handler!` in `src/lib.rs`; a test checks
/// (`crates/jarvis-node/tests/app_permissions.rs`).
const COMMANDS: &[&str] = &[
    "tap",
    "say",
    "quit",
    "video_failed",
    "toggle_fullscreen",
    "typed_input_enabled",
    "needs_key",
    "save_key",
    "decide_approval",
    "node_status",
    "enrol",
    "forget_claude_key",
    "build_label",
    "board_snapshot",
    "board_set_state",
    "board_rendered",
    "board_clear_closed",
    "board_open",
    "board_open_link",
    "board_canvas_address",
    "board_canvas_served",
    "board_say",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("the app's build script");
}
