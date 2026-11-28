//! The runner against the stand-in engine (`fake-cadcraft`), over its real TCP protocol.
//!
//! Unix only: the runner runs in a Linux container, and these tests look at the child's
//! working directory and process. The real engine is tested in `real_engine.rs`.
#![cfg(unix)]

mod common;

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use common::*;
use data_encoding::BASE64;
use jarvis_cad_runner::Config;
use serde_json::{json, Value};

#[tokio::test]
async fn replays_steps_in_order_and_reports_each_command() {
    let addr = start(config()).await;
    let steps = json!([
        {"label": "Walls", "commands": [
            cmd("line", json!({"points": [[0, 0], [9000, 0]]})),
            cmd("line", json!({"points": [[9000, 0], [9000, 7000]]})),
            cmd("text", json!({"at": [600, 6200], "text": "LIVING ROOM", "height": 200})),
        ]},
        {"label": "Dimensions", "commands": [
            cmd("dimlinear", json!({"p1": [0, 0], "p2": [9000, 0], "at": [4500, -900]})),
            cmd("text", json!({"at": [5600, 6200], "text": "BEDROOM", "height": 200})),
        ]},
    ]);
    let answer = ask(addr, replay_req(steps, true)).await;
    assert_eq!(answer["v"], 1);
    assert_eq!(answer["ok"], true, "{answer}");
    assert_eq!(answer["engine"], "cadcraft");
    assert_eq!(answer["engine_version"], "0.0-fake");
    let results = answer["results"].as_array().unwrap();
    let order: Vec<(u64, u64, &str, bool)> = results
        .iter()
        .map(|r| {
            (
                r["step"].as_u64().unwrap(),
                r["n"].as_u64().unwrap(),
                r["command"].as_str().unwrap(),
                r["ok"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        order,
        vec![
            (1, 1, "line", true),
            (1, 2, "line", true),
            (1, 3, "text", true),
            (2, 1, "dimlinear", true),
            (2, 2, "text", true),
        ]
    );
    assert!(results.iter().all(|r| r["text"].is_string()));
    assert_eq!(answer["failed"], 0);
    assert_eq!(
        answer["counts"],
        json!({"Dimension": 1, "Line": 2, "Text": 2})
    );
    assert_eq!(answer["items"], 5);
    assert_eq!(answer["extents"], json!([9000.0, 7000.0]));
    assert_eq!(answer["texts"], json!(["LIVING ROOM", "BEDROOM"]));
    let png = BASE64
        .decode(answer["png_b64"].as_str().unwrap().as_bytes())
        .unwrap();
    assert!(png.starts_with(b"\x89PNG\r\n\x1a\n"));
    let svg = BASE64
        .decode(answer["svg_b64"].as_str().unwrap().as_bytes())
        .unwrap();
    let svg = String::from_utf8(svg).unwrap();
    assert!(svg.starts_with("<svg"), "{svg}");
    assert!(svg.contains("lines=2"), "{svg}");
}

#[tokio::test]
async fn a_failed_command_is_counted_and_no_picture_is_returned() {
    let addr = start(config()).await;
    let steps = json!([{"label": "One bad", "commands": [
        cmd("line", json!({})),
        cmd("fail", json!({})),
        cmd("text", json!({"text": "KITCHEN"})),
    ]}]);
    let answer = ask(addr, replay_req(steps, true)).await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert_eq!(answer["failed"], 1);
    let results = answer["results"].as_array().unwrap();
    assert_eq!(results.len(), 3);
    assert_eq!(results[1]["ok"], false);
    assert_eq!(results[1]["command"], "fail");
    assert!(results[1]["text"]
        .as_str()
        .unwrap()
        .contains("fake failure"));
    assert_eq!(results[2]["ok"], true);
    assert!(answer["svg_b64"].is_null());
    assert!(
        answer["png_b64"].is_string(),
        "the picture of the state is still sent"
    );
}

#[tokio::test]
async fn a_command_outside_the_list_is_refused_before_the_engine_starts() {
    // An engine that does not exist: starting it would be `engine_failed`, not `not_allowed`.
    let cfg = Config::new(
        PathBuf::from("/nonexistent/cadcraft-cli"),
        allowed(&["line"]),
    );
    let addr = start(cfg).await;
    let steps = json!([{"label": "Sneaky", "commands": [
        cmd("line", json!({})),
        cmd("save", json!({"path": "/etc/passwd"})),
    ]}]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(error_of(&answer), "not_allowed");
}

#[tokio::test]
async fn more_than_2000_commands_is_refused() {
    let cfg = Config::new(
        PathBuf::from("/nonexistent/cadcraft-cli"),
        allowed(&["line"]),
    );
    let addr = start(cfg).await;
    let many: Vec<Value> = (0..1001).map(|_| cmd("line", json!({}))).collect();
    let steps = json!([
        {"label": "A", "commands": many},
        {"label": "B", "commands": many},
    ]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(error_of(&answer), "too_many_commands");

    // Exactly 2,000 is allowed through (and then fails only because there is no engine).
    let thousand: Vec<Value> = (0..1000).map(|_| cmd("line", json!({}))).collect();
    let steps = json!([
        {"label": "A", "commands": thousand},
        {"label": "B", "commands": thousand},
    ]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn a_replay_past_ten_seconds_is_killed_and_answers_timeout() {
    let scratch = Scratch::new();
    assert_eq!(
        Config::new(fake_engine(), allowed(&[])).replay_limit_value(),
        Duration::from_secs(10)
    );
    let addr = start(config().replay_limit(Duration::from_secs(1))).await;
    let report = scratch.file("sleep.txt");
    let steps = json!([{"label": "Slow", "commands": [
        cmd("sleep", json!({"seconds": 30, "report": report})),
    ]}]);
    let began = Instant::now();
    let answer = ask(addr, replay_req(steps, true)).await;
    let took = began.elapsed();
    assert_eq!(error_of(&answer), "timeout");
    assert!(took < Duration::from_secs(4), "took {took:?}");

    let text = wait_for_file(&report).await;
    let mut lines = text.lines();
    let pid: u32 = lines.next().unwrap().parse().unwrap();
    let cwd = PathBuf::from(lines.next().unwrap());
    assert!(
        !cwd.exists(),
        "the working directory is removed: {}",
        cwd.display()
    );
    #[cfg(target_os = "linux")]
    assert!(
        !Path::new(&format!("/proc/{pid}")).exists(),
        "the engine process {pid} is gone"
    );
    let _ = pid;
}

#[tokio::test]
async fn the_engine_gets_no_environment_and_an_empty_directory() {
    let addr = start(config()).await;
    let steps = json!([{"label": "Look around", "commands": [cmd("probe", json!({}))]}]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(answer["ok"], true, "{answer}");
    let seen: Value = serde_json::from_str(answer["results"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(seen["env"], 0, "no environment variables: {seen}");
    assert_eq!(seen["entries"], 0, "an empty directory: {seen}");
    let cwd = PathBuf::from(seen["cwd"].as_str().unwrap());
    let temp = std::env::temp_dir().canonicalize().unwrap();
    assert!(
        cwd.starts_with(&temp),
        "{} is under {}",
        cwd.display(),
        temp.display()
    );
    assert!(!cwd.exists(), "and it is removed afterwards");
}

#[tokio::test]
async fn a_request_line_over_one_mebibyte_is_bad_request() {
    let addr = start(config()).await;
    // No newline at all: refused at the size limit, not at the read time limit.
    let began = Instant::now();
    let answer = ask_raw(addr, &vec![b'a'; 1_048_577]).await;
    assert_eq!(error_of(&answer), "bad_request");
    assert!(began.elapsed() < Duration::from_secs(4));

    // A request of exactly 1,048,576 bytes before its newline is read whole (and parsed).
    let mut line =
        br#"{"v":1,"op":"replay","steps":[{"commands":[{"command":"line","params":{}}],"label":""#
            .to_vec();
    let close = br#""}]}"#;
    line.resize(1_048_576 - close.len(), b'x');
    line.extend_from_slice(close);
    assert_eq!(line.len(), 1_048_576);
    line.push(b'\n');
    let answer = ask_raw(addr, &line).await;
    assert_eq!(answer["ok"], true, "{answer}");
}

#[tokio::test]
async fn a_second_request_waits_then_is_busy() {
    let addr = start(config().busy_wait(Duration::from_secs(1))).await;
    let slow = tokio::spawn(ask(
        addr,
        replay_req(
            json!([{"label": "Slow", "commands": [cmd("sleep", json!({"seconds": 3}))]}]),
            false,
        ),
    ));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let began = Instant::now();
    let answer = ask(
        addr,
        replay_req(
            json!([{"label": "Quick", "commands": [cmd("line", json!({}))]}]),
            false,
        ),
    )
    .await;
    let waited = began.elapsed();
    assert_eq!(error_of(&answer), "busy");
    assert!(waited >= Duration::from_millis(900), "waited {waited:?}");
    assert!(waited < Duration::from_secs(3), "waited {waited:?}");
    let first = slow.await.unwrap();
    assert_eq!(first["ok"], true, "{first}");
}

#[tokio::test]
async fn a_slow_client_is_cut_off_and_does_not_hold_the_slot() {
    let addr = start(config().read_limit(Duration::from_secs(1))).await;
    let began = Instant::now();
    let answer = ask_raw(addr, b"{\"v\":1,\"op\":\"repl").await;
    assert_eq!(error_of(&answer), "bad_request");
    assert!(began.elapsed() < Duration::from_secs(3));
    let answer = ask(
        addr,
        replay_req(
            json!([{"label": "Next", "commands": [cmd("line", json!({}))]}]),
            false,
        ),
    )
    .await;
    assert_eq!(answer["ok"], true, "{answer}");
}

#[tokio::test]
async fn malformed_requests_are_bad_request() {
    let addr = start(config()).await;
    for line in [
        &b"not json\n"[..],
        b"{\"v\":2,\"op\":\"catalogue\"}\n",
        b"{\"v\":1,\"op\":\"paint\"}\n",
        b"{\"v\":1,\"op\":\"replay\",\"steps\":[{\"label\":\"x\",\"commands\":[{\"command\":\"line\",\"params\":[1]}]}]}\n",
        b"{\"v\":1,\"op\":\"replay\",\"steps\":\"none\"}\n",
        b"\n",
    ] {
        let answer = ask_raw(addr, line).await;
        assert_eq!(error_of(&answer), "bad_request", "{}", String::from_utf8_lossy(line));
    }
}

#[tokio::test]
async fn the_catalogue_lists_only_allowed_commands() {
    let addr = start(config()).await;
    let answer = ask(addr, json!({"v": 1, "op": "catalogue"})).await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert_eq!(answer["engine_version"], "0.0-fake");
    let commands = answer["commands"].as_object().unwrap();
    let ids: Vec<&String> = commands.keys().collect();
    assert_eq!(ids, vec!["dimlinear", "line", "text"]);
    assert_eq!(commands["line"], "points: [[x,y],...], closed?: bool");
}

#[tokio::test]
async fn an_svg_over_two_million_bytes_is_too_large() {
    let addr = start(config()).await;
    let steps = json!([{"label": "Huge", "commands": [cmd("bloat", json!({}))]}]);
    let answer = ask(addr, replay_req(steps.clone(), true)).await;
    assert_eq!(error_of(&answer), "too_large");
    // Without a picture the same recipe is fine.
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(answer["ok"], true, "{answer}");
}

#[tokio::test]
async fn an_engine_line_over_the_limit_is_engine_failed() {
    let addr = start(config()).await;
    let steps = json!([{"label": "Flood", "commands": [cmd("flood", json!({}))]}]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn the_engines_text_is_cut_to_one_short_line() {
    let addr = start(config()).await;
    let steps = json!([{"label": "Chatty", "commands": [cmd("chatty", json!({}))]}]);
    let answer = ask(addr, replay_req(steps, false)).await;
    assert_eq!(answer["ok"], true, "{answer}");
    let text = answer["results"][0]["text"].as_str().unwrap();
    assert!(text.chars().count() <= 300, "{}", text.len());
    assert!(!text.chars().any(char::is_control), "{text:?}");
    assert!(text.starts_with("word word"), "{text:?}");
    let texts = answer["texts"].as_array().unwrap();
    assert_eq!(texts.len(), 80);
    for t in texts {
        let t = t.as_str().unwrap();
        assert!(
            t.chars().count() <= 80 && !t.chars().any(char::is_control),
            "{t:?}"
        );
    }
}
