//! `fake-cadcraft`: a stand-in for `cadcraft-cli mcp`, for the runner's own tests only.
//!
//! It speaks the same newline-delimited JSON-RPC over stdin and stdout and offers the tools the
//! runner uses (`new_drawing`, `execute`, `inspect_drawing`, `find_text`, `render`, `save`,
//! `list_commands`). It counts the lines, texts and dimensions it is sent.
//!
//! Ordinary test commands: `fail` fails; `sleep {seconds, report?}` sleeps (writing its pid and
//! working directory to `report` first); `probe` reports its environment and working directory;
//! `bloat` makes the saved SVG 2,000,001 bytes; `flood` makes its reply a 9 MiB line; `chatty`
//! makes its reply and the found texts long and full of line breaks.
//!
//! Hostile test commands (the independent review of Task 2):
//! - `spawn {seconds, report, session?}`: starts a grandchild that sleeps (with `session`, in a
//!   session of its own, which only container mode's `kill(-1)` reaches), and writes its pid
//!   to `report`;
//! - `swap {kind, target?}`: after `save`, puts a symlink (to `target`), a FIFO or a directory
//!   where the saved SVG was;
//! - `race {kind, target?}`: after `save`, starts a helper that keeps swapping the SVG with a
//!   symlink or a FIFO;
//! - `lock {target}`: leaves `sub/` with mode 000 holding a file and a symlink to `target`, and
//!   a symlink `escape` to `target`;
//! - `extent {x}`: reports the drawing's maximum x as `x`;
//! - `stray {n}`: writes `n` lines that do not answer before its reply (the usual log line
//!   included);
//! - `bigline {bytes}`: its reply line carries a `pad` array of zeros about `bytes` long;
//! - `wrongflood`: writes 4 MiB lines with the wrong id for ever;
//! - `biginspect`: `inspect_drawing` answers about 4 MiB of `[0,0,...]` text;
//! - `bigpng`: `render` answers an image of over 2 MiB of base64; `noimage`: no image;
//! - `manyitems`: `inspect_drawing` counts 100,001 lines.
//!
//! Started with a program name containing `badversion`, it reports version `0.12.0 (abc)`.

use std::io::{BufRead, Write};
use std::time::{Duration, Instant};

use data_encoding::BASE64;
use serde_json::{json, Value};

/// A PNG signature followed by a few bytes: enough for the runner's check.
const PNG: &[u8] = b"\x89PNG\r\n\x1a\nfake-png";
/// Just under the runner's 4 MiB engine line limit, with room for the envelope.
const NEAR_LINE_LIMIT: usize = 4 * 1024 * 1024 - 200;

#[derive(Default)]
struct Drawing {
    lines: u64,
    dims: u64,
    texts: Vec<String>,
    bloat: bool,
    flood: bool,
    chatty: bool,
    swap: Option<(String, String)>,
    race: Option<(String, String)>,
    extent_x: Option<f64>,
    stray: usize,
    bigline: usize,
    wrongflood: bool,
    biginspect: bool,
    bigpng: bool,
    noimage: bool,
    manyitems: bool,
}

fn text_reply(id: &Value, text: &str, is_error: bool) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }})
}

fn zeros(bytes: usize) -> String {
    let mut s = String::with_capacity(bytes + 2);
    s.push('[');
    while s.len() + 3 < bytes {
        s.push_str("0,");
    }
    s.push_str("0]");
    s
}

fn spawn_self(args: &[&str]) -> std::io::Result<std::process::Child> {
    spawn_self_in(args, false)
}

/// Starts this program again; with `session`, in a session (and process group) of its own,
/// out of reach of a kill of the engine's group.
fn spawn_self_in(args: &[&str], session: bool) -> std::io::Result<std::process::Child> {
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .args(args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(unix)]
    if session {
        use std::os::unix::process::CommandExt;
        // SAFETY: `setsid` is async-signal-safe.
        unsafe {
            command.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    #[cfg(not(unix))]
    let _ = session;
    command.spawn()
}

fn kind_and_target(params: &Value) -> (String, String) {
    (
        params["kind"].as_str().unwrap_or("symlink").to_string(),
        params["target"]
            .as_str()
            .unwrap_or("/etc/hostname")
            .to_string(),
    )
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
            std::thread::sleep(Duration::from_secs_f64(seconds));
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
        "spawn" => {
            let seconds = params["seconds"].as_f64().unwrap_or(30.0).to_string();
            let session = params["session"].as_bool().unwrap_or(false);
            match spawn_self_in(&["sleeper", &seconds], session) {
                Ok(child) => {
                    if let Some(report) = params["report"].as_str() {
                        let _ = std::fs::write(report, format!("{}\n", child.id()));
                    }
                    // Not waited for: the grandchild is left running.
                    std::mem::forget(child);
                    ("spawned".into(), false)
                }
                Err(e) => (format!("spawn failed: {e}"), true),
            }
        }
        "swap" => {
            drawing.swap = Some(kind_and_target(params));
            ("ok".into(), false)
        }
        "race" => {
            drawing.race = Some(kind_and_target(params));
            ("ok".into(), false)
        }
        "lock" => match lock(params["target"].as_str().unwrap_or("/")) {
            Ok(()) => ("locked".into(), false),
            Err(e) => (format!("lock failed: {e}"), true),
        },
        "extent" => {
            drawing.extent_x = params["x"].as_f64();
            ("ok".into(), false)
        }
        "stray" => {
            drawing.stray = params["n"].as_u64().unwrap_or(0) as usize;
            ("ok".into(), false)
        }
        "bigline" => {
            drawing.bigline = params["bytes"].as_u64().unwrap_or(1_000_000) as usize;
            ("ok".into(), false)
        }
        "wrongflood" => {
            drawing.wrongflood = true;
            ("ok".into(), false)
        }
        "biginspect" => {
            drawing.biginspect = true;
            ("ok".into(), false)
        }
        "bigpng" => {
            drawing.bigpng = true;
            ("ok".into(), false)
        }
        "noimage" => {
            drawing.noimage = true;
            ("ok".into(), false)
        }
        "manyitems" => {
            drawing.manyitems = true;
            ("ok".into(), false)
        }
        other => (format!("Unknown command: {other}"), true),
    }
}

#[cfg(unix)]
fn lock(target: &str) -> std::io::Result<()> {
    use std::os::unix::fs::{symlink, PermissionsExt};
    std::fs::create_dir_all("sub/deeper")?;
    std::fs::write("sub/deeper/blob", vec![b'x'; 100_000])?;
    symlink(target, "sub/out")?;
    symlink(target, "escape")?;
    std::fs::set_permissions("sub/deeper", std::fs::Permissions::from_mode(0o000))?;
    std::fs::set_permissions("sub", std::fs::Permissions::from_mode(0o000))?;
    Ok(())
}

#[cfg(not(unix))]
fn lock(_target: &str) -> std::io::Result<()> {
    Err(std::io::Error::other("unix only"))
}

/// Puts a symlink, a FIFO or a directory at `path` (replacing what is there).
#[cfg(unix)]
fn plant(kind: &str, target: &str, path: &str) -> std::io::Result<()> {
    let alt = format!("{path}.alt");
    let _ = std::fs::remove_file(&alt);
    match kind {
        "symlink" => std::os::unix::fs::symlink(target, &alt)?,
        "fifo" => {
            let c = std::ffi::CString::new(alt.clone()).map_err(std::io::Error::other)?;
            // SAFETY: a valid NUL-terminated path.
            if unsafe { libc::mkfifo(c.as_ptr(), 0o600) } != 0 {
                return Err(std::io::Error::last_os_error());
            }
        }
        _ => std::fs::create_dir(&alt)?,
    }
    if kind == "dir" {
        std::fs::remove_file(path)?;
    }
    std::fs::rename(&alt, path)
}

#[cfg(not(unix))]
fn plant(_kind: &str, _target: &str, _path: &str) -> std::io::Result<()> {
    Err(std::io::Error::other("unix only"))
}

/// The `race` helper: swaps `drawing.svg` between a regular file and a symlink or a FIFO, for
/// at most 30 s (the runner should have killed it long before).
fn racer(kind: &str, target: &str) {
    let began = Instant::now();
    while began.elapsed() < Duration::from_secs(30) {
        let _ = plant(kind, target, "drawing.svg");
        let _ = std::fs::write("regular.tmp", "<svg>regular</svg>");
        let _ = std::fs::rename("regular.tmp", "drawing.svg");
    }
}

fn inspect(drawing: &Drawing) -> String {
    if drawing.biginspect {
        return format!(
            "{{\"counts\":{{\"Line\":1}},\"pad\":{}}}",
            zeros(NEAR_LINE_LIMIT - 100)
        );
    }
    let mut counts = serde_json::Map::new();
    let lines = if drawing.manyitems {
        100_001
    } else {
        drawing.lines
    };
    for (kind, n) in [
        ("Line", lines),
        ("Text", drawing.texts.len() as u64),
        ("Dimension", drawing.dims),
    ] {
        if n > 0 {
            counts.insert(kind.into(), json!(n));
        }
    }
    let max_x = drawing.extent_x.unwrap_or(9000.0);
    json!({
        "counts": counts,
        "drawing": {"extents": {"min": {"x": 0.0, "y": 0.0}, "max": {"x": max_x, "y": 7000.0}}},
        "entities": [{"type": "Line", "extents": [[1, 1], [2, 2]]}],
    })
    .to_string()
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
        "inspect_drawing" => text_reply(id, &inspect(drawing), false),
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
        "render" => {
            if drawing.noimage {
                return text_reply(id, "rendered nothing", false);
            }
            let data = if drawing.bigpng {
                let mut png = PNG.to_vec();
                png.resize(1_600_000, 0);
                BASE64.encode(&png)
            } else {
                BASE64.encode(PNG)
            };
            json!({"jsonrpc": "2.0", "id": id, "result": {
                "content": [{"type": "image", "mimeType": "image/png", "data": data}],
            }})
        }
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
            if let Err(e) = std::fs::write(path, svg) {
                return text_reply(id, &format!("save failed: {e}"), true);
            }
            if let Some((kind, target)) = &drawing.swap {
                if let Err(e) = plant(kind, target, path) {
                    return text_reply(id, &format!("swap failed: {e}"), true);
                }
            }
            if let Some((kind, target)) = &drawing.race {
                if let Ok(child) = spawn_self(&["racer", kind, target]) {
                    std::mem::forget(child);
                    std::thread::sleep(Duration::from_millis(30));
                }
            }
            text_reply(id, &format!("Saved {path}"), false)
        }
        "list_commands" => {
            let list = json!([
                {"id": "line", "params": "points: [[x,y],...], closed?: bool"},
                {"id": "text", "params": "at: [x,y], text: str, height?: num"},
                {"id": "dimlinear", "params": "p1, p2, at"},
                {"id": "save", "params": "path"},
                {"id": "circle", "params": "center, radius"},
                {"id": 7, "params": "an id that is not a string"},
            ]);
            text_reply(id, &list.to_string(), false)
        }
        other => {
            json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": format!("no tool {other}")}})
        }
    }
}

fn main() {
    let mut args = std::env::args();
    let program = args.next().unwrap_or_default();
    match args.next().as_deref() {
        Some("sleeper") => {
            let seconds: f64 = args.next().and_then(|s| s.parse().ok()).unwrap_or(30.0);
            std::thread::sleep(Duration::from_secs_f64(seconds));
            return;
        }
        Some("racer") => {
            let kind = args.next().unwrap_or_default();
            let target = args.next().unwrap_or_default();
            racer(&kind, &target);
            return;
        }
        _ => {}
    }
    let version = if program.contains("badversion") {
        "0.12.0 (abc)"
    } else {
        "0.0-fake"
    };
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
                "serverInfo": {"name": "fake-cadcraft", "version": version},
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
        if drawing.wrongflood {
            let junk = format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":999999,\"pad\":{}}}\n",
                zeros(NEAR_LINE_LIMIT)
            );
            loop {
                if stdout.write_all(junk.as_bytes()).is_err() || stdout.flush().is_err() {
                    return;
                }
            }
        }
        // A log line first, as real servers sometimes write: the runner must skip it. With
        // `stray n`, n lines in all that do not answer.
        let strays = std::mem::take(&mut drawing.stray).max(1);
        for k in 0..strays {
            let _ = if k % 2 == 0 {
                writeln!(
                    stdout,
                    "{{\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{{}}}}"
                )
            } else {
                writeln!(
                    stdout,
                    "{{\"jsonrpc\":\"2.0\",\"id\":424242,\"result\":{{}}}}"
                )
            };
        }
        let mut reply = reply.to_string();
        let pad = std::mem::take(&mut drawing.bigline);
        if pad > 0 {
            // `{"pad":[0,...],` in front of the reply's own fields.
            reply = format!("{{\"pad\":{},{}", zeros(pad), &reply[1..]);
        }
        if writeln!(stdout, "{reply}").is_err() || stdout.flush().is_err() {
            break;
        }
    }
}
