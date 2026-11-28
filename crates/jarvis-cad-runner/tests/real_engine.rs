//! The runner against the real engine, `cadcraft-cli` 0.12.0.
//!
//! Skipped unless `JARVIS_CAD_ENGINE` names the engine binary. The engine is third-party code:
//! it is only ever run inside the `cad` container or on a CI runner (`.github/workflows/cad.yml`).

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use data_encoding::BASE64;
use jarvis_cad_runner::protocol::{Answer, ReplayAnswer, Request};
use jarvis_cad_runner::{load_commands, replay, Config};
use serde_json::{json, Value};

/// Task 3's allow-list for the picture check (`check_svg`, CAD board slice 1 plan).
const SVG_ELEMENTS: &[&str] = &[
    "svg", "g", "rect", "line", "polyline", "polygon", "path", "circle", "ellipse",
];
const SVG_ATTRIBUTES: &[&str] = &[
    "xmlns",
    "viewBox",
    "width",
    "height",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "d",
    "points",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-dasharray",
    "transform",
    "opacity",
    "fill-opacity",
    "stroke-opacity",
];

fn engine() -> Option<PathBuf> {
    let path = std::env::var_os("JARVIS_CAD_ENGINE")?;
    // Absolute: the engine starts in its own new directory.
    Some(std::fs::canonicalize(&path).expect("JARVIS_CAD_ENGINE names a file"))
}

fn commands_file() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../server/config/cad_commands.txt")
}

fn config(engine: PathBuf) -> Config {
    Config::new(engine, load_commands(&commands_file()).unwrap())
}

macro_rules! engine_or_skip {
    () => {
        match engine() {
            Some(path) => path,
            None => {
                eprintln!("skipped: JARVIS_CAD_ENGINE is not set");
                return;
            }
        }
    };
}

/// The canned two-room plan (`CANNED` in the slice 0 test, `spikes/cad-engine/draw_test.py`).
fn two_room_plan(picture: bool) -> Request {
    let request = json!({"v": 1, "op": "replay", "picture": picture, "steps": [
        {"label": "Whole plan", "commands": [
            {"command": "setvar", "params": {"name": "DIMSCALE", "value": 40}},
            {"command": "line", "params": {"points": [[0, 0], [9000, 0], [9000, 7000], [0, 7000]], "closed": true}},
            {"command": "line", "params": {"points": [[5000, 0], [5000, 7000]]}},
            {"command": "text", "params": {"at": [600, 6200], "text": "LIVING ROOM", "height": 200}},
            {"command": "text", "params": {"at": [5600, 6200], "text": "BEDROOM", "height": 200}},
            {"command": "dimlinear", "params": {"p1": [0, 0], "p2": [9000, 0], "at": [4500, -900]}},
            {"command": "dimlinear", "params": {"p1": [9000, 0], "p2": [9000, 7000], "at": [9900, 3500]}},
        ]},
    ]});
    Request::parse(&serde_json::to_vec(&request).unwrap()).unwrap()
}

fn replayed(answer: Answer) -> ReplayAnswer {
    match answer {
        Answer::Replay(r) => *r,
        other => panic!(
            "expected a replay answer, got {}",
            String::from_utf8_lossy(&other.to_line())
        ),
    }
}

fn svg_of(answer: &ReplayAnswer) -> Vec<u8> {
    let svg = answer.svg_b64.as_deref().expect("an SVG");
    BASE64.decode(svg.as_bytes()).unwrap()
}

#[tokio::test]
async fn draws_the_two_room_plan() {
    let engine = engine_or_skip!();
    let answer = replayed(replay(&config(engine), two_room_plan(true)).await);
    println!(
        "engine {} {}; results {}",
        answer.engine,
        answer.engine_version,
        serde_json::to_string(&answer.results).unwrap()
    );
    println!(
        "counts {:?}; items {}; extents {:?}; texts {:?}",
        answer.counts, answer.items, answer.extents, answer.texts
    );
    assert_eq!(answer.engine, "cadcraft");
    assert_eq!(answer.engine_version, "0.12.0");
    assert_eq!(answer.failed, 0, "{:?}", answer.results);
    assert_eq!(answer.results.len(), 7);
    assert_eq!(answer.counts.get("Line"), Some(&5));
    assert_eq!(answer.counts.get("Text"), Some(&2));
    assert_eq!(answer.counts.get("Dimension"), Some(&2));
    assert!(
        answer.texts.iter().any(|t| t == "LIVING ROOM"),
        "{:?}",
        answer.texts
    );
    assert!(
        answer.texts.iter().any(|t| t == "BEDROOM"),
        "{:?}",
        answer.texts
    );
    assert!(answer.extents.is_some(), "the extents are found");
    let png = BASE64
        .decode(answer.png_b64.as_deref().expect("a PNG").as_bytes())
        .unwrap();
    assert!(png.starts_with(b"\x89PNG\r\n\x1a\n"));
    let svg = svg_of(&answer);
    println!("png {} bytes; svg {} bytes", png.len(), svg.len());
    assert!(svg.len() <= 2_000_000);
}

#[tokio::test]
async fn the_same_recipe_twice_gives_the_same_svg_bytes() {
    let engine = engine_or_skip!();
    let cfg = config(engine);
    let first = svg_of(&replayed(replay(&cfg, two_room_plan(true)).await));
    let second = svg_of(&replayed(replay(&cfg, two_room_plan(true)).await));
    if first != second {
        let at = first.iter().zip(&second).position(|(a, b)| a != b);
        panic!(
            "the SVGs differ ({} and {} bytes, first difference at {:?})",
            first.len(),
            second.len(),
            at
        );
    }
}

#[tokio::test]
async fn the_svg_uses_only_these_elements_and_attributes() {
    let engine = engine_or_skip!();
    let svg = svg_of(&replayed(
        replay(&config(engine), two_room_plan(true)).await,
    ));
    let svg = String::from_utf8(svg).expect("UTF-8");
    let (elements, attributes) = names_in(&svg);
    println!("elements: {elements:?}");
    println!("attributes: {attributes:?}");
    let extra_elements: Vec<_> = elements
        .iter()
        .filter(|e| !SVG_ELEMENTS.contains(&e.as_str()))
        .collect();
    let extra_attributes: Vec<_> = attributes
        .iter()
        .filter(|a| !SVG_ATTRIBUTES.contains(&a.as_str()))
        .collect();
    assert!(
        extra_elements.is_empty(),
        "elements outside the list: {extra_elements:?}"
    );
    assert!(
        extra_attributes.is_empty(),
        "attributes outside the list: {extra_attributes:?}"
    );
    for bad in ["url(", "javascript:", "&#", "<!DOCTYPE", "<!ENTITY"] {
        assert!(!svg.contains(bad), "the SVG contains {bad}");
    }
}

#[tokio::test]
async fn the_catalogue_names_every_allowed_command() {
    let engine = engine_or_skip!();
    let cfg = config(engine);
    let answer = replay(
        &cfg,
        Request::parse(br#"{"v":1,"op":"catalogue"}"#).unwrap(),
    )
    .await;
    let line: Value = serde_json::from_slice(&answer.to_line()).unwrap();
    assert_eq!(line["ok"], true, "{line}");
    assert_eq!(line["engine_version"], "0.12.0");
    let named: BTreeSet<String> = line["commands"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    let missing: Vec<_> = cfg.allowed().difference(&named).collect();
    assert!(missing.is_empty(), "the engine does not list: {missing:?}");
    assert_eq!(&named, cfg.allowed());
}

/// The element and attribute names in an SVG, by a plain scan of its tags. The engine's SVG is
/// simple (measured: `svg`, `rect`, `polyline`, `path`); this is a check, not a parser.
fn names_in(svg: &str) -> (BTreeSet<String>, BTreeSet<String>) {
    let mut elements = BTreeSet::new();
    let mut attributes = BTreeSet::new();
    let mut rest = svg;
    while let Some(start) = rest.find('<') {
        rest = &rest[start + 1..];
        if let Some(after) = rest.strip_prefix("!--") {
            rest = after.find("-->").map_or("", |end| &after[end + 3..]);
            continue;
        }
        if rest.starts_with('?') || rest.starts_with('/') {
            rest = rest.find('>').map_or("", |end| &rest[end + 1..]);
            continue;
        }
        let end = tag_end(rest);
        let tag = &rest[..end];
        rest = rest.get(end + 1..).unwrap_or("");
        let tag = tag.trim_end_matches('/');
        let name_end = tag.find(|c: char| c.is_whitespace()).unwrap_or(tag.len());
        elements.insert(tag[..name_end].to_string());
        let mut attrs = &tag[name_end..];
        loop {
            attrs = attrs.trim_start();
            let Some(eq) = attrs.find('=') else { break };
            attributes.insert(attrs[..eq].trim().to_string());
            attrs = attrs[eq + 1..].trim_start();
            let Some(quote) = attrs.chars().next().filter(|q| *q == '"' || *q == '\'') else {
                break;
            };
            let Some(close) = attrs[1..].find(quote) else {
                break;
            };
            attrs = &attrs[close + 2..];
        }
    }
    (elements, attributes)
}

/// The index of the `>` that ends a tag, skipping quoted attribute values.
fn tag_end(tag: &str) -> usize {
    let mut quote = None;
    for (i, c) in tag.char_indices() {
        match (quote, c) {
            (None, '"' | '\'') => quote = Some(c),
            (Some(q), _) if c == q => quote = None,
            (None, '>') => return i,
            _ => {}
        }
    }
    tag.len()
}

#[test]
fn the_tag_scan_finds_names() {
    let (e, a) = names_in(
        r#"<?xml version="1.0"?><!-- a <b> --><svg xmlns="x" viewBox='0 0 1 1'><path d="M0 0 L1>1"/><g><rect x="1" /></g></svg>"#,
    );
    assert_eq!(e, ["g", "path", "rect", "svg"].map(String::from).into());
    assert_eq!(a, ["d", "viewBox", "x", "xmlns"].map(String::from).into());
}
