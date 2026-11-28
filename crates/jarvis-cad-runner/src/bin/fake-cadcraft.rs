//! `fake-cadcraft`: a stand-in for `cadcraft-cli mcp`, for the runner's own tests only.
//!
//! It speaks the same newline-delimited JSON-RPC over stdin and stdout and offers the tools the
//! runner uses (`new_drawing`, `execute`, `inspect_drawing`, `find_text`, `render`, `save`,
//! `list_commands`). It counts the lines, texts and dimensions it is sent. Test commands:
//! `fail` fails; `sleep {seconds, report?}` sleeps (writing its pid and working directory to
//! `report` first); `probe` reports its environment and working directory; `bloat` makes the
//! saved SVG 2,000,001 bytes; `flood` makes its reply a 9 MiB line; `chatty` makes its reply
//! and the found texts long and full of line breaks.

use std::io::{BufRead, Write};

use data_encoding::BASE64;
use serde_json::{json, Value};

/// A PNG signature followed by a few bytes: enough for the runner's check.
const PNG: &[u8] = b"\x89PNG\r\n\x1a\nfake-png";

#[derive(Default)]
struct Drawing {
    lines: u64,
    dims: u64,
    texts: Vec<String>,
    bloat: bool,
    flood: bool,
    chatty: bool,
}

fn text_reply(id: &Value, text: &str, is_error: bool) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }})
}

fn execute(drawing: &mut Drawing, args: &Value) -> (String, bool) {
    let command = args["command"].as_str().unwrap_or("");
    let params = &args["params"];
    match command {
        "fail" => ("fake failure: this command always fails".into(), true),
        "line" => {
            drawing.lines += 1;
            ("Created 1 entity (Line)".into(), false)
        }
        "text" => {
            drawing
                .texts
                .push(params["text"].as_str().unwrap_or("").to_string());
            ("Created 1 entity (Text)".into(), false)
        }
        "dimlinear" => {
            drawing.dims += 1;
            ("Created 1 entity (Dimension)".into(), false)
        }
        "sleep" => {
            if let Some(report) = params["report"].as_str() {
                let cwd = std::env::current_dir().unwrap_or_default();
                let note = format!("{}\n{}\n", std::process::id(), cwd.display());
                let _ = std::fs::write(report, note);
            }
            let seconds = params["seconds"].as_f64().unwrap_or(30.0);
            std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
            ("slept".into(), false)
        }
        "probe" => {
            let cwd = std::env::current_dir().unwrap_or_default();
            let entries = std::fs::read_dir(&cwd)
                .map(|d| d.count())
                .unwrap_or(usize::MAX);
            let seen = json!({
                "env": std::env::vars_os().count(),
                "cwd": cwd.display().to_string(),
                "entries": entries,
            });
            (seen.to_string(), false)
        }
        "bloat" => {
            drawing.bloat = true;
            ("ok".into(), false)
        }
        "flood" => {
            drawing.flood = true;
            ("ok".into(), false)
        }
        "chatty" => {
            drawing.chatty = true;
            ("word\nword\t\u{1b}[31m ".repeat(200), false)
        }
        other => (format!("Unknown command: {other}"), true),
    }
}

fn tool(drawing: &mut Drawing, id: &Value, name: &str, args: &Value) -> Value {
    match name {
        "new_drawing" => text_reply(id, "New drawing (metric)", false),
        "execute" => {
            let (text, is_error) = execute(drawing, args);
            if drawing.flood {
                return text_reply(id, &"x".repeat(9 * 1024 * 1024), false);
            }
            text_reply(id, &text, is_error)
        }
        "inspect_drawing" => {
            let mut counts = serde_json::Map::new();
            for (kind, n) in [
                ("Line", drawing.lines),
                ("Text", drawing.texts.len() as u64),
                ("Dimension", drawing.dims),
            ] {
                if n > 0 {
                    counts.insert(kind.into(), json!(n));
                }
            }
            let report = json!({
                "counts": counts,
                "drawing": {"extents": {"min": {"x": 0.0, "y": 0.0}, "max": {"x": 9000.0, "y": 7000.0}}},
                "entities": [{"type": "Line", "extents": [[1, 1], [2, 2]]}],
            });
            text_reply(id, &report.to_string(), false)
        }
        "find_text" => {
            let mut texts: Vec<Value> = drawing
                .texts
                .iter()
                .map(|t| json!({"text": t, "type": "Text", "height": 200}))
                .collect();
            if drawing.chatty {
                texts = (0..100)
                    .map(|i| json!({"text": format!("{i}\n{}", "long ".repeat(40))}))
                    .collect();
            }
            let found =
                json!({"count": texts.len(), "offset": 0, "returned": texts.len(), "texts": texts});
            text_reply(id, &found.to_string(), false)
        }
        "render" => json!({"jsonrpc": "2.0", "id": id, "result": {
            "content": [{"type": "image", "mimeType": "image/png", "data": BASE64.encode(PNG)}],
        }}),
        "save" => {
            let path = args["path"].as_str().unwrap_or("");
            let mut svg = format!(
                "<svg xmlns=\"http://www.w3.org/2000/svg\"><!-- lines={} dims={} texts={} --></svg>",
                drawing.lines,
                drawing.dims,
                drawing.texts.len()
            );
            if drawing.bloat {
                svg = format!("<svg>{}</svg>", " ".repeat(2_000_001 - 11));
            }
            match std::fs::write(path, svg) {
                Ok(()) => text_reply(id, &format!("Saved {path}"), false),
                Err(e) => text_reply(id, &format!("save failed: {e}"), true),
            }
        }
        "list_commands" => {
            let list = json!([
                {"id": "line", "params": "points: [[x,y],...], closed?: bool"},
                {"id": "text", "params": "at: [x,y], text: str, height?: num"},
                {"id": "dimlinear", "params": "p1, p2, at"},
                {"id": "save", "params": "path"},
                {"id": "circle", "params": "center, radius"},
            ]);
            text_reply(id, &list.to_string(), false)
        }
        other => {
            json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": format!("no tool {other}")}})
        }
    }
}

fn main() {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    let mut drawing = Drawing::default();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(id) = message.get("id").cloned() else {
            continue; // a notification
        };
        let reply = match message["method"].as_str().unwrap_or("") {
            "initialize" => json!({"jsonrpc": "2.0", "id": id, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-cadcraft", "version": "0.0-fake"},
            }}),
            "tools/call" => {
                let params = &message["params"];
                tool(
                    &mut drawing,
                    &id,
                    params["name"].as_str().unwrap_or(""),
                    &params["arguments"],
                )
            }
            other => {
                json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": format!("no method {other}")}})
            }
        };
        // A log line first, as real servers sometimes write: the runner must skip it.
        let _ = writeln!(
            stdout,
            "{{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{{}}}}"
        );
        if writeln!(stdout, "{reply}").is_err() || stdout.flush().is_err() {
            break;
        }
    }
}
