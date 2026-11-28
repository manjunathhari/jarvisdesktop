//! The runner against a hostile stand-in engine and hostile clients: the findings of the
//! independent review of Task 2 (`cad-task-2-review.md`), one test or more for each.
//!
//! Unix only, like `replay.rs`. Nothing here runs container mode: the `kill(-1)` and the
//! emptying of /tmp are never reached from a test (they need `--container` and pid 1).
#![cfg(unix)]

mod common;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use common::*;
use data_encoding::BASE64;
use jarvis_cad_runner::{container_mode_allowed, Config};
use serde_json::{json, Value};

const PROMPT: Duration = Duration::from_secs(8);

fn secret_file(scratch: &Scratch) -> PathBuf {
    let path = scratch.file("secret.txt");
    std::fs::write(&path, "TOP-SECRET-FILE-CONTENT").unwrap();
    path
}

fn svg_text(answer: &Value) -> Option<String> {
    let svg = answer["svg_b64"].as_str()?;
    Some(String::from_utf8_lossy(&BASE64.decode(svg.as_bytes()).unwrap()).into_owned())
}

// ---- finding 6: one time limit for the whole replay

#[tokio::test]
async fn three_steps_of_0_6_s_against_a_1_s_limit_is_timeout() {
    let addr = start(config().replay_limit(Duration::from_secs(1))).await;
    let steps = json!([
        {"label": "A", "commands": [cmd("sleep", json!({"seconds": 0.6}))]},
        {"label": "B", "commands": [cmd("sleep", json!({"seconds": 0.6}))]},
        {"label": "C", "commands": [cmd("sleep", json!({"seconds": 0.6}))]},
    ]);
    let began = Instant::now();
    let answer = ask_within(addr, replay_req(steps, false), PROMPT).await;
    assert_eq!(error_of(&answer), "timeout");
    assert!(
        began.elapsed() < Duration::from_millis(1700),
        "{:?}",
        began.elapsed()
    );
}

// ---- finding 1: the saved SVG is read only after the engine is dead, without following links

#[tokio::test]
async fn a_symlink_in_place_of_the_svg_is_engine_failed() {
    let addr = start(config()).await;
    let scratch = Scratch::new();
    let secret = secret_file(&scratch);
    let req = one_step(
        vec![cmd("swap", json!({"kind": "symlink", "target": secret}))],
        true,
    );
    let answer = ask_within(addr, req, PROMPT).await;
    assert!(!answer.to_string().contains("TOP-SECRET"), "{answer}");
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn a_fifo_in_place_of_the_svg_is_engine_failed_and_the_runner_still_answers() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("swap", json!({"kind": "fifo"}))], true);
    let began = Instant::now();
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
    assert!(
        began.elapsed() < Duration::from_secs(3),
        "{:?}",
        began.elapsed()
    );
    let answer = ask_within(addr, json!({"v": 1, "op": "catalogue"}), PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    let answer = ask_within(addr, json!("garbage"), PROMPT).await;
    assert_eq!(error_of(&answer), "bad_request");
}

#[tokio::test]
async fn a_directory_in_place_of_the_svg_is_engine_failed() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("swap", json!({"kind": "dir"}))], true);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn a_helper_racing_the_svg_never_leaks_another_file_or_wedges_the_runner() {
    let addr = start(config()).await;
    let scratch = Scratch::new();
    let secret = secret_file(&scratch);
    for kind in ["symlink", "fifo", "symlink", "fifo", "symlink", "symlink"] {
        let req = one_step(
            vec![
                cmd("line", json!({})),
                cmd("race", json!({"kind": kind, "target": secret})),
            ],
            true,
        );
        let answer = ask_within(addr, req, PROMPT).await;
        assert!(!answer.to_string().contains("TOP-SECRET"), "{answer}");
        if answer["ok"] == true {
            let svg = svg_text(&answer).unwrap();
            assert!(svg.starts_with("<svg"), "{svg}");
        } else {
            assert_eq!(error_of(&answer), "engine_failed");
        }
    }
}

// ---- finding 2: the engine's whole process group is killed

#[cfg(target_os = "linux")]
#[tokio::test]
async fn a_grandchild_of_the_engine_is_killed_with_it() {
    let scratch = Scratch::new();
    let addr = start(config()).await;
    let report = scratch.file("grandchild.txt");
    let req = one_step(
        vec![cmd("spawn", json!({"seconds": 60, "report": report}))],
        false,
    );
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    let pid: u32 = wait_for_file(&report).await.trim().parse().unwrap();
    assert!(
        gone(pid).await,
        "the engine's grandchild {pid} is still running"
    );
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn a_grandchild_is_killed_on_timeout_too() {
    let scratch = Scratch::new();
    let addr = start(config().replay_limit(Duration::from_secs(1))).await;
    let report = scratch.file("grandchild.txt");
    let req = one_step(
        vec![
            cmd("spawn", json!({"seconds": 60, "report": report})),
            cmd("sleep", json!({"seconds": 30})),
        ],
        false,
    );
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(error_of(&answer), "timeout");
    let pid: u32 = wait_for_file(&report).await.trim().parse().unwrap();
    assert!(
        gone(pid).await,
        "the engine's grandchild {pid} is still running"
    );
}

#[test]
fn container_mode_needs_the_flag_and_pid_1() {
    assert!(container_mode_allowed(true, 1));
    assert!(!container_mode_allowed(true, 7));
    assert!(!container_mode_allowed(true, 4242));
    assert!(!container_mode_allowed(false, 1));
    assert!(!container_mode_allowed(false, 4242));
}

#[test]
fn the_runner_refuses_container_mode_outside_a_container() {
    // The runner started here is never pid 1 (its parent is this test).
    let scratch = Scratch::new();
    let commands = scratch.file("commands.txt");
    std::fs::write(&commands, "line\n").unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_jarvis-cad-runner"))
        .args(["--container", "--listen", "127.0.0.1:0", "--engine"])
        .arg(fake_engine())
        .arg("--commands")
        .arg(&commands)
        // /tmp, as in the image: even a broken runner that started would get no replay here
        // (so no kill and no clean-up), and is SIGKILLed below (so no SIGTERM handler either).
        .arg("--work-root")
        .arg("/tmp")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    // A runner that wrongly starts would serve for ever: give it 10 s, then fail.
    let began = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if began.elapsed() > Duration::from_secs(10) {
            let _ = child.kill();
            let _ = child.wait();
            panic!("the runner accepted --container outside a container");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(status.code(), Some(2));
    let mut err = String::new();
    std::io::Read::read_to_string(&mut child.stderr.take().unwrap(), &mut err).unwrap();
    assert!(err.contains("--container"), "{err}");
}

// ---- finding 4: the working directory is always removed, without following links

#[tokio::test]
async fn a_locked_directory_is_removed_and_a_link_out_is_not_followed() {
    let scratch = Scratch::new();
    let root = scratch.dir();
    let outside = scratch.dir();
    std::fs::write(outside.join("keep.txt"), "keep me").unwrap();
    let addr = start(config().work_root(root.clone())).await;
    let req = one_step(
        vec![
            cmd("lock", json!({"target": outside})),
            cmd("probe", json!({})),
        ],
        false,
    );
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert_eq!(answer["results"][0]["ok"], true, "{answer}");
    let seen: Value = serde_json::from_str(answer["results"][1]["text"].as_str().unwrap()).unwrap();
    let cwd = PathBuf::from(seen["cwd"].as_str().unwrap());
    assert!(
        cwd.starts_with(root.canonicalize().unwrap()),
        "{} is under {}",
        cwd.display(),
        root.display()
    );
    assert!(!cwd.exists(), "the locked directory is removed");
    assert_eq!(
        std::fs::read_dir(&root).unwrap().count(),
        0,
        "the work root is empty"
    );
    assert_eq!(
        std::fs::read_to_string(outside.join("keep.txt")).unwrap(),
        "keep me"
    );
    // And the next request runs normally.
    let answer = ask_within(addr, one_step(vec![cmd("line", json!({}))], true), PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
}

/// A chain of 20,000 directories (which overflowed the stack of the first runner) and a
/// mode-000 directory 5,000 levels down: the runner (a child process here, so a crash fails
/// this test and not the harness) answers, the directory is gone, and the next request works.
#[tokio::test]
async fn a_deep_chain_or_a_deeply_buried_locked_directory_is_removed() {
    let scratch = Scratch::new();
    let root = scratch.dir();
    let (_runner, addr) = spawn_runner(&scratch, &["--work-root".as_ref(), root.as_os_str()]).await;
    for hostile in [
        cmd("deepchain", json!({"depth": 20_000})),
        cmd("deeplock", json!({"depth": 5_100, "at": 5_000})),
    ] {
        let answer = ask_within(
            addr,
            one_step(vec![hostile.clone()], false),
            Duration::from_secs(30),
        )
        .await;
        assert_eq!(answer["ok"], true, "{hostile}: {answer}");
        assert_eq!(answer["results"][0]["ok"], true, "{hostile}: {answer}");
        let left: Vec<_> = std::fs::read_dir(&root).unwrap().collect();
        assert!(left.is_empty(), "{hostile}: left {left:?}");
        let answer = ask_within(addr, one_step(vec![cmd("line", json!({}))], true), PROMPT).await;
        assert_eq!(answer["ok"], true, "after {hostile}: {answer}");
    }
}

#[tokio::test]
async fn a_missing_work_root_is_engine_failed() {
    let scratch = Scratch::new();
    let root = scratch.dir().join("not-there");
    let addr = start(config().work_root(root)).await;
    let answer = ask_within(addr, one_step(vec![cmd("line", json!({}))], false), PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

// ---- finding 3: bounded memory

#[tokio::test]
async fn a_line_under_four_mib_is_read_and_one_over_is_engine_failed() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("bigline", json!({"bytes": 4_000_000}))], false);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    let req = one_step(vec![cmd("bigline", json!({"bytes": 4_300_000}))], false);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn sixty_four_lines_that_do_not_answer_is_engine_failed() {
    let addr = start(config()).await;
    let answer = ask_within(
        addr,
        one_step(vec![cmd("stray", json!({"n": 63}))], false),
        PROMPT,
    )
    .await;
    assert_eq!(answer["ok"], true, "{answer}");
    let answer = ask_within(
        addr,
        one_step(vec![cmd("stray", json!({"n": 64}))], false),
        PROMPT,
    )
    .await;
    assert_eq!(error_of(&answer), "engine_failed");
    // A flood of 4 MiB lines with the wrong id ends at the 64th line, not at the time limit
    // (which would answer `timeout`).
    let addr = start(config().replay_limit(Duration::from_secs(60))).await;
    let answer = ask_within(
        addr,
        one_step(vec![cmd("wrongflood", json!({}))], false),
        Duration::from_secs(50),
    )
    .await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn only_two_requests_wait_for_the_slot() {
    let addr = start(config()).await;
    let slow = tokio::spawn(ask(
        addr,
        one_step(vec![cmd("sleep", json!({"seconds": 1.5}))], false),
    ));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let quick = || one_step(vec![cmd("line", json!({}))], false);
    let first = tokio::spawn(ask(addr, quick()));
    let second = tokio::spawn(ask(addr, quick()));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let began = Instant::now();
    let third = ask_within(addr, quick(), PROMPT).await;
    assert_eq!(error_of(&third), "busy");
    assert!(
        began.elapsed() < Duration::from_millis(500),
        "{:?}",
        began.elapsed()
    );
    for task in [slow, first, second] {
        let answer = task.await.unwrap();
        assert_eq!(answer["ok"], true, "{answer}");
    }
}

#[test]
fn a_request_waits_at_most_five_seconds_for_the_slot() {
    assert_eq!(
        Config::new(fake_engine(), allowed(&[])).busy_wait_value(),
        Duration::from_secs(5)
    );
}

#[tokio::test]
async fn a_png_over_two_mib_of_base64_or_no_image_is_null() {
    let addr = start(config()).await;
    for hostile in ["bigpng", "noimage"] {
        let req = one_step(vec![cmd(hostile, json!({}))], false);
        let answer = ask_within(addr, req, PROMPT).await;
        assert_eq!(answer["ok"], true, "{answer}");
        assert!(answer["png_b64"].is_null(), "{hostile}");
    }
}

#[tokio::test]
async fn odd_content_lists_and_deep_or_wide_lines_are_handled() {
    let addr = start(config()).await;
    for hostile in ["emptyitems", "textitems", "nested", "tinystrings"] {
        let answer = ask_within(addr, one_step(vec![cmd(hostile, json!({}))], false), PROMPT).await;
        assert_eq!(answer["ok"], true, "{hostile}: {answer}");
        let text = answer["results"][0]["text"].as_str().unwrap();
        assert!(text.chars().count() <= 300, "{hostile}");
    }
}

#[tokio::test]
async fn four_mib_of_inspect_text_is_handled() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("biginspect", json!({}))], false);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert_eq!(answer["counts"], json!({"Line": 1}));
}

// ---- findings 5 and 7: the answer's shape

#[tokio::test]
async fn extents_that_overflow_are_null() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("extent", json!({"x": 1e308}))], false);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert!(answer["extents"].is_null(), "{}", answer["extents"]);
}

#[tokio::test]
async fn an_engine_version_outside_the_pattern_is_engine_failed() {
    let scratch = Scratch::new();
    let dir = scratch.dir();
    let engine = dir.join("cadcraft-badversion");
    std::os::unix::fs::symlink(fake_engine(), &engine).unwrap();
    let addr = start(Config::new(engine, allowed(&["line"]))).await;
    let answer = ask_within(addr, one_step(vec![cmd("line", json!({}))], false), PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
    let answer = ask_within(addr, json!({"v": 1, "op": "catalogue"}), PROMPT).await;
    assert_eq!(error_of(&answer), "engine_failed");
}

#[tokio::test]
async fn more_than_100000_items_is_too_large() {
    let addr = start(config()).await;
    let req = one_step(vec![cmd("manyitems", json!({}))], false);
    let answer = ask_within(addr, req, PROMPT).await;
    assert_eq!(error_of(&answer), "too_large");
}

#[tokio::test]
async fn unknown_fields_are_bad_request() {
    let addr = start(config()).await;
    for request in [
        json!({"v": 1, "op": "catalogue", "evil": 1}),
        json!({"v": 1, "op": "catalogue", "steps": []}),
        json!({"v": 1, "op": "catalogue", "picture": true}),
        json!({"v": 1, "op": "replay", "steps": [], "picture": false, "evil": 1}),
        json!({"v": 1, "op": "replay", "steps": [{"label": "a", "commands": [], "evil": 1}]}),
        json!({"v": 1, "op": "replay", "steps": [{"label": "a", "commands": [
            {"command": "line", "params": {}, "evil": 1}
        ]}]}),
        json!({"v": 1, "op": "replay"}),
        json!({"v": 1.0, "op": "catalogue"}),
        json!({"v": 2, "op": "catalogue"}),
        json!({"v": 1, "op": "paint"}),
    ] {
        let answer = ask_within(addr, request.clone(), PROMPT).await;
        assert_eq!(error_of(&answer), "bad_request", "{request}");
    }
    // `picture` may be left out.
    let answer = ask_within(
        addr,
        json!({"v": 1, "op": "replay", "steps": [{"label": "a", "commands": [cmd("line", json!({}))]}]}),
        PROMPT,
    )
    .await;
    assert_eq!(answer["ok"], true, "{answer}");
    assert!(answer["svg_b64"].is_null());
}

// ---- SIGTERM: the runner exits promptly and takes the engine with it

#[cfg(target_os = "linux")]
#[tokio::test]
async fn sigterm_stops_the_runner_and_its_engine() {
    let scratch = Scratch::new();
    let commands = scratch.file("commands.txt");
    std::fs::write(&commands, "sleep\nspawn\n").unwrap();
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let listen = format!("127.0.0.1:{port}");
    let mut runner = tokio::process::Command::new(env!("CARGO_BIN_EXE_jarvis-cad-runner"))
        .args(["--listen", &listen, "--engine"])
        .arg(fake_engine())
        .arg("--commands")
        .arg(&commands)
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let addr: std::net::SocketAddr = listen.parse().unwrap();
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(addr).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let report = scratch.file("sleep.txt");
    let grand = scratch.file("grandchild.txt");
    // The request is left pending: the runner may close the connection without an answer.
    let mut line = serde_json::to_vec(&one_step(
        vec![
            cmd("spawn", json!({"seconds": 60, "report": grand})),
            cmd("sleep", json!({"seconds": 30, "report": report})),
        ],
        false,
    ))
    .unwrap();
    line.push(b'\n');
    let mut pending = tokio::net::TcpStream::connect(addr).await.unwrap();
    tokio::io::AsyncWriteExt::write_all(&mut pending, &line)
        .await
        .unwrap();
    let engine: u32 = wait_for_file(&report)
        .await
        .lines()
        .next()
        .unwrap()
        .parse()
        .unwrap();
    let grandchild: u32 = wait_for_file(&grand).await.trim().parse().unwrap();
    let pid = runner.id().unwrap() as i32;
    // SAFETY: a signal to the runner this test started.
    assert_eq!(unsafe { libc::kill(pid, libc::SIGTERM) }, 0);
    let status = tokio::time::timeout(Duration::from_secs(3), runner.wait())
        .await
        .expect("the runner exits within 3 s of SIGTERM")
        .unwrap();
    assert!(status.success() || status.code().is_some(), "{status:?}");
    assert!(gone(engine).await, "the engine {engine} is still running");
    assert!(
        gone(grandchild).await,
        "the grandchild {grandchild} is still running"
    );
    drop(pending);
}

// ---- memory: a hostile engine and hostile clients against the runner binary

/// The runner's peak resident memory (VmHWM) while it faces the worst this test can make:
/// engine lines just under 4 MiB, a flood of wrong-id lines, 4 MiB of `inspect_drawing` text
/// of `[0,0,...]`, a PNG over the cap, and one replay running with two 1 MiB requests waiting
/// and more being refused. Set `JARVIS_CAD_RUNNER_BIN` to measure another build (release).
#[cfg(target_os = "linux")]
#[tokio::test]
async fn the_runner_stays_under_96_mb_against_a_hostile_engine_and_clients() {
    let scratch = Scratch::new();
    let bin = std::env::var_os("JARVIS_CAD_RUNNER_BIN")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_jarvis-cad-runner")));
    let commands = scratch.file("commands.txt");
    std::fs::write(&commands, FAKE_COMMANDS.join("\n")).unwrap();
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let listen = format!("127.0.0.1:{port}");
    let mut runner = tokio::process::Command::new(&bin)
        .args(["--listen", &listen, "--engine"])
        .arg(fake_engine())
        .arg("--commands")
        .arg(&commands)
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let pid = runner.id().unwrap();
    let addr: std::net::SocketAddr = listen.parse().unwrap();
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(addr).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let peak = || -> u64 {
        let status = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
        status
            .lines()
            .find_map(|l| l.strip_prefix("VmHWM:"))
            .and_then(|v| v.trim().trim_end_matches("kB").trim().parse().ok())
            .unwrap()
    };
    let limit = Duration::from_secs(20);
    let idle = peak();
    let mut report = vec![("idle", idle)];
    for (name, hostile) in [
        ("line of 4 MB", cmd("bigline", json!({"bytes": 4_150_000}))),
        ("wrong-id flood", cmd("wrongflood", json!({}))),
        ("4 MiB inspect text", cmd("biginspect", json!({}))),
        ("PNG over the cap", cmd("bigpng", json!({}))),
        ("1.39 M empty content items", cmd("emptyitems", json!({}))),
        ("150 k tiny text items", cmd("textitems", json!({}))),
        ("a line of deeply nested arrays", cmd("nested", json!({}))),
        ("a million tiny strings", cmd("tinystrings", json!({}))),
    ] {
        let answer = ask_within(addr, one_step(vec![hostile], false), limit).await;
        assert!(answer["v"] == 1, "{name}: {answer}");
        report.push((name, peak()));
    }
    // One replay holds the slot; two 1 MiB requests wait; five more are refused.
    let zeros = vec!["0"; 520_000].join(",");
    let big_params: Value = serde_json::from_str(&format!("{{\"a\":[{zeros}]}}")).unwrap();
    let big = one_step(vec![cmd("line", big_params)], false);
    assert!(serde_json::to_vec(&big).unwrap().len() < 1_048_576);
    let slow = tokio::spawn(ask(
        addr,
        one_step(vec![cmd("sleep", json!({"seconds": 2}))], false),
    ));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut waiting = Vec::new();
    for _ in 0..7 {
        waiting.push(tokio::spawn(ask(addr, big.clone())));
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let answer = slow.await.unwrap();
    assert_eq!(answer["ok"], true, "{answer}");
    let mut answered = 0;
    for task in waiting {
        if task.await.unwrap()["ok"] == true {
            answered += 1;
        }
    }
    assert_eq!(answered, 2, "two requests waited, the rest were busy");
    report.push(("1 running, 2 waiting 1 MiB requests", peak()));
    runner.kill().await.unwrap();
    for (name, kb) in &report {
        println!("VmHWM after {name}: {kb} kB");
    }
    let worst = report.iter().map(|(_, kb)| *kb).max().unwrap();
    assert!(worst < 96_000, "peak {worst} kB");
}
