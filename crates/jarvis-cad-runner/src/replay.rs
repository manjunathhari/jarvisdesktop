//! One replay (or catalogue) in a fresh engine, from nothing, inside the time limit.
//!
//! Ported from the slice 0 test (`spikes/cad-engine/draw_test.py`, `Engine.replay`,
//! `Engine.catalogue`, `summarise`, `find_extents`). The engine's replies are untrusted data:
//! they are parsed, checked and cut to the protocol's sizes here.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;

use data_encoding::BASE64;
use serde_json::{json, Value};

use crate::engine::{Engine, EngineError};
use crate::protocol::{
    Answer, CatalogueAnswer, Command, CommandResult, ErrorCode, ReplayAnswer, Request, Step,
    VERSION,
};

/// Request line limit, in bytes, not counting its `\n`.
pub const MAX_REQUEST_BYTES: usize = 1_048_576;
/// Commands per recipe.
pub const MAX_COMMANDS: usize = 2_000;
/// The SVG limit, in bytes.
pub const MAX_SVG_BYTES: u64 = 2_000_000;
/// The longest line read from the engine.
pub const MAX_ENGINE_LINE: usize = 8 * 1024 * 1024;
/// The longest PNG passed on, in bytes (an 800x600 render is far smaller).
pub const MAX_PNG_BYTES: usize = 4 * 1024 * 1024;
/// A command result's text: one line of at most this many characters.
pub const RESULT_TEXT_CHARS: usize = 300;
/// The drawing's texts: at most this many, each at most this many characters.
pub const MAX_TEXTS: usize = 80;
pub const TEXT_CHARS: usize = 80;

const ENGINE_NAME: &str = "cadcraft";
const SVG_NAME: &str = "drawing.svg";
const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";

/// How the runner runs: the engine, the allowed commands and the limits.
#[derive(Debug, Clone)]
pub struct Config {
    engine: PathBuf,
    allowed: BTreeSet<String>,
    replay_limit: Duration,
    busy_wait: Duration,
    read_limit: Duration,
}

impl Config {
    /// The engine binary and the allowed command ids, with the plan's limits: 10 s per replay,
    /// 10 s waiting for the slot, 5 s to read the request line.
    pub fn new(engine: PathBuf, allowed: BTreeSet<String>) -> Config {
        Config {
            engine,
            allowed,
            replay_limit: Duration::from_secs(10),
            busy_wait: Duration::from_secs(10),
            read_limit: Duration::from_secs(5),
        }
    }

    /// The time one replay may take, engine start to answer.
    pub fn replay_limit(mut self, limit: Duration) -> Config {
        self.replay_limit = limit;
        self
    }

    /// How long a request waits for the one replay slot before `busy`.
    pub fn busy_wait(mut self, wait: Duration) -> Config {
        self.busy_wait = wait;
        self
    }

    /// How long a client has to send its request line.
    pub fn read_limit(mut self, limit: Duration) -> Config {
        self.read_limit = limit;
        self
    }

    pub fn replay_limit_value(&self) -> Duration {
        self.replay_limit
    }

    pub fn busy_wait_value(&self) -> Duration {
        self.busy_wait
    }

    pub fn read_limit_value(&self) -> Duration {
        self.read_limit
    }

    pub fn allowed(&self) -> &BTreeSet<String> {
        &self.allowed
    }
}

/// Reads the commands file: one command id per line; blank lines are skipped.
pub fn load_commands(path: &Path) -> std::io::Result<BTreeSet<String>> {
    let text = std::fs::read_to_string(path)?;
    parse_commands(&text).map_err(|bad| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("not a command id: {bad:?}"),
        )
    })
}

/// The ids in a commands file's text, or the first line that is not an id.
pub fn parse_commands(text: &str) -> Result<BTreeSet<String>, String> {
    let mut ids = BTreeSet::new();
    for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let good = line.len() <= 40
            && line
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '_');
        if !good {
            return Err(line.chars().take(40).collect());
        }
        ids.insert(line.to_string());
    }
    Ok(ids)
}

/// The checks made before any engine starts: the command count, then every command id.
pub fn check(cfg: &Config, req: &Request) -> Result<(), ErrorCode> {
    if req.command_count() > MAX_COMMANDS {
        return Err(ErrorCode::TooManyCommands);
    }
    if let Request::Replay { steps, .. } = req {
        let all_allowed = steps
            .iter()
            .flat_map(|s| &s.commands)
            .all(|c| cfg.allowed.contains(&c.command));
        if !all_allowed {
            return Err(ErrorCode::NotAllowed);
        }
    }
    Ok(())
}

/// Runs one request in a fresh engine and answers it. Does not take the replay slot: the
/// listener does that (`crate::serve`).
pub async fn replay(cfg: &Config, req: Request) -> Answer {
    if let Err(code) = check(cfg, &req) {
        return Answer::failure(code);
    }
    // A new empty directory under the system temp dir, removed when this returns (and on a
    // panic). Declared before the engine, so the engine is dropped (killed) first.
    let Ok(workdir) = tempfile::Builder::new().prefix("jarvis-cad-").tempdir() else {
        return Answer::failure(ErrorCode::EngineFailed);
    };
    let mut engine: Option<Engine> = None;
    let work = run(cfg, &req, workdir.path(), &mut engine);
    let answer = match tokio::time::timeout(cfg.replay_limit, work).await {
        Ok(Ok(answer)) => answer,
        Ok(Err(code)) => Answer::failure(code),
        Err(_) => Answer::failure(ErrorCode::Timeout),
    };
    if let Some(engine) = engine.take() {
        engine.kill().await;
    }
    drop(workdir);
    answer
}

async fn run(
    cfg: &Config,
    req: &Request,
    workdir: &Path,
    slot: &mut Option<Engine>,
) -> Result<Answer, ErrorCode> {
    let started = Engine::start(&cfg.engine, workdir, MAX_ENGINE_LINE)
        .await
        .map_err(failed)?;
    let engine = slot.insert(started);
    match req {
        Request::Catalogue { .. } => catalogue(cfg, engine).await,
        Request::Replay { steps, picture, .. } => {
            replay_steps(engine, steps, *picture, workdir).await
        }
    }
}

fn failed(_: EngineError) -> ErrorCode {
    ErrorCode::EngineFailed
}

async fn catalogue(cfg: &Config, engine: &mut Engine) -> Result<Answer, ErrorCode> {
    let reply = engine
        .call("list_commands", json!({}))
        .await
        .map_err(failed)?;
    let listed: Value = serde_json::from_str(&reply.text).map_err(|_| ErrorCode::EngineFailed)?;
    let mut commands = BTreeMap::new();
    for item in listed.as_array().into_iter().flatten() {
        let Some(id) = item.get("id").and_then(Value::as_str) else {
            continue;
        };
        if cfg.allowed.contains(id) {
            let note = item.get("params").and_then(Value::as_str).unwrap_or("");
            commands.insert(id.to_string(), one_line(note, 2_000));
        }
    }
    Ok(Answer::Catalogue(CatalogueAnswer {
        v: VERSION,
        ok: true,
        engine_version: engine.version().to_string(),
        commands,
    }))
}

async fn replay_steps(
    engine: &mut Engine,
    steps: &[Step],
    picture: bool,
    workdir: &Path,
) -> Result<Answer, ErrorCode> {
    engine
        .call("new_drawing", json!({"metric": true}))
        .await
        .map_err(failed)?;
    let mut results = Vec::new();
    let mut failed_count = 0;
    for (step_no, step) in steps.iter().enumerate() {
        for (cmd_no, Command { command, params }) in step.commands.iter().enumerate() {
            let reply = engine
                .call("execute", json!({"command": command, "params": params}))
                .await
                .map_err(failed)?;
            let ok = !reply.is_error;
            failed_count += usize::from(!ok);
            results.push(CommandResult {
                step: step_no + 1,
                n: cmd_no + 1,
                command: command.clone(),
                ok,
                text: one_line(&reply.text, RESULT_TEXT_CHARS),
            });
        }
    }

    let inspected = engine
        .call("inspect_drawing", json!({}))
        .await
        .map_err(failed)?;
    let summary = summarise(&inspected.text);
    let found = engine
        .call("find_text", json!({"limit": MAX_TEXTS}))
        .await
        .map_err(failed)?;
    let texts = texts_of(&found.text);
    let rendered = engine
        .call("render", json!({"width": 800, "height": 600}))
        .await
        .map_err(failed)?;
    let png_b64 = rendered.image.as_deref().and_then(checked_png);

    let svg_b64 = if picture && failed_count == 0 {
        let path = workdir.join(SVG_NAME);
        let saved = engine
            .call("save", json!({"path": path.to_string_lossy()}))
            .await
            .map_err(failed)?;
        if saved.is_error {
            return Err(ErrorCode::EngineFailed);
        }
        let svg = read_svg(&path)?;
        let _ = std::fs::remove_file(&path);
        Some(BASE64.encode(&svg))
    } else {
        None
    };

    Ok(Answer::Replay(Box::new(ReplayAnswer {
        v: VERSION,
        ok: true,
        engine: ENGINE_NAME.into(),
        engine_version: engine.version().to_string(),
        results,
        failed: failed_count,
        counts: summary.counts,
        items: summary.items,
        extents: summary.extents,
        texts,
        png_b64,
        svg_b64,
    })))
}

/// Reads the saved SVG: a regular file (not a link the engine left there), at most the limit.
fn read_svg(path: &Path) -> Result<Vec<u8>, ErrorCode> {
    let meta = std::fs::symlink_metadata(path).map_err(|_| ErrorCode::EngineFailed)?;
    if !meta.file_type().is_file() {
        return Err(ErrorCode::EngineFailed);
    }
    if meta.len() > MAX_SVG_BYTES {
        return Err(ErrorCode::TooLarge);
    }
    let file = std::fs::File::open(path).map_err(|_| ErrorCode::EngineFailed)?;
    let mut svg = Vec::new();
    file.take(MAX_SVG_BYTES + 1)
        .read_to_end(&mut svg)
        .map_err(|_| ErrorCode::EngineFailed)?;
    if svg.len() as u64 > MAX_SVG_BYTES {
        return Err(ErrorCode::TooLarge);
    }
    if svg.is_empty() {
        return Err(ErrorCode::EngineFailed);
    }
    Ok(svg)
}

/// The PNG, decoded and encoded again, if it is base64 of a PNG within the limit.
fn checked_png(data: &str) -> Option<String> {
    if data.len() > MAX_PNG_BYTES / 3 * 4 + 4 {
        return None;
    }
    let png = BASE64.decode(data.as_bytes()).ok()?;
    (png.starts_with(PNG_SIGNATURE) && png.len() <= MAX_PNG_BYTES).then(|| BASE64.encode(&png))
}

/// One line: whitespace runs become one space, control characters go, at most `max` characters.
pub fn one_line(text: &str, max: usize) -> String {
    let mut out = String::new();
    let mut chars = 0;
    'words: for word in text.split_whitespace() {
        let mut word = word.chars().filter(|c| !c.is_control()).peekable();
        if word.peek().is_none() {
            continue;
        }
        if chars >= max {
            break;
        }
        if chars > 0 {
            out.push(' ');
            chars += 1;
        }
        for c in word {
            if chars == max {
                break 'words;
            }
            out.push(c);
            chars += 1;
        }
    }
    out.truncate(out.trim_end().len());
    out
}

/// The parts of an `inspect_drawing` reply the answer carries.
#[derive(Debug, PartialEq)]
pub struct Summary {
    pub counts: BTreeMap<String, u64>,
    pub items: u64,
    pub extents: Option<[f64; 2]>,
}

/// `summarise` in the slice 0 test: the `counts` object and the extents, checked.
pub fn summarise(inspect_text: &str) -> Summary {
    let data: Value = serde_json::from_str(inspect_text).unwrap_or(Value::Null);
    let mut counts = BTreeMap::new();
    if let Some(found) = data.get("counts").and_then(Value::as_object) {
        for (kind, n) in found.iter().take(64) {
            let (Some(n), kind) = (n.as_u64(), one_line(kind, 40)) else {
                continue;
            };
            if !kind.is_empty() {
                counts.insert(kind, n);
            }
        }
    }
    let items = counts.values().fold(0u64, |sum, n| sum.saturating_add(*n));
    let extents =
        find_extents(&data, 0).map(|(w, h)| [(w * 10.0).round() / 10.0, (h * 10.0).round() / 10.0]);
    Summary {
        counts,
        items,
        extents,
    }
}

/// `find_extents` in the slice 0 test: the first key naming "extent" whose value is a min and
/// a max point, searched through objects (not lists), depth first. `(width, height)`.
pub fn find_extents(node: &Value, depth: usize) -> Option<(f64, f64)> {
    fn point(value: &Value) -> Option<(f64, f64)> {
        let (x, y) = match value {
            Value::Object(o) => (o.get("x")?.as_f64()?, o.get("y")?.as_f64()?),
            Value::Array(a) if a.len() >= 2 => (a[0].as_f64()?, a[1].as_f64()?),
            _ => return None,
        };
        (x.is_finite() && y.is_finite()).then_some((x, y))
    }
    let object = node.as_object()?;
    if depth > 32 {
        return None;
    }
    for (key, value) in object {
        if key.to_lowercase().contains("extent") {
            let pair = match value {
                Value::Object(o) => o
                    .get("min")
                    .and_then(point)
                    .zip(o.get("max").and_then(point)),
                Value::Array(a) if a.len() == 2 => point(&a[0]).zip(point(&a[1])),
                _ => None,
            };
            if let Some((low, high)) = pair {
                let (w, h) = ((high.0 - low.0).abs(), (high.1 - low.1).abs());
                if w.is_finite() && h.is_finite() {
                    return Some((w, h));
                }
            }
        }
    }
    object.values().find_map(|v| find_extents(v, depth + 1))
}

/// The drawing's texts from a `find_text` reply: a list, or an object with a `texts` list.
pub fn texts_of(find_text: &str) -> Vec<String> {
    let data: Value = serde_json::from_str(find_text).unwrap_or(Value::Null);
    let rows = match &data {
        Value::Array(rows) => Some(rows),
        Value::Object(o) => o.get("texts").and_then(Value::as_array),
        _ => None,
    };
    rows.into_iter()
        .flatten()
        .filter_map(|r| r.get("text").and_then(Value::as_str))
        .map(|t| one_line(t, TEXT_CHARS))
        .take(MAX_TEXTS)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_line_cuts_and_cleans() {
        assert_eq!(one_line("  a\n b\t\tc  ", 300), "a b c");
        assert_eq!(one_line("abcdef", 3), "abc");
        assert_eq!(one_line("ab cd", 3), "ab");
        assert_eq!(one_line("\u{1b}[31mred", 10), "[31mred");
        assert_eq!(one_line("ééé", 2), "éé");
        assert_eq!(one_line("", 5), "");
    }

    #[test]
    fn extents_like_the_slice_0_search() {
        let data = json!({"counts": {}, "drawing": {"extents": {"min": {"x": -1, "y": 2}, "max": [9, 7]}}});
        assert_eq!(find_extents(&data, 0), Some((10.0, 5.0)));
        let data = json!({"extmin": [0, 0], "entities": [{"extents": [[0, 0], [1, 1]]}]});
        assert_eq!(find_extents(&data, 0), None, "lists are not searched");
        assert_eq!(find_extents(&json!({"extents": null}), 0), None);
        let mut deep = json!({"extents": [[0, 0], [1, 1]]});
        for _ in 0..40 {
            deep = json!({ "d": deep });
        }
        assert_eq!(find_extents(&deep, 0), None);
    }

    #[test]
    fn summary_keeps_only_whole_counts() {
        let s = summarise(
            r#"{"counts": {"Line": 5, "Text": 2, "Odd": -1, "Bad": "x", "Dimension": 2}}"#,
        );
        assert_eq!(s.items, 9);
        assert_eq!(s.counts.len(), 3);
        assert_eq!(s.extents, None);
        assert_eq!(summarise("not json").items, 0);
    }

    #[test]
    fn texts_in_both_shapes() {
        assert_eq!(
            texts_of(r#"{"count":1,"texts":[{"text":"A\nB"}]}"#),
            vec!["A B"]
        );
        assert_eq!(texts_of(r#"[{"text":"C"},{"x":1}]"#), vec!["C"]);
        assert!(texts_of("3").is_empty());
    }

    #[test]
    fn commands_file() {
        let ids = parse_commands("line\n\ntext\nlayer.new\n").unwrap();
        assert_eq!(ids.len(), 3);
        assert!(parse_commands("line\nrm -rf\n").is_err());
        let file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../server/config/cad_commands.txt");
        let ids = load_commands(&file).unwrap();
        assert_eq!(ids.len(), 37);
        assert!(ids.contains("dimlinear") && ids.contains("arraypolar"));
        assert!(!ids.contains("save") && !ids.contains("open"));
    }

    #[test]
    fn png_is_checked() {
        let good = BASE64.encode(b"\x89PNG\r\n\x1a\nrest");
        assert_eq!(checked_png(&good), Some(good.clone()));
        assert_eq!(checked_png(&BASE64.encode(b"GIF89a")), None);
        assert_eq!(checked_png("not base64!"), None);
    }
}
