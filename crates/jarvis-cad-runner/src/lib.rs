//! `jarvis-cad-runner`: runs the CAD engine (`cadcraft-cli mcp`) for the JARVIS server.
//!
//! It lives in the locked-down `cad` container and listens on the internal network. One TCP
//! connection carries one request line and one answer line (UTF-8 JSON, `\n`-ended), then
//! closes (CAD board slice 1, decision C-6). Every replay starts a fresh engine with an empty
//! environment in a new empty directory and its own process group; the group is killed and the
//! directory removed before the answer is written, or when the time limit passes. One replay
//! runs at a time and at most two requests wait for it.
//!
//! The client is on the internal network but is not trusted either: the request line is read
//! with a size and a time limit, and command ids are checked before any engine starts.

pub mod engine;
pub mod protocol;
pub mod remove;
pub mod replay;
pub mod scan;

use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;

pub use protocol::{Answer, ErrorCode, Request};
pub use replay::{check, load_commands, replay, Config, MAX_REQUEST_BYTES};

/// Connections handled at once; more are answered `busy` straight away.
pub const MAX_CONNECTIONS: usize = 8;

/// Requests waiting for the replay slot; one more is answered `busy` straight away.
pub const MAX_WAITING: usize = 2;

/// How long writing the answer may take.
const WRITE_LIMIT: Duration = Duration::from_secs(10);

/// Whether container mode may be switched on: only when asked for (`--container`) and only
/// when the runner is pid 1, as the `cad` image's entry point makes it (no init in front).
///
/// This is a guard against starting it by mistake, not a proof of being in a container: a
/// process that is pid 1 of some other PID namespace passes too. In container mode `kill(-1)`
/// and emptying the work root reach every process and file the runner's user can reach in that
/// namespace.
pub fn container_mode_allowed(flag: bool, pid: u32) -> bool {
    flag && pid == 1
}

struct Shared {
    cfg: Config,
    slot: Semaphore,
    waiting: Semaphore,
}

/// Serves connections for ever.
pub async fn serve(listener: TcpListener, cfg: Config) {
    let shared = Arc::new(Shared {
        cfg,
        slot: Semaphore::new(1),
        waiting: Semaphore::new(MAX_WAITING),
    });
    let connections = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    loop {
        let Ok((stream, _)) = listener.accept().await else {
            // Out of file descriptors and the like: wait a little rather than spin.
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        };
        match connections.clone().try_acquire_owned() {
            Ok(permit) => {
                let shared = shared.clone();
                tokio::spawn(async move {
                    handle(stream, &shared).await;
                    drop(permit);
                });
            }
            Err(_) => {
                tokio::spawn(write_answer(stream, Answer::failure(ErrorCode::Busy)));
            }
        }
    }
}

/// One connection: read the line, check it, wait for the slot, run it, answer.
async fn handle(stream: TcpStream, shared: &Shared) {
    let (read, write) = stream.into_split();
    let mut reader = BufReader::new(read);
    let line = tokio::time::timeout(
        shared.cfg.read_limit_value(),
        engine::read_bounded_line(&mut reader, MAX_REQUEST_BYTES),
    )
    .await;
    let request = match line {
        // The line's bytes are dropped here: only the parsed request (its params still raw
        // text) is kept while waiting.
        Ok(Ok(Some(line))) => Request::parse(&line),
        // Too long, too slow, cut off or empty.
        _ => None,
    };
    let answer = match request {
        Some(request) => answer_request(request, shared).await,
        None => Answer::failure(ErrorCode::BadRequest),
    };
    if let Ok(stream) = reader.into_inner().reunite(write) {
        write_answer(stream, answer).await;
    }
    if shared.cfg.must_exit() {
        // Container mode could not empty its work root (see `Config::must_exit`).
        eprintln!("jarvis-cad-runner: exiting: the work root could not be emptied");
        std::process::exit(3);
    }
}

async fn answer_request(request: Request, shared: &Shared) -> Answer {
    if let Err(code) = check(&shared.cfg, &request) {
        return Answer::failure(code);
    }
    let Ok(waiting) = shared.waiting.try_acquire() else {
        return Answer::failure(ErrorCode::Busy);
    };
    let wait = shared.cfg.busy_wait_value();
    let Ok(Ok(_turn)) = tokio::time::timeout(wait, shared.slot.acquire()).await else {
        return Answer::failure(ErrorCode::Busy);
    };
    drop(waiting);
    // The engine is killed and its directory removed inside `replay`, before the slot is freed.
    replay(&shared.cfg, request).await
}

async fn write_answer(mut stream: TcpStream, answer: Answer) {
    let line = answer.to_line();
    drop(answer);
    let _ = tokio::time::timeout(WRITE_LIMIT, async {
        stream.write_all(&line).await?;
        stream.shutdown().await
    })
    .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn container_mode_gate() {
        assert!(container_mode_allowed(true, 1));
        assert!(!container_mode_allowed(true, 12));
        assert!(!container_mode_allowed(false, 1));
    }
}
