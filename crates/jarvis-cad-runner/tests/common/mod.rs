//! Helpers shared by the tests against the stand-in engine.
#![allow(dead_code)]

use std::collections::BTreeSet;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

use jarvis_cad_runner::{serve, Config};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};

pub fn fake_engine() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_fake-cadcraft"))
}

pub fn allowed(ids: &[&str]) -> BTreeSet<String> {
    ids.iter().map(|s| s.to_string()).collect()
}

/// Every test command the stand-in knows.
pub const FAKE_COMMANDS: &[&str] = &[
    "line",
    "text",
    "dimlinear",
    "fail",
    "sleep",
    "probe",
    "bloat",
    "flood",
    "chatty",
    "spawn",
    "swap",
    "race",
    "lock",
    "extent",
    "stray",
    "bigline",
    "wrongflood",
    "biginspect",
    "bigpng",
    "noimage",
    "manyitems",
    "emptyitems",
    "textitems",
    "nested",
    "tinystrings",
    "deepchain",
    "deeplock",
];

pub fn config() -> Config {
    Config::new(fake_engine(), allowed(FAKE_COMMANDS))
}

/// Starts the runner on a thread and runtime of its own, never joined: if a broken runner
/// blocks (a FIFO opened without `O_NONBLOCK`, say), the test's own runtime is not stuck and its
/// timeouts still fail the test instead of hanging it.
pub async fn start(cfg: Config) -> SocketAddr {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async move {
            let listener = TcpListener::from_std(listener).unwrap();
            serve(listener, cfg).await;
        });
    });
    addr
}

/// Sends raw bytes and reads the one answer line.
pub async fn ask_raw(addr: SocketAddr, bytes: &[u8]) -> Value {
    let stream = TcpStream::connect(addr).await.unwrap();
    let (read, mut write) = stream.into_split();
    let bytes = bytes.to_vec();
    let writer = tokio::spawn(async move {
        let _ = write.write_all(&bytes).await;
        // Keep the write half open until the answer is read: a closed half is not a signal.
        tokio::time::sleep(Duration::from_secs(60)).await;
        drop(write);
    });
    let mut line = String::new();
    BufReader::new(read).read_line(&mut line).await.unwrap();
    writer.abort();
    assert!(line.ends_with('\n'), "answer must be one line: {line:?}");
    serde_json::from_str(&line).unwrap()
}

pub async fn ask(addr: SocketAddr, request: Value) -> Value {
    let mut line = serde_json::to_vec(&request).unwrap();
    line.push(b'\n');
    ask_raw(addr, &line).await
}

/// `ask`, failing the test if no answer comes within `limit`.
pub async fn ask_within(addr: SocketAddr, request: Value, limit: Duration) -> Value {
    match tokio::time::timeout(limit, ask(addr, request)).await {
        Ok(answer) => answer,
        Err(_) => panic!("no answer within {limit:?}"),
    }
}

pub fn cmd(command: &str, params: Value) -> Value {
    json!({"command": command, "params": params})
}

pub fn replay_req(steps: Value, picture: bool) -> Value {
    json!({"v": 1, "op": "replay", "steps": steps, "picture": picture})
}

/// A one-step replay of these commands.
pub fn one_step(commands: Vec<Value>, picture: bool) -> Value {
    replay_req(json!([{"label": "Test", "commands": commands}]), picture)
}

pub fn error_of(answer: &Value) -> &str {
    assert_eq!(answer["v"], 1);
    assert_eq!(answer["ok"], false, "expected a failure: {answer}");
    answer["error"].as_str().unwrap()
}

/// A test's own scratch directory, removed when dropped (even with mode-000 directories in it).
pub struct Scratch(PathBuf);

impl Scratch {
    pub fn new() -> Scratch {
        let dir = tempfile::Builder::new()
            .prefix("cad-runner-test-")
            .tempdir()
            .unwrap()
            .keep();
        Scratch(dir)
    }

    /// A path for a file in it.
    pub fn file(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }

    /// A new empty directory in it.
    pub fn dir(&self) -> PathBuf {
        tempfile::Builder::new()
            .prefix("d-")
            .tempdir_in(&self.0)
            .unwrap()
            .keep()
    }
}

impl Drop for Scratch {
    /// Removed by the stand-in's `remove-tree` in a child process: a broken removal routine
    /// (a mutation, say) then crashes that child, not this test harness.
    fn drop(&mut self) {
        let _ = std::process::Command::new(fake_engine())
            .arg("remove-tree")
            .arg(&self.0)
            .arg("30")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

pub async fn wait_for_file(path: &Path) -> String {
    for _ in 0..100 {
        if let Ok(text) = std::fs::read_to_string(path) {
            if text.ends_with('\n') {
                return text;
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("the fake engine never wrote {}", path.display());
}

/// Whether the process is still running (a zombie is not: it has exited).
#[cfg(target_os = "linux")]
pub fn running(pid: u32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => {
            let state = stat
                .rsplit_once(')')
                .and_then(|(_, rest)| rest.split_whitespace().next())
                .unwrap_or("");
            !matches!(state, "Z" | "X" | "x")
        }
        Err(_) => false,
    }
}

/// Waits up to two seconds for the process to stop running.
#[cfg(target_os = "linux")]
pub async fn gone(pid: u32) -> bool {
    for _ in 0..40 {
        if !running(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    false
}

/// Starts the runner binary (a child process, killed on drop) with every stand-in command
/// allowed and these extra arguments; waits until it listens.
pub async fn spawn_runner(
    scratch: &Scratch,
    extra: &[&std::ffi::OsStr],
) -> (tokio::process::Child, SocketAddr) {
    let commands = scratch.file("commands.txt");
    std::fs::write(&commands, FAKE_COMMANDS.join("\n")).unwrap();
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let listen = format!("127.0.0.1:{port}");
    let child = tokio::process::Command::new(env!("CARGO_BIN_EXE_jarvis-cad-runner"))
        .args(["--listen", &listen, "--engine"])
        .arg(fake_engine())
        .arg("--commands")
        .arg(&commands)
        .args(extra)
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let addr: SocketAddr = listen.parse().unwrap();
    for _ in 0..200 {
        if TcpStream::connect(addr).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    (child, addr)
}
