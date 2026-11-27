//! Which window may call which command of the app. The app crate cannot be built off Windows,
//! and a slip here would only show as a button that silently does nothing, so the lists are
//! checked against each other here: the commands the app registers, the commands the build
//! declares, what each window is allowed, and what each window's pages actually call.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

fn app() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../app")
}

fn read(path: impl AsRef<Path>) -> String {
    std::fs::read_to_string(app().join(path.as_ref())).expect("an app file")
}

/// The identifiers between `open` and the next `]`, last path segment of each.
fn list_after(text: &str, open: &str) -> BTreeSet<String> {
    let rest = text.split(open).nth(1).expect("the list");
    rest.split(']')
        .next()
        .unwrap()
        .split(',')
        .map(|item| {
            item.trim()
                .trim_matches('"')
                .rsplit("::")
                .next()
                .unwrap()
                .to_owned()
        })
        .filter(|item| !item.is_empty())
        .collect()
}

fn allowed(capability: &str) -> BTreeSet<String> {
    let json: serde_json::Value =
        serde_json::from_str(&read(format!("src-tauri/capabilities/{capability}.json"))).unwrap();
    json["permissions"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|p| p.as_str()?.strip_prefix("allow-"))
        .map(|command| command.replace('-', "_"))
        .collect()
}

/// Every command the pages under `dir` call: `invoke("name"` and `invoke<T>("name"`. Tests
/// are left out; `skip` names a sub-folder that belongs to the other window. A call whose
/// command is not a plain string would escape this check, so it is an error.
fn invoked(dir: &str, skip: Option<&str>) -> BTreeSet<String> {
    fn walk(dir: &Path, skip: Option<&str>, found: &mut BTreeSet<String>) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            if path.is_dir() {
                if Some(name.as_str()) != skip {
                    walk(&path, None, found);
                }
                continue;
            }
            if !name.ends_with(".ts") || name.ends_with(".test.ts") {
                continue;
            }
            let text = std::fs::read_to_string(&path).unwrap();
            for piece in text.split("invoke").skip(1) {
                let piece = piece.trim_start();
                let call = match piece.strip_prefix('<') {
                    Some(typed) => typed.split_once('>').map_or("", |(_, rest)| rest),
                    None => piece,
                };
                let Some(args) = call.strip_prefix('(') else {
                    continue; // the word in an import or a comment, not a call
                };
                let command = args.trim_start().strip_prefix('"').unwrap_or_else(|| {
                    panic!("{name}: invoke() with a command that is not a plain string")
                });
                found.insert(command.split('"').next().unwrap().to_owned());
            }
        }
    }
    let mut found = BTreeSet::new();
    walk(&app().join(dir), skip, &mut found);
    found
}

#[test]
fn the_build_declares_exactly_the_commands_the_app_registers() {
    let registered = list_after(&read("src-tauri/src/lib.rs"), "generate_handler![");
    let declared = list_after(&read("src-tauri/build.rs"), "COMMANDS: &[&str] = &[");
    assert!(registered.len() >= 20, "{registered:?}");
    assert_eq!(declared, registered);
}

#[test]
fn every_command_is_allowed_to_exactly_one_window() {
    let registered = list_after(&read("src-tauri/src/lib.rs"), "generate_handler![");
    let (main, board) = (allowed("default"), allowed("board"));
    let both: Vec<_> = main.intersection(&board).collect();
    assert!(both.is_empty(), "allowed to both windows: {both:?}");
    let either: BTreeSet<String> = main.union(&board).cloned().collect();
    assert_eq!(
        either, registered,
        "a command no window may call, or one that does not exist"
    );
}

#[test]
fn each_window_is_allowed_everything_its_pages_call() {
    let (main, board) = (allowed("default"), allowed("board"));
    let main_calls = invoked("src", Some("board"));
    let board_calls = invoked("src/board", None);
    assert!(main_calls.len() >= 10 && board_calls.len() >= 8);
    let missing: Vec<_> = main_calls.difference(&main).collect();
    assert!(
        missing.is_empty(),
        "the main window calls but may not: {missing:?}"
    );
    let missing: Vec<_> = board_calls.difference(&board).collect();
    assert!(
        missing.is_empty(),
        "the Board calls but may not: {missing:?}"
    );
}

#[test]
fn the_board_window_has_only_board_commands() {
    let board = allowed("board");
    for command in &board {
        assert!(command.starts_with("board_"), "{command}");
    }
    // Opening the Board is the main window's (F9); the Board has no use for it.
    assert!(!board.contains("board_open"));
    assert!(allowed("default").contains("board_open"));
    // Not having `decide_approval` would mean little if the typing box could decide: the
    // app's `board_say` is told it may not, and `from_board` turns every decision away.
    assert!(
        read("src-tauri/src/lib.rs").contains("say_text(text, false, &jarvis"),
        "board_say must not decide"
    );
    assert!(read("src-tauri/src/approvals.rs").contains(
        "Intercept::Decide(..) | Intercept::AskToTap | Intercept::NotHere => Intercept::NotHere"
    ));
    for never in [
        "quit",
        "say",
        "save_key",
        "enrol",
        "forget_claude_key",
        "decide_approval",
    ] {
        assert!(!board.contains(never), "{never}");
    }
    // Both windows may listen for events, and nothing else of the framework's.
    for capability in ["default", "board"] {
        let json: serde_json::Value =
            serde_json::from_str(&read(format!("src-tauri/capabilities/{capability}.json")))
                .unwrap();
        let framework: Vec<&str> = json["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|p| p.as_str())
            .filter(|p| !p.starts_with("allow-"))
            .collect();
        assert_eq!(
            framework,
            ["core:event:allow-listen", "core:event:allow-unlisten"]
        );
    }
}
