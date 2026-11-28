//! `jarvis-cad-runner`: runs the CAD engine (`cadcraft-cli mcp`) for the JARVIS server.
//!
//! It lives in the locked-down `cad` container and listens on the internal network. One TCP
//! connection carries one request line and one answer line (UTF-8 JSON, `\n`-ended), then
//! closes (CAD board slice 1, decision C-6). Every replay starts a fresh engine with an empty
//! environment in a new empty directory, and the engine is killed and the directory removed
//! before the answer is written, or when the time limit passes. One replay runs at a time.
//!
//! The client is on the internal network but is not trusted either: the request line is read
//! with a size and a time limit, and command ids are checked before any engine starts.

pub mod engine;
pub mod protocol;
pub mod replay;

use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;

pub use protocol::{Answer, ErrorCode, Request};
pub use replay::{check, load_commands, replay, Config, MAX_REQUEST_BYTES};

/// Connections handled at once; more are answered `busy` straight away.
pub const MAX_CONNECTIONS: usize = 8;

/// How long writing the answer may take.
const WRITE_LIMIT: Duration = Duration::from_secs(10);

/// Serves connections for ever.
pub async fn serve(listener: TcpListener, cfg: Config) {
    let cfg = Arc::new(cfg);
    let slot = Arc::new(Semaphore::new(1));
    let connections = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    loop {
        let Ok((stream, _)) = listener.accept().await else {
            // Out of file descriptors and the like: wait a little rather than spin.
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        };
        let (cfg, slot) = (cfg.clone(), slot.clone());
        match connections.clone().try_acquire_owned() {
            Ok(permit) => {
                tokio::spawn(async move {
                    handle(stream, &cfg, &slot).await;
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
async fn handle(stream: TcpStream, cfg: &Config, slot: &Semaphore) {
    let (read, write) = stream.into_split();
    let mut reader = BufReader::new(read);
    let line = tokio::time::timeout(
        cfg.read_limit_value(),
        engine::read_bounded_line(&mut reader, MAX_REQUEST_BYTES),
    )
    .await;
    let answer = match line {
        Ok(Ok(Some(line))) => answer_line(&line, cfg, slot).await,
        // Too long, too slow, cut off or empty.
        _ => Answer::failure(ErrorCode::BadRequest),
    };
    let stream = match reader.into_inner().reunite(write) {
        Ok(stream) => stream,
        Err(_) => return,
    };
    write_answer(stream, answer).await;
}

async fn answer_line(line: &[u8], cfg: &Config, slot: &Semaphore) -> Answer {
    let Some(request) = Request::parse(line) else {
        return Answer::failure(ErrorCode::BadRequest);
    };
    if let Err(code) = check(cfg, &request) {
        return Answer::failure(code);
    }
    let Ok(Ok(_turn)) = tokio::time::timeout(cfg.busy_wait_value(), slot.acquire()).await else {
        return Answer::failure(ErrorCode::Busy);
    };
    // The engine is killed and its directory removed inside `replay`, before the slot is freed.
    replay(cfg, request).await
}

async fn write_answer(mut stream: TcpStream, answer: Answer) {
    let line = answer.to_line();
    let _ = tokio::time::timeout(WRITE_LIMIT, async {
        stream.write_all(&line).await?;
        stream.shutdown().await
    })
    .await;
}
